"""Commit JSON files atomically; a successful return means replacement succeeded."""
import json
import os
import tempfile


class StorageWriteError(OSError):
    """Persistence failed. Safe to report without exposing a user path."""


def atomic_write_json(path, value):
    directory = os.path.dirname(os.path.abspath(path))
    temp_path = None
    try:
        os.makedirs(directory, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix='.save-', suffix='.tmp', dir=directory)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
    except OSError as exc:
        raise StorageWriteError(exc.errno, 'Unable to save changes') from exc
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass  # Preserve the original write/serialization failure.


def update_user_settings(path, changes, *, remove=()):
    """Apply selected preferences under the same lock as credential cleanup."""
    from pathlib import Path
    from services.character_state import character_state_lock

    path = Path(path)
    with character_state_lock(path.with_name('credentials.json')):
        try:
            with path.open(encoding='utf-8') as handle:
                current = json.load(handle)
        except FileNotFoundError:
            current = {}
        for name in remove:
            current.pop(name, None)
        current.update(changes)
        atomic_write_json(path, current)
