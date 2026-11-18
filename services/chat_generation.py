"""Single-chat delivery receipts. Store IDs only, in the character's directory."""
from contextlib import closing
import sqlite3


def connect(db_path):
    db = sqlite3.connect(db_path)
    try:
        db.execute("ATTACH DATABASE ? AS delivery", (str(db_path) + ".delivery.sqlite",))
        db.execute("""CREATE TABLE IF NOT EXISTS delivery.receipts (
            message_id INTEGER PRIMARY KEY, request_id TEXT UNIQUE, state TEXT, assistant_id INTEGER
        )""")
        return db
    except BaseException:
        db.close()
        raise


def begin_generation(db_path, message_id, retry):
    with closing(connect(db_path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT state, assistant_id FROM delivery.receipts WHERE message_id=?",
                         (message_id,)).fetchone()
        if row and row[0] is not None and not (row[0] == "failed" and retry):
            return row
        db.execute("""INSERT INTO delivery.receipts(message_id,state) VALUES (?, 'started')
                      ON CONFLICT(message_id) DO UPDATE SET state='started'""", (message_id,))
        return None


def finish(db, message_id, state, assistant_id=None):
    db.execute("UPDATE delivery.receipts SET state=?,assistant_id=? WHERE message_id=?",
               (state, assistant_id, message_id))


def latest_message(db_path, *, connection=None):
    """Snapshot the newest message, including edits to its contents."""
    if connection is None:
        with closing(sqlite3.connect(db_path)) as db:
            return latest_message(db_path, connection=db)
    return connection.execute(
        "SELECT id, role, content, timestamp FROM messages ORDER BY id DESC LIMIT 1"
    ).fetchone()
