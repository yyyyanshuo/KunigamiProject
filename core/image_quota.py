"""Per-account daily image quota, with transactional reservations across workers.

A reservation remains charged while generation runs. Success turns it into usage;
failure releases it. Process crashes conservatively retain the slot for that day.
Day boundaries keep the existing server calendar, independent of UI language.
"""
import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from core.runtime import DATA_ROOT


def _today():
    return datetime.now().strftime('%Y-%m-%d')


def _connect():
    path = Path(DATA_ROOT) / 'configs' / 'image_usage.db'
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute('CREATE TABLE IF NOT EXISTS image_days (user_id INTEGER NOT NULL, day TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id, day))')
    conn.execute('CREATE TABLE IF NOT EXISTS image_reservations (token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, day TEXT NOT NULL)')
    conn.execute('CREATE INDEX IF NOT EXISTS image_reservation_day ON image_reservations(user_id, day)')
    return conn


def _legacy_count(uid, day):
    path = Path(DATA_ROOT) / 'users' / str(uid) / 'logs' / 'daily_gen_status.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        count = data.get('count', 0)
        return count if data.get('date') == day and type(count) is int and 0 <= count <= 2147483647 else 0
    except (OSError, ValueError, AttributeError):
        return 0


def reserve_image(user_id):
    """Return an opaque reservation, or None when this account has no slots."""
    from core.model_settings import load_config
    uid = int(user_id)
    if uid <= 0:
        raise ValueError('Invalid user')
    day = _today()
    conn = _connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        limit = load_config(uid)['image_daily_limit']
        conn.execute('INSERT OR IGNORE INTO image_days(user_id, day, used) VALUES (?, ?, ?)', (uid, day, _legacy_count(uid, day)))
        used = conn.execute('SELECT used FROM image_days WHERE user_id=? AND day=?', (uid, day)).fetchone()[0]
        pending = conn.execute('SELECT COUNT(*) FROM image_reservations WHERE user_id=? AND day=?', (uid, day)).fetchone()[0]
        if used + pending >= limit:
            conn.commit()
            return None
        token = uuid.uuid4().hex
        conn.execute('INSERT INTO image_reservations(token, user_id, day) VALUES (?, ?, ?)', (token, uid, day))
        conn.commit()
        return token
    finally:
        conn.close()


def finish_image(token, *, success):
    """Finalize exactly once; use the original day even if midnight has passed."""
    conn = _connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT user_id, day FROM image_reservations WHERE token=?', (token,)).fetchone()
        if row:
            if success:
                conn.execute('UPDATE image_days SET used=used+1 WHERE user_id=? AND day=?', row)
            conn.execute('DELETE FROM image_reservations WHERE token=?', (token,))
        conn.commit()
    finally:
        conn.close()
