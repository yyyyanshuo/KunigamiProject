"""Helpers for server-revocable Flask login sessions."""

import sqlite3

from flask import session

import core.config


def get_auth_version(user_id: int | str) -> int | None:
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    conn = sqlite3.connect(core.config.USERS_DB)
    try:
        try:
            row = conn.execute("SELECT auth_version FROM users WHERE id = ?", (uid,)).fetchone()
        except sqlite3.OperationalError:
            legacy_row = conn.execute("SELECT id FROM users WHERE id = ?", (uid,)).fetchone()
            row = (1,) if legacy_row else None
    finally:
        conn.close()
    if not row:
        return None
    return int(row[0] or 1)


def establish_authenticated_session(user_id: int | str) -> None:
    uid = int(user_id)
    auth_version = get_auth_version(uid)
    if auth_version is None:
        raise ValueError("User does not exist")
    session["user_id"] = uid
    session["logged_in"] = True
    session["auth_version"] = auth_version
    session.permanent = True
