"""Bounded replay for the single, flock-protected production scheduler."""
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
import sqlite3

from core.memory_periods import week_key_for_end_date

WINDOW = 7
MAX_ATTEMPTS = 3


def latest_day(now):
    # At 04:00 Beijing time the previous calendar day becomes eligible.
    return now.date() - timedelta(days=1 if now.hour >= 4 else 2)


def initialize(db, first):
    with db:
        db.execute("CREATE TABLE IF NOT EXISTS inception(first TEXT PRIMARY KEY)")
        if not db.execute("SELECT 1 FROM inception").fetchone():
            db.execute("INSERT INTO inception VALUES (?)", (first.isoformat(),))
        db.execute("CREATE TABLE IF NOT EXISTS complete(day TEXT PRIMARY KEY)")
        db.execute("""CREATE TABLE IF NOT EXISTS work(
            day TEXT, owner TEXT, kind TEXT, entity TEXT, attempts INTEGER, done INTEGER,
            PRIMARY KEY(day,owner,kind,entity))""")


def attempt(db, key, action):
    row = db.execute("SELECT attempts,done FROM work WHERE (day,owner,kind,entity)=(?,?,?,?)", key).fetchone()
    if row and (row[1] or row[0] >= MAX_ATTEMPTS):
        return bool(row[1])
    with db:
        db.execute("""INSERT INTO work VALUES(?,?,?,?,1,0)
            ON CONFLICT(day,owner,kind,entity) DO UPDATE SET attempts=attempts+1""", key)
    try:
        action()
    except Exception as exc:
        print(f"[memory recovery] {key} attempt {(row[0] if row else 0)+1} failed ({type(exc).__name__})", flush=True)
        return False
    with db:
        db.execute("UPDATE work SET done=1 WHERE (day,owner,kind,entity)=(?,?,?,?)", key)
    return True


def process_user(db, day, uid):
    from app import _get_groups_config_file, _get_characters_config_file, get_paths
    from blueprints.group import update_group_short_memory
    from core.context import set_background_user, clear_background_user
    from services.memory import update_short_memory_for_date, generate_medium_memory_for_date, generate_long_memory_for_week
    from services.memory_store import load_json_object

    stamp = day.isoformat()
    owner = str(uid)

    def daily(char):
        result = update_short_memory_for_date(char, stamp, user_id=uid)
        if result.ok:
            result = generate_medium_memory_for_date(char, stamp, user_id=uid, missing_only=True)
        if not result.ok:
            raise RuntimeError(result.status)

    def weekly(char):
        _, prompts = get_paths(char, user_id=uid)
        if not (Path(prompts) / "5_memory_medium.json").exists():
            return
        result = generate_long_memory_for_week(char, week_key_for_end_date(day), user_id=uid, missing_only=True)
        if not result.ok:
            raise RuntimeError(result.status)

    set_background_user(uid)
    try:
        groups_ok = True
        for group in load_json_object(_get_groups_config_file()):
            if not attempt(db, (stamp, owner, "group", group),
                           lambda: update_group_short_memory(group, stamp, scheduled=True)):
                groups_ok = False
        if not groups_ok:
            return False
        ok = True
        for char in load_json_object(_get_characters_config_file()):
            if not attempt(db, (stamp, owner, "daily", char), lambda: daily(char)):
                ok = False
                continue
            if day.weekday() == 6:
                monday = (day - timedelta(days=6)).isoformat()
                pending = db.execute("""SELECT 1 FROM work WHERE owner=? AND day BETWEEN ? AND ?
                    AND done=0 AND (kind='group' OR (kind='daily' AND entity=?)) LIMIT 1""",
                                     (owner, monday, stamp, char)).fetchone()
                if pending or not attempt(db, (stamp, owner, "weekly", char), lambda: weekly(char)):
                    ok = False
        return ok
    finally:
        clear_background_user()


def recover(db, latest, users, worker=process_user):
    first = date.fromisoformat(db.execute("SELECT first FROM inception").fetchone()[0])
    start = max(first, latest - timedelta(days=WINDOW - 1))
    unreadable = set()
    for offset in range((latest - start).days + 1):
        day = start + timedelta(days=offset)
        stamp = day.isoformat()
        if db.execute("SELECT 1 FROM complete WHERE day=?", (stamp,)).fetchone():
            continue
        ok = True
        for uid in users:
            if (uid, day.isocalendar()[:2]) in unreadable and day.weekday() == 6:
                ok = False
                continue
            try:
                if not worker(db, day, uid):
                    ok = False
            except Exception as exc:
                unreadable.add((uid, day.isocalendar()[:2]))
                print(f"[memory recovery] {stamp} user {uid}: {type(exc).__name__}", flush=True)
                ok = False
        if ok:
            with db:
                db.execute("INSERT INTO complete VALUES (?)", (stamp,))
    # Retain prior-week failures for the Sunday dependency check.
    cutoff = (start-timedelta(days=7)).isoformat()
    with db:
        db.execute("DELETE FROM complete WHERE day<?", (cutoff,))
        db.execute("DELETE FROM work WHERE day<?", (cutoff,))


def maintenance_callback(root, now):
    from core.config import USERS_DB
    path = Path(root) / "configs" / "maintenance_recovery.db"
    with closing(sqlite3.connect(path)) as db:
        initialize(db, latest_day(now()))

    def run():
        with closing(sqlite3.connect(path)) as db:
            # An unavailable account database must never mark a day complete.
            uri = Path(USERS_DB).resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as accounts:
                users = [row[0] for row in accounts.execute("SELECT id FROM users ORDER BY id")]
            from core.account_lifecycle import pending
            recover(db, latest_day(now()), [uid for uid in users if not pending(uid, root)])
    return run
