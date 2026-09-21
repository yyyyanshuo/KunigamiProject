"""Per-user, per-conversation read cursors, independent of message timestamps."""

import os
import sqlite3
from contextlib import closing

from services.memory_store import atomic_write_json, load_json_object, memory_file_lock


CURSORS_KEY = "_message_ids"


def count_unread(cursor, state, kind, conversation_id):
    last_id = state.get(CURSORS_KEY, {}).get(kind, {}).get(conversation_id)
    if isinstance(last_id, int) and not isinstance(last_id, bool):
        cursor.execute(
            "SELECT COUNT(*) FROM messages WHERE id > ? AND role != 'user'", (last_id,)
        )
    else:
        # Old timestamp records remain readable until this conversation is read again.
        timestamp = state.get(conversation_id, "2000-01-01 00:00:00")
        cursor.execute(
            "SELECT COUNT(*) FROM messages WHERE timestamp > ? AND role != 'user'",
            (timestamp,),
        )
    return cursor.fetchone()[0]


def mark_conversations_read(status_file, targets):
    """targets: (kind, id, database_path, observed_message_id or None).

    None supports old callers by capturing the database maximum. New clients
    supply the message ID they displayed, so concurrent arrivals stay unread.
    A single atomic write covers the whole batch; read progress never retreats.
    """
    snapshots = []
    for kind, conversation_id, db_path, observed_id in targets:
        if observed_id is not None and (
            not isinstance(observed_id, int) or isinstance(observed_id, bool) or observed_id < 0
        ):
            raise ValueError("last_message_id must be a non-negative integer")
        maximum = 0
        if os.path.isfile(db_path):
            with closing(sqlite3.connect(db_path)) as conn:
                maximum = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
        snapshots.append((kind, conversation_id, min(observed_id, maximum) if observed_id is not None else maximum))

    with memory_file_lock(status_file):
        state = load_json_object(status_file)
        cursors = state.setdefault(CURSORS_KEY, {})
        results = []
        for kind, conversation_id, last_id in snapshots:
            scope = cursors.setdefault(kind, {})
            scope[conversation_id] = max(scope.get(conversation_id, 0), last_id)
            results.append({"type": kind, "id": conversation_id, "last_read_id": scope[conversation_id]})
        atomic_write_json(status_file, state)
    return results


def remove_read_state(status_file, kind, conversation_id):
    """Delete only this conversation's cursor without overwriting other readers."""
    with memory_file_lock(status_file):
        state = load_json_object(status_file)
        state.pop(conversation_id, None)
        state.get(CURSORS_KEY, {}).get(kind, {}).pop(conversation_id, None)
        atomic_write_json(status_file, state)
