"""Serialize a user's character catalog and the map state derived from it."""
import errno
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from core.json_storage import StorageWriteError
from services.memory_store import MemoryStoreBusy, atomic_write_json, load_json_object, memory_file_lock

try:
    import fcntl
except ImportError:  # Windows development environments
    fcntl = None


_active = threading.local()


@contextmanager
def _file_lock(path, timeout):
    if fcntl is None:
        try:
            with memory_file_lock(path, timeout=timeout):
                yield
        except MemoryStoreBusy as exc:
            raise StorageWriteError(errno.EBUSY, 'Character state is busy') from exc
        return

    # Keep the inode stable across atomic replacements of the catalog. The OS
    # releases the lock even if the worker dies. Never unlink the lock file.
    deadline = time.monotonic() + timeout
    with open(str(path) + '.state.lock', 'a+b') as handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise StorageWriteError(errno.EBUSY, 'Character state is busy') from None
                time.sleep(0.025)
        yield


def _checked_target(catalog, key):
    root = catalog.parent.parent
    if not re.fullmatch(
        r'configs/(?:characters|locations|character_positions|user_position)\.json'
        r'|characters/[A-Za-z0-9_-]{1,128}/prompts/2_relationship\.json', key
    ):
        raise StorageWriteError(errno.EINVAL, 'Invalid state journal target')
    target = root / key
    if target.resolve() != target:
        raise StorageWriteError(errno.EINVAL, 'Unsafe state journal target')
    return target


def _restore(catalog, journal):
    originals = load_json_object(journal, missing_ok=False)
    for key, value in originals.items():
        target = _checked_target(catalog, key)
        if value is None:
            target.unlink(missing_ok=True)
        else:
            atomic_write_json(target, value)
    journal.unlink()


@contextmanager
def state_batch(catalog):
    """Undo interrupted writes before the next owner enters the catalog lock."""
    catalog = Path(catalog).absolute()
    with character_state_lock(catalog):
        parent = getattr(_active, 'batch', None)
        if parent and parent[0] == catalog:
            yield
            return
        journal = catalog.with_suffix('.pending.json')
        originals = {}
        _active.batch = (catalog, journal, originals)
        try:
            yield
            if originals:
                journal.unlink()
        except Exception:
            if journal.exists():
                _restore(catalog, journal)
            raise
        finally:
            _active.batch = parent


def remember_state_files(paths):
    catalog, journal, originals = _active.batch
    added = {}
    for path in paths:
        target = Path(path).absolute()
        key = target.relative_to(catalog.parent.parent).as_posix()
        _checked_target(catalog, key)
        if key not in originals:
            added[key] = load_json_object(target, missing_ok=False) if target.exists() else None
    if added:
        atomic_write_json(journal, {**originals, **added})
        originals.update(added)


def remember_map_write(path):
    if getattr(_active, 'batch', None):
        remember_state_files([path])


@contextmanager
def character_state_lock(catalog, *, timeout=5):
    """Use the same lock for nested map operations in the same thread/user."""
    path = Path(catalog).absolute()
    key = (os.getpid(), path)
    held = getattr(_active, 'keys', ())
    if key in held:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(path, timeout):
        _active.keys = (*held, key)
        try:
            if path.name == 'characters.json':
                journal = path.with_suffix('.pending.json')
                if journal.exists():
                    _restore(path, journal)
            yield
        finally:
            _active.keys = held


def diary_attempt_is_current(info, day, attempt):
    return isinstance(info, dict) and (attempt is None or (
        info.get('bedtime_diary_date') == day
        and info.get('bedtime_diary_status') == 'running'
        and info.get('bedtime_diary_attempts') == attempt
    ))


def finish_diary_attempt(catalog, char_id, day, status, timestamp, *, error=None, attempt=None):
    """Publish a delayed result only while this date and attempt still own the job."""
    with character_state_lock(catalog):
        config = load_json_object(catalog, missing_ok=False)
        info = config.get(char_id)
        if not diary_attempt_is_current(info, day, attempt):
            return False
        info.update(bedtime_diary_date=day, bedtime_diary_status=status,
                    bedtime_diary_updated_at=timestamp)
        if status == 'success':
            info['last_diary_date'] = day
            info['bedtime_diary_last_error'] = None
        elif error:
            info['bedtime_diary_last_error'] = str(error)[:500]
        atomic_write_json(catalog, config)
        return True
