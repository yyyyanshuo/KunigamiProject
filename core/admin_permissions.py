"""Administrator authority comes only from the server environment."""
import os
from functools import wraps
from flask import jsonify, redirect, request, session


def admin_user_ids():
    raw = os.getenv('SAKI_ADMIN_USER_IDS', '').strip()
    if not raw:
        return frozenset()
    parts = [part.strip() for part in raw.split(',')]
    if any(not part.isascii() or not part.isdigit() or int(part) <= 0 for part in parts):
        return frozenset()  # Invalid runtime configuration never grants access.
    return frozenset(int(part) for part in parts)


def is_admin(user_id):
    if type(user_id) not in (int, str):
        return False
    try:
        return int(user_id) in admin_user_ids()
    except (TypeError, ValueError, OverflowError):
        return False


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_admin(session.get('user_id')) or session.get('impersonator_user_id'):
            if request.path.startswith('/api/'):
                return jsonify(error='Forbidden'), 403
            return redirect('/')
        return view(*args, **kwargs)
    return wrapped
