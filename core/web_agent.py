"""Single-slot browser jobs. Private credentials never appear in argv or logs."""
from contextlib import contextmanager
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit
import uuid


class AgentUnavailable(Exception):
    pass


class AgentBusy(Exception):
    pass


def runtime_root(data_root):
    path = Path(data_root) / '.agent-runtime'
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.agent-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


@contextmanager
def slot_lock(root):
    with (Path(root) / 'slot.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def validate_url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        raise ValueError('invalid_url')
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
            or parsed.password or parsed.port not in (None, 80, 443) or '\\' in value):
        raise ValueError('invalid_url')
    host = parsed.hostname.lower().rstrip('.')
    if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
        raise ValueError('invalid_url')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return value
    if not address.is_global or address.is_multicast:
        raise ValueError('invalid_url')
    return value


def enqueue(data_root, *, user_id, char_id, url, cookie, ui_language, output_language):
    uid = int(user_id)
    if uid <= 0 or not re.fullmatch(r'[A-Za-z0-9_]+', str(char_id)) or not cookie:
        raise ValueError('invalid_job')
    validate_url(url)
    root = runtime_root(data_root)
    with slot_lock(root):
        heartbeat = read_json(root / 'heartbeat.json')
        if time.time() - float(heartbeat.get('updated', 0)) > 20:
            raise AgentUnavailable()
        if (root / 'pending.json').exists() or (root / 'active.json').exists():
            raise AgentBusy()
        job = {'id': uuid.uuid4().hex, 'created': time.time(), 'user_id': uid,
               'char_id': char_id, 'url': url, 'cookie': cookie,
               'ui_language': ui_language, 'output_language': output_language}
        user_root = Path(data_root) / 'users' / str(uid)
        (user_root / 'agent_input.json').unlink(missing_ok=True)
        atomic_json(user_root / 'agent_state.json', {
            'running': True, 'char_id': char_id, 'status': 'queued', 'question': '',
            'job_id': job['id'], 'last_activity': time.time(),
        })
        try:
            atomic_json(root / 'pending.json', job)
        except Exception:
            (user_root / 'agent_state.json').unlink(missing_ok=True)
            raise
        return job['id']


def set_worker_heartbeat(root):
    atomic_json(Path(root) / 'heartbeat.json', {'updated': time.time()})


def take_job(root):
    with slot_lock(root):
        path = Path(root) / 'pending.json'
        job = read_json(path)
        if not job:
            path.unlink(missing_ok=True)
            return None
        atomic_json(Path(root) / 'active.json', {key: job[key] for key in ('id', 'user_id', 'char_id')})
        path.unlink()
        return job


def finish_job(data_root, root, job, status='stopped'):
    with slot_lock(root):
        uid = job.get('user_id')
        from core.account_lifecycle import pending
        if isinstance(uid, int) and uid > 0 and not pending(uid, data_root):
            folder = Path(data_root) / 'users' / str(uid)
            atomic_json(folder / 'agent_state.json', {
                'running': False, 'status': status, 'char_id': job.get('char_id', ''),
                'question': '', 'last_activity': time.time(),
            })
            (folder / 'agent_input.json').unlink(missing_ok=True)
        (Path(root) / 'active.json').unlink(missing_ok=True)

def discard_account_jobs(data_root, uid):
    root = runtime_root(data_root)
    with slot_lock(root):
        for name in ('active.json', 'pending.json'):
            path = root / name
            if path.exists():
                job = json.loads(path.read_text(encoding='utf-8'))
                if job.get('user_id') == uid:
                    path.unlink()
