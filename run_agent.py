"""One production browser job at a time; no public API listener or forged sessions."""
from contextlib import closing
from core.account_lifecycle import activity, pending, DeletionPending
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import time

from core.web_agent import (finish_job, read_json, runtime_root,
                            set_worker_heartbeat, take_job)
from services.agent_proxy import PublicWebProxy

SOURCE = Path(__file__).resolve().parent
MAX_SECONDS = 900


def child_environment(data_root):
    # The browser process does not need application secrets, COS keys or mail keys.
    allowed = ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TZ', 'PLAYWRIGHT_BROWSERS_PATH')
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env['SAKI_DATA_ROOT'] = str(data_root)
    env['SAKI_AGENT_API_URL'] = os.getenv('SAKI_AGENT_API_URL', 'http://127.0.0.1:8010')
    return env


def terminate(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def browser_process(node, data_root, job, check=False):
    args = [node, str(SOURCE / 'agent-test' / 'agent_universal.js')]
    if check:
        args.append('--check')
    process = subprocess.Popen(args, cwd=SOURCE / 'agent-test', env=child_environment(data_root),
                               stdin=subprocess.PIPE, start_new_session=True)
    try:
        process.stdin.write(json.dumps(job, ensure_ascii=False).encode())
        process.stdin.close()
        return process
    except Exception:
        terminate(process)
        raise


def recover(data_root, root):
    # Called with the exclusive worker lifetime lock, before publishing a heartbeat.
    for filename in ('active.json', 'pending.json'):
        job = read_json(root / filename)
        if job:
            finish_job(data_root, root, job, 'interrupted')
        (root / filename).unlink(missing_ok=True)


def execute_job(node, data_root, root, job, stop):
    process = None
    status = 'error'
    try:
        with PublicWebProxy() as proxy:
            process = browser_process(node, data_root, dict(job, proxy=proxy))
            started = time.monotonic()
            while process.poll() is None:
                set_worker_heartbeat(root)
                signal_data = read_json(Path(data_root) / 'users' / str(job['user_id']) / 'agent_input.json')
                if stop.is_set() or pending(job['user_id'], data_root) or signal_data.get('command') == 'stop':
                    status = 'stopped'
                    break
                if time.monotonic() - started >= MAX_SECONDS:
                    status = 'timeout'
                    break
                stop.wait(1)
            else:
                status = 'completed' if process.returncode == 0 else 'error'
            terminate(process)
    finally:
        if process:
            terminate(process)
        finish_job(data_root, root, job, status)
    print(f'Agent task finished: {status}', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true', help='Check browser startup without creating jobs or calling models')
    args = parser.parse_args()
    node = shutil.which('node')
    if not node:
        raise RuntimeError('Node.js is not installed')
    data_root = Path(os.getenv('SAKI_DATA_ROOT', '/var/lib/sakiglobal')).resolve()
    if args.check:
        with PublicWebProxy() as proxy:
            process = browser_process(node, data_root, {'proxy': proxy}, check=True)
            try:
                if process.wait(timeout=60) != 0:
                    raise RuntimeError('Browser check failed; check new-site Playwright installation')
            finally:
                terminate(process)
        print('PASS：浏览器检查完成；未调用模型，未创建任务或修改聊天。')
        return
    root = runtime_root(data_root)
    with (root / 'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another SakiGlobal agent worker is already running') from None
        recover(data_root, root)
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        print('SakiGlobal Agent 已启动：同时 1 个任务，每次最多 15 分钟、50 步。', flush=True)
        try:
            while not stop.is_set():
                set_worker_heartbeat(root)
                job = take_job(root)
                if job:
                    try:
                        with closing(activity(job['user_id'], data_root)):
                            execute_job(node, data_root, root, job, stop)
                    except DeletionPending:
                        finish_job(data_root, root, job, 'stopped')
                    except Exception:
                        # Avoid session cookies, page content or credential-bearing URLs in logs.
                        print('Agent task failed; private details omitted.', flush=True)
                else:
                    stop.wait(1)
        finally:
            (root / 'heartbeat.json').unlink(missing_ok=True)
            recover(data_root, root)


if __name__ == '__main__':
    main()
