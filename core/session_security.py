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
    from core.account_lifecycle import DeletionPending, request_activity
    try:
        request_activity(uid)
        deleting = False
    except DeletionPending:
        deleting = True
    auth_version = get_auth_version(uid)
    if auth_version is None:
        raise ValueError("User does not exist")
    session["user_id"] = uid
    session["logged_in"] = True
    session["auth_version"] = auth_version
    session.permanent = True
    from core.localization import reconcile_login_language
    if not deleting:
        reconcile_login_language(uid)


# Chat context is a cache hint, not authentication. Keep its cookie separate so
# slow chat requests cannot write an old login identity after an account switch.
def _memory_context_serializer():
    from flask import current_app
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(current_app.secret_key, salt="memory-context-v1")


def _memory_context_cookie_name():
    from flask import current_app
    return current_app.config["SESSION_COOKIE_NAME"] + "_memory_context"


def request_memory_context_changed(user_id, context_key):
    from flask import current_app, g, request
    from itsdangerous import BadData
    state = getattr(g, "_memory_context_state", None)
    if state is None:
        value = request.cookies.get(_memory_context_cookie_name())
        state = {}
        if value:
            try:
                decoded = _memory_context_serializer().loads(
                    value, max_age=int(current_app.permanent_session_lifetime.total_seconds())
                )
                if isinstance(decoded, dict):
                    state = decoded
            except BadData:
                pass
        elif session.get("user_id") == user_id:
            # Read old sessions during rollout without modifying the login cookie.
            state = {"user_id": user_id, "context": session.get("_last_memory_context")}
        g._memory_context_needs_cookie = not value
    previous = state.get("context") if state.get("user_id") == user_id else None
    if previous != context_key:
        g._memory_context_needs_cookie = True
    g._memory_context_state = {"user_id": user_id, "context": context_key}
    return previous != context_key


def save_memory_context_cookie(response):
    from flask import current_app, g
    if getattr(g, "_memory_context_needs_cookie", False):
        response.set_cookie(
            _memory_context_cookie_name(),
            _memory_context_serializer().dumps(g._memory_context_state),
            max_age=int(current_app.permanent_session_lifetime.total_seconds()),
            secure=current_app.config["SESSION_COOKIE_SECURE"],
            httponly=True,
            samesite=current_app.config["SESSION_COOKIE_SAMESITE"],
            path=current_app.config.get("SESSION_COOKIE_PATH") or "/",
            domain=current_app.config.get("SESSION_COOKIE_DOMAIN"),
        )
    return response
