from contextvars import ContextVar
from flask import session

from core.config import USERS_DB
import sqlite3
import os

# --- 后台用户上下文（定时任务用，无 request 时替代 session） ---
_background_user_var: ContextVar[int | None] = ContextVar("background_user_id", default=None)


def set_background_user(user_id: int | None) -> None:
    _background_user_var.set(user_id)


def clear_background_user() -> None:
    _background_user_var.set(None)


# --- 主动任务 API 致命错误上下文 ---
_last_api_fatal_error_var: ContextVar[dict | None] = ContextVar("last_api_fatal_error", default=None)

GEMINI_FATAL_CODES = {400, 403}
RELAY_FATAL_CODES = {401, 402, 403, 525}


def reset_api_fatal_error() -> None:
    _last_api_fatal_error_var.set(None)


def mark_api_fatal_error(route: str, status_code: int) -> None:
    _last_api_fatal_error_var.set({"route": route, "status_code": status_code})


def get_api_fatal_error() -> dict | None:
    try:
        return _last_api_fatal_error_var.get()
    except LookupError:
        return None


# --- 用户/会话相关辅助 ---

def init_users_db():
    os.makedirs(os.path.dirname(USERS_DB), exist_ok=True)
    conn = sqlite3.connect(USERS_DB)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT,
            created_at TEXT,
            provider TEXT,
            provider_user_id TEXT
        )
        """
    )
    try:
        cur.execute("ALTER TABLE users ADD COLUMN is_frozen INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS circuit_breaker (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            route TEXT NOT NULL,
            status_code INTEGER NOT NULL,
            trigger_count INTEGER DEFAULT 1,
            first_trigger_time TEXT,
            last_trigger_time TEXT,
            cooldown_until TEXT,
            UNIQUE(user_id, route, status_code)
        )
        """
    )
    try:
        cur.execute("ALTER TABLE circuit_breaker ADD COLUMN route_disabled INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


def get_current_user_id():
    try:
        bg = _background_user_var.get()
        if bg is not None:
            return bg
    except LookupError:
        pass
    try:
        return session.get("user_id")
    except Exception:
        return None


def list_all_user_ids() -> list[int]:
    if not os.path.exists(USERS_DB):
        return []
    try:
        conn = sqlite3.connect(USERS_DB)
        cur = conn.cursor()
        cur.execute("SELECT id FROM users ORDER BY id ASC")
        rows = cur.fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception as e:
        print(f"[list_all_user_ids] 错误: {e}")
        return []
