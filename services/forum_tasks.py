"""Durable ownership and completion references for the polling forum API."""
import os
import sqlite3
import sys
import time
import uuid
from contextlib import closing
from pathlib import Path

TTL = 3600
TIMEOUT = 1800
CAPACITY = 256
WORKERS = 16
PER_USER = 4


def _worker(pid):
    # Distinguish a live Linux worker from a recycled PID, including after reboot.
    if not sys.platform.startswith('linux'):
        return None
    try:
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return 'exited' if fields[0] == 'Z' else boot + ':' + fields[19]
    except (FileNotFoundError, ProcessLookupError):
        return 'exited'
    except (PermissionError, IndexError):
        return None


def _refresh(db, now):
    for pid, token in db.execute('SELECT DISTINCT pid,worker FROM forum_tasks WHERE active=1').fetchall():
        current = _worker(pid)
        if current is not None and token and current != token:
            db.execute("""UPDATE forum_tasks SET active=0,
                        updated=CASE WHEN status='working' THEN ? ELSE updated END,
                        error=CASE WHEN status='working' THEN 'Generation interrupted; check your forum history before retrying.' ELSE error END,
                        status=CASE WHEN status='working' THEN 'error' ELSE status END
                        WHERE pid=? AND worker=? AND active=1""", (now, pid, token))
    db.execute("""UPDATE forum_tasks SET status='error', updated=?, error='Generation timed out; please check history.'
                  WHERE status='working' AND created<=?""", (now, now-TIMEOUT))
    # Without a process identity (e.g. Windows), age out abandoned slots too.
    db.execute("DELETE FROM forum_tasks WHERE (active=0 OR worker='') AND status!='working' AND updated<=?",
               (now-TTL,))


def create(path, uid):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("""CREATE TABLE IF NOT EXISTS forum_tasks (
            id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, status TEXT NOT NULL, error TEXT,
            created REAL NOT NULL, updated REAL NOT NULL, pid INTEGER NOT NULL, worker TEXT NOT NULL,
            active INTEGER NOT NULL, forum_id INTEGER, background_id INTEGER, post_count INTEGER)""")
        now = time.time()
        _refresh(db, now)
        count, active, mine = db.execute(
            'SELECT COUNT(*), TOTAL(active), TOTAL(active AND user_id=?) FROM forum_tasks', (uid,)
        ).fetchone()
        if count >= CAPACITY or active >= WORKERS or mine >= PER_USER:
            return None
        task = str(uuid.uuid4())
        db.execute("""INSERT INTO forum_tasks(id,user_id,status,created,updated,pid,worker,active)
                      VALUES(?,?,'working',?,?,?,?,1)""",
                   (task, uid, now, now, os.getpid(), _worker(os.getpid()) or ''))
        return task


def fail(path, task, uid, error):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("""UPDATE forum_tasks SET status='error',error=?,updated=?
                      WHERE id=? AND user_id=? AND status='working'""", (error,time.time(),task,uid))


def release(path, task, uid):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('UPDATE forum_tasks SET active=0 WHERE id=? AND user_id=?', (task,uid))


def complete(db, task, uid, forum_id, background_id, post_count):
    """Called inside the transaction inserting/appending the generated posts."""
    now = time.time()
    changed = db.execute("""UPDATE forum_tasks SET status='done',forum_id=?,background_id=?,post_count=?,updated=?
                            WHERE id=? AND user_id=? AND status='working' AND created>?""",
                         (forum_id,background_id,post_count,now,task,uid,now-TIMEOUT))
    if changed.rowcount != 1:
        raise ValueError('Task is no longer eligible to publish')


def lookup(db, task, uid):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='forum_tasks'").fetchone():
        return None
    def owned():
        return db.execute("SELECT * FROM forum_tasks WHERE id=? AND user_id=?", (task, uid)).fetchone()

    row = owned()
    now = time.time()
    if row is not None and row["status"] == "working":
        current = _worker(row["pid"])
        if now - row["created"] >= TIMEOUT or (row["worker"] and current and current != row["worker"]):
            with db:
                db.execute("BEGIN IMMEDIATE")
                _refresh(db, now)
                row = owned()
    if row is not None and row["status"] != "working" and row["updated"] <= now - TTL:
        return None
    return row
