"""Safe shared storage helpers for JSON-backed memory files.

Memory files are written by both Gunicorn workers and the scheduler process.
The lock therefore has to be cross-process; a regular ``threading.Lock`` is
not enough.  This module deliberately uses only the standard library so the
same code also works in the Windows development environment.
"""

from __future__ import annotations

import json
import os
from core.json_storage import atomic_write_json
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

    deadline = time.monotonic() + timeout
    if not local_lock.acquire(timeout=timeout):
        raise MemoryStoreBusy("memory file is busy")
    try:
        acquired = False
        while not acquired:
            try:
                fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    with os.fdopen(fd, "w", encoding="ascii") as handle:
                        handle.write(f"pid={os.getpid()} created={time.time():.6f}\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                except BaseException:
                    os.unlink(lock_path)
                    raise
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

    finally:
        local_lock.release()


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




def append_short_memory_events(short_file, date_str, events, *, source_timezone="Asia/Shanghai", user_id=None):
    return _write_external_events(short_file, date_str, events, None, source_timezone, user_id)


def replace_short_memory_events_by_prefix(short_file, date_str, prefix, events, *, source_timezone="Asia/Shanghai", user_id=None):
    return _write_external_events(short_file, date_str, events, prefix, source_timezone, user_id)


def _write_external_events(path, day, events, prefix, source_zone, user_id):
    zone = memory_timezone(user_id)
    converted = reindex_memory_days({day: {"events": events, "timezone": source_zone}}, zone)
    with memory_file_lock(path):
        data = load_calendar_memory(path, zone)
        if prefix is not None:
            # Replacement source is already a Beijing-day group summary.
            target = data.setdefault(day, {"events": [], "last_id": 0, "timezone": zone})
            target["events"] = [e for e in target.get("events", []) if not e.get("event", "").lstrip().startswith(prefix)]
        added = 0
        for key, incoming in converted.items():
            target = data.setdefault(key, {"events": [], "last_id": 0, "timezone": zone})
            def identity_for(event):
                instant = memory_event_instant(key, event, zone)
                return (instant.timestamp() if instant else event.get("time", ""), event.get("event", ""))
            known = {identity_for(e) for e in target.get("events", [])}
            for item in incoming["events"]:
                identity = identity_for(item)
                if item.get("event", "").strip() and identity not in known:
                    target.setdefault("events", []).append(item)
                    known.add(identity)
                    added += 1
            target["events"].sort(key=lambda e: (e.get("time", ""), e.get("event", "")))
        if added or prefix is not None:
            atomic_write_json(path, data)
        return added


# One storage calendar for chats, group distribution, and daily/weekly memory.
# Character-local clocks are derived only when building their prompts.
def memory_timezone(user_id=None):
    from core.time_utils import BEIJING_TZ_NAME
    return BEIJING_TZ_NAME


def memory_now(user_id=None):
    from core.time_utils import utc_now, get_zone
    return utc_now().astimezone(get_zone(memory_timezone(user_id)))


def memory_day_bounds(day, zone):
    from datetime import date, datetime, time, timedelta
    from core.time_utils import resolve_local_datetime, format_beijing_timestamp
    value = date.fromisoformat(day)
    return tuple(format_beijing_timestamp(resolve_local_datetime(d, time(), zone))
                 for d in (value, value + timedelta(days=1)))


def memory_event_instant(day, event, zone="Asia/Shanghai"):
    from datetime import date
    from core.time_utils import parse_beijing_timestamp, parse_hhmm, resolve_local_datetime
    if event.get("timestamp"):
        instant = parse_beijing_timestamp(event["timestamp"])
        if instant:
            return instant
    clock = parse_hhmm(event.get("time"))
    if clock is None:
        return None
    return resolve_local_datetime(date.fromisoformat(day), clock, zone)


def dated_memory_event(day, event, zone, source_rows=()):
    """Keep an absolute instant, including which occurrence of a DST hour."""
    from core.time_utils import parse_beijing_timestamp, get_zone
    item = dict(event)
    instant = memory_event_instant(day, item, zone)
    if not item.get("timestamp"):
        for row in source_rows:
            candidate = parse_beijing_timestamp(row[1])
            if candidate and candidate.astimezone(get_zone(zone)).strftime("%H:%M") == item.get("time"):
                instant = candidate
                break
    if instant:
        local = instant.astimezone(get_zone(zone))
        item.update(time=local.strftime("%H:%M"), timestamp=local.isoformat())
    return item


def reindex_memory_days(data, zone):
    """Rebucket derived events, retaining exact source-range cursor coverage.

    A cursor from one calendar day must never cover an entire day in another
    zone. Coverage intervals are clipped to their intersection with each new day.
    Untimed legacy entries retain their date; no event instant is invented.
    """
    from datetime import timedelta
    from core.time_utils import get_zone, parse_beijing_timestamp, format_beijing_timestamp
    result = {}
    def bucket(day):
        return result.setdefault(day, {"events": [], "last_id": 0, "timezone": zone, "coverage": []})
    for day, raw in data.items():
        memory_day_bounds(day, zone)  # reject malformed keys instead of losing data
        info = raw if isinstance(raw, dict) else {"events": raw}
        old_zone = info.get("timezone", "Asia/Shanghai")
        for event in info.get("events", []):
            item = dated_memory_event(day, event, old_zone)
            instant = memory_event_instant(day, item, old_zone)
            target_day = day
            if instant:
                local = instant.astimezone(get_zone(zone))
                target_day = local.strftime("%Y-%m-%d")
                item["time"] = local.strftime("%H:%M")
                item["timestamp"] = local.isoformat()
            bucket(target_day)["events"].append(item)
        # Chat message IDs and call-turn IDs are separate sequences. Rebucket
        # their coverage independently, including calls that cross midnight.
        for cursor_key, coverage_key in (("last_id", "coverage"), ("call_last_id", "call_coverage")):
            coverage = list(info.get(coverage_key, []))
            last_id = int(info.get(cursor_key, 0) or 0)
            if last_id:
                start, end = memory_day_bounds(day, old_zone)
                coverage.append({"start": start, "end": end, "last_id": last_id})
            if not coverage and not info.get("events"):
                bucket(day)
            for span in coverage:
                start, end = span["start"], span["end"]
                current = parse_beijing_timestamp(start).astimezone(get_zone(zone)).date()
                final = (parse_beijing_timestamp(end) - timedelta(microseconds=1)).astimezone(get_zone(zone)).date()
                while current <= final:
                    key = current.isoformat()
                    left, right = memory_day_bounds(key, zone)
                    clipped = {"start": max(start, left), "end": min(end, right), "last_id": int(span["last_id"])}
                    target_coverage = bucket(key).setdefault(coverage_key, [])
                    if clipped["start"] < clipped["end"] and clipped not in target_coverage:
                        target_coverage.append(clipped)
                    current += timedelta(days=1)
    for info in result.values():
        info["events"].sort(key=lambda e: (e.get("time", ""), e.get("event", "")))
    return result


def load_calendar_memory(path, zone):
    """Caller holds the memory lock. One recoverable backup before reindexing."""
    data = load_json_object(path)
    if any(not isinstance(day, dict) or day.get("timezone", "Asia/Shanghai") != zone
           for day in data.values()):
        # Preserve a fresh pre-Beijing snapshot even if an older conversion
        # already created .before-timezone.json.
        backup = path + (".before-beijing.json" if zone == "Asia/Shanghai" else ".before-timezone.json")
        if not os.path.exists(backup):
            atomic_write_json(backup, data)
        data = reindex_memory_days(data, zone)
        atomic_write_json(path, data)
    return data


def memory_row_is_covered(row, day, *, source="chat"):
    cursor_key, coverage_key = ("call_last_id", "call_coverage") if source == "call" else ("last_id", "coverage")
    if int(row[0]) <= int(day.get(cursor_key, 0) or 0):
        return True
    return any(span["start"] <= row[1] < span["end"] and int(row[0]) <= int(span["last_id"])
               for span in day.get(coverage_key, []))


def split_memory_clock_epochs(rows, zone):
    """Never put both occurrences of a repeated DST hour in one model batch."""
    from core.time_utils import parse_beijing_timestamp, get_zone
    epochs = []
    offset = None
    for row in rows:
        current = parse_beijing_timestamp(row[1]).astimezone(get_zone(zone)).utcoffset()
        if not epochs or current != offset:
            epochs.append([])
            offset = current
        epochs[-1].append(row)
    return epochs
