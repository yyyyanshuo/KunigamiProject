"""Safe shared storage helpers for JSON-backed memory files.

Memory files are written by both Gunicorn workers and the scheduler process.
The lock therefore has to be cross-process; a regular ``threading.Lock`` is
not enough.  This module deliberately uses only the standard library so the
same code also works in the Windows development environment.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator


class MemoryStoreError(RuntimeError):
    pass


class MemoryStoreBusy(MemoryStoreError):
    pass


_thread_locks: dict[str, threading.RLock] = {}
_thread_locks_guard = threading.Lock()


def _thread_lock(path: str) -> threading.RLock:
    key = os.path.abspath(path)
    with _thread_locks_guard:
        return _thread_locks.setdefault(key, threading.RLock())


@contextmanager
def memory_file_lock(
    path: str,
    *,
    timeout: float = 30.0,
    stale_after: float = 1800.0,
) -> Iterator[None]:
    """Lock one memory file across threads and application processes."""

    absolute = os.path.abspath(path)
    lock_path = f"{absolute}.lock"
    os.makedirs(os.path.dirname(absolute), exist_ok=True)
    local_lock = _thread_lock(absolute)

    with local_lock:
        deadline = time.monotonic() + timeout
        acquired = False
        while not acquired:
            try:
                fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="ascii") as handle:
                    handle.write(f"pid={os.getpid()} created={time.time():.6f}\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                acquired = True
            except FileExistsError:
                try:
                    age = time.time() - os.path.getmtime(lock_path)
                    if age > stale_after:
                        os.unlink(lock_path)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise MemoryStoreBusy(f"memory file is busy: {absolute}")
                time.sleep(0.05)

        try:
            yield
        finally:
            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                pass


def load_json_object(path: str, *, missing_ok: bool = True) -> dict[str, Any]:
    if not os.path.exists(path):
        if missing_ok:
            return {}
        raise MemoryStoreError(f"memory file does not exist: {path}")
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise MemoryStoreError(f"unable to read memory file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MemoryStoreError(f"memory file root must be an object: {path}")
    return value


def atomic_write_json(path: str, value: Any) -> None:
    """Write JSON next to the target and atomically replace it."""

    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


def append_short_memory_events(
    short_file: str,
    date_str: str,
    events: list[dict[str, str]],
) -> int:
    """Append deduplicated external/manual events without changing private cursor."""

    if not events:
        return 0
    with memory_file_lock(short_file):
        current_data = load_json_object(short_file)
        day_data = current_data.get(date_str, {})
        if isinstance(day_data, list):
            existing = list(day_data)
            last_id = 0
        elif isinstance(day_data, dict):
            existing = list(day_data.get("events", []))
            last_id = int(day_data.get("last_id", 0) or 0)
        else:
            existing = []
            last_id = 0

        known = {
            (str(item.get("time", "")), str(item.get("event", "")))
            for item in existing
            if isinstance(item, dict)
        }
        added = 0
        for event in events:
            normalized = {
                "time": str(event.get("time", ""))[:5],
                "event": str(event.get("event", "")).strip(),
            }
            key = (normalized["time"], normalized["event"])
            if not normalized["event"] or key in known:
                continue
            existing.append(normalized)
            known.add(key)
            added += 1

        if added:
            existing.sort(key=lambda item: (str(item.get("time", "")), str(item.get("event", ""))))
            current_data[date_str] = {"events": existing, "last_id": last_id}
            atomic_write_json(short_file, current_data)
        return added
