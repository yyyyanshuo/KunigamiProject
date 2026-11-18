"""Per-user, per-conversation read cursors, independent of message timestamps."""

import logging
import os
import sqlite3
from contextlib import closing

from werkzeug.exceptions import NotFound

from core.runtime import LOCAL_TEST
from core.user_resources import iter_owned_chat_databases
from services.memory_store import MemoryStoreError, atomic_write_json, load_json_object, memory_file_lock


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


def chat_poll_header(user_id, status_file, *, current):
    """Count unread messages in the user's other chats and groups."""
    header = {}
    try:
        conversations = list(iter_owned_chat_databases(user_id))
        if current[0] == 'chat':
            for kind, cid, info, _ in conversations:
                if (kind, cid) == current:
                    header['deep_sleep'] = bool(info.get('deep_sleep', False))
                    break
        state = load_json_object(status_file)
        total = 0
        for kind, cid, _, db in conversations:
            if (kind, cid) != current and db.is_file():
                # The offline filesystem guard requires a plain path rather than a SQLite URI.
                source = db if LOCAL_TEST else db.as_uri() + '?mode=ro'
                with closing(sqlite3.connect(source, uri=not LOCAL_TEST, timeout=0.2)) as conn:
                    total += count_unread(conn.cursor(), state, kind, cid)
        header['unread_total'] = total
    except (MemoryStoreError, sqlite3.Error, OSError, NotFound, AttributeError):
        logging.getLogger(__name__).warning("Chat header temporarily unavailable", exc_info=True)
    return header


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
