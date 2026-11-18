"""Call-memory catch-up shared by hangups and the scheduled retry job."""

import os
import threading
from concurrent.futures import ThreadPoolExecutor

from core.context import clear_background_user, set_background_user
from core.runtime import LOCAL_TEST
from core.time_utils import get_zone, parse_beijing_timestamp
from core.utils import get_paths
from services.memory_store import (
    load_calendar_memory,
    memory_file_lock,
    memory_row_is_covered,
    memory_timezone,
)
from services.voice_calls import list_memory_turns


_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="call-memory")
_pending = set()
_pending_lock = threading.Lock()


def sync_pending_call_memories(user_id, char_id):
    """Catch up ended calls, including old anchors and previous calendar days.

    Processing progress lives in the same atomic JSON commit as the memories.
    A crash, failed summary or busy writer therefore remains eligible to retry.
    """
    turns = list_memory_turns(user_id, char_id, ended_only=True)
    if not turns:
        return {}
    zone = memory_timezone(user_id)
    _, prompts_dir = get_paths(char_id, user_id=user_id)
    path = os.path.join(prompts_dir, "6_memory_short.json")
    with memory_file_lock(path):
        snapshot = load_calendar_memory(path, zone)
    days = set()
    for row in turns:
        day = parse_beijing_timestamp(row[1]).astimezone(get_zone(zone)).date().isoformat()
        if not memory_row_is_covered(row, snapshot.get(day, {}), source="call"):
            days.add(day)
    from services.memory import update_short_memory_for_date

    results = {}
    for day in sorted(days):
        result = update_short_memory_for_date(char_id, day, user_id=user_id)
        results[day] = result
        if not result.ok:
            print(f"[Call Memory] {char_id} {day}: {result.status}; will retry")
            break
    return results


def _sync_in_background(user_id, char_id):
    try:
        set_background_user(user_id)
        sync_pending_call_memories(user_id, char_id)
    except Exception as exc:
        print(f"[Call Memory] catch-up failed for {char_id}: {type(exc).__name__}")
    finally:
        clear_background_user()
        with _pending_lock:
            _pending.discard((user_id, char_id))


def queue_call_memory(user_id, char_id):
    # Match existing offline-mode behavior: automatic model jobs stay disabled.
    if LOCAL_TEST:
        return
    key = (int(user_id), str(char_id))
    with _pending_lock:
        if key in _pending:
            return
        _pending.add(key)
    try:
        _executor.submit(_sync_in_background, *key)
    except Exception:
        with _pending_lock:
            _pending.discard(key)
        raise
