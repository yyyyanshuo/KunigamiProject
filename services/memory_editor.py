"""Merge manual character-memory edits under the same lock used by summaries."""
from collections import defaultdict, deque
from contextlib import closing
from datetime import date
from pathlib import Path
import sqlite3

from services.memory_store import (
    atomic_write_json, dated_memory_event, load_calendar_memory,
    load_json_object, memory_day_bounds, memory_event_instant, memory_file_lock, memory_timezone,
)
from core.time_utils import format_beijing_timestamp


class EditConflict(Exception):
    pass


def _events(value):
    events = value.get('events', []) if isinstance(value, dict) else value
    if not isinstance(events, list):
        raise ValueError('Invalid events')
    rows = []
    for item in events:
        if not isinstance(item, dict) or not all(isinstance(item.get(k, ''), str) for k in ('time', 'event')):
            raise ValueError('Invalid event')
        rows.append({'time': item.get('time', ''), 'event': item.get('event', '')})
    return rows


def _chat_cursor(db_path, day, moment, previous):
    history = Path(db_path)
    if not moment or not history.is_file():
        return previous
    with closing(sqlite3.connect(db_path, timeout=1)) as db:
        db.execute('PRAGMA query_only=ON')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'").fetchone():
            return previous
        row = db.execute('SELECT MAX(id) FROM messages WHERE timestamp BETWEEN ? AND ?',
                         (memory_day_bounds(day, memory_timezone())[0],
                          format_beijing_timestamp(moment.replace(second=59)))).fetchone()
        return row[0] or previous


def _short_day(current, day, rows, reset, db_path):
    zone = memory_timezone()
    saved = dict(current or {})
    originals = defaultdict(deque)
    for item in saved.get('events', []):
        originals[(item.get('time', ''), item.get('event', ''))].append(item)
    events = []
    for row in rows:
        matches = originals[(row['time'], row['event'])]
        events.append(matches.popleft() if matches else dated_memory_event(day, row, zone))
    saved.update(events=events, timezone=zone)
    saved.pop('coverage', None)
    if reset or not events:
        saved.update(last_id=0, call_last_id=0, call_coverage=[])
    else:
        last = memory_event_instant(day, events[-1], zone)
        saved['last_id'] = _chat_cursor(db_path, day, last, saved.get('last_id', 0))
    return saved


def _validate_entry(key, period, before, after):
    if not isinstance(period, str) or not period:
        raise ValueError('Invalid period')
    if key == 'short':
        if date.fromisoformat(period).isoformat() != period or not isinstance(after, list):
            raise ValueError('Invalid day')
        _events(before)
        _events(after)
    elif any(v is not None and not isinstance(v, str) for v in (before, after)):
        raise ValueError('Invalid summary')


def save_memory_edits(key, path, db_path, payload):
    patch = 'changes' in payload
    changes = payload.get('changes')
    content = payload.get('content')
    if patch and (not isinstance(changes, dict) or 'content' in payload):
        raise ValueError('Invalid changes')
    if not patch and not isinstance(content, dict):
        raise ValueError('Invalid content')

    with memory_file_lock(path, timeout=2):
        current = load_calendar_memory(path, memory_timezone()) if key == 'short' else load_json_object(path)
        if not patch:
            # Older open tabs submit a full snapshot. Preserve that API's replace semantics.
            changes = {}
            for period, value in content.items():
                if key == 'short':
                    changes[period] = {'before': _events(current.get(period, [])), 'after': _events(value),
                                       'reset': isinstance(value, dict) and value.get('reset_call_progress', False)}
                else:
                    changes[period] = {'before': current.get(period), 'after': value}

        updated = current.copy() if patch else {}
        applied = {}
        for period, change in changes.items():
            if not isinstance(change, dict) or 'before' not in change or 'after' not in change:
                raise ValueError('Invalid change')
            before, after = change['before'], change['after']
            reset = change.get('reset', False)
            if type(reset) is not bool or (reset and key != 'short'):
                raise ValueError('Invalid reset')
            _validate_entry(key, period, before, after)
            previous = current.get(period)
            visible = _events(previous if previous is not None else []) if key == 'short' else previous
            if patch and visible != before and visible != after:
                raise EditConflict()
            if patch and reset and previous:
                progress = [previous.get('last_id', 0), previous.get('call_last_id', 0),
                            previous.get('coverage', []), previous.get('call_coverage', [])]
                if change.get('progress') != progress and not (visible == after and progress == [0, 0, [], []]):
                    raise EditConflict()
            if key == 'short':
                if patch and visible == after and previous is not None and not reset:
                    value = previous   # A lost acknowledgement is safe to retry.
                else:
                    value = _short_day(previous, period, _events(after), reset, db_path)
            else:
                value = after
            if value is None:
                updated.pop(period, None)
            else:
                updated[period] = value
            applied[period] = value
        if updated != current:
            atomic_write_json(path, updated)
        return applied
