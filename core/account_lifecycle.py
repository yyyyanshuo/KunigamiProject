"""Per-account activity leases and resumable deletion, outside private workspaces."""
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import weakref


class DeletionPending(RuntimeError):
    pass


def _directory(root, uid):
    if root is None:
        from core.runtime import DATA_ROOT
        root = DATA_ROOT
    uid = int(uid)
    if uid <= 0:
        raise ValueError("Invalid account")
    return Path(root) / 'configs' / 'account-lifecycle' / str(uid)


def pending(uid, root=None):
    return (_directory(root, uid) / 'deleting').is_dir()


def _connect(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return sqlite3.connect(directory / 'activity.db', timeout=0, check_same_thread=False)


class ActivityLease:
    def __init__(self, db):
        # Standalone background threads also release their lease on context exit.
        self.close = weakref.finalize(self, db.close)


def activity(uid, root=None):
    directory = _directory(root, uid)
    if (directory / 'deleting').exists():
        raise DeletionPending()
    db = _connect(directory)
    try:
        # An explicit read transaction holds a shared OS lock until close().
        db.execute('BEGIN')
        db.execute('SELECT count(*) FROM sqlite_master').fetchone()
        if (directory / 'deleting').exists():
            raise DeletionPending()
    except BaseException as exc:
        db.close()
        if isinstance(exc, sqlite3.OperationalError) and (directory / 'deleting').exists():
            raise DeletionPending() from None
        raise
    return ActivityLease(db)


@contextmanager
def deletion(uid, root):
    directory = _directory(root, uid)
    marker = directory / 'deleting'
    marker.mkdir(parents=True, exist_ok=True, mode=0o700)
    with closing(_connect(directory)) as db:
        try:
            db.execute('BEGIN EXCLUSIVE')
        except sqlite3.OperationalError as exc:
            if exc.sqlite_errorcode in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise DeletionPending() from None
            raise
        yield marker


def step(marker, name, action, *args, **kwargs):
    checkpoint = marker / name
    if not checkpoint.exists():
        action(*args, **kwargs)
        checkpoint.touch(mode=0o600)


def request_activity(uid):
    from flask import g
    leases = g.setdefault('_account_leases', {})
    if uid not in leases:
        leases[uid] = activity(uid)


def install_response_hooks(app):
    from flask import g
    from werkzeug.wsgi import ClosingIterator

    @app.after_request
    def keep_stream_lease(response):
        if response.is_streamed and g.get('_account_leases'):
            leases = g.pop('_account_leases')
            response.response = ClosingIterator(response.response, [db.close for db in leases.values()])
        return response

    @app.teardown_request
    def release_leases(error):
        for db in g.pop('_account_leases', {}).values():
            db.close()
