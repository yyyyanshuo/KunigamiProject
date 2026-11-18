import sqlite3
from pathlib import Path

from flask import Flask

from blueprints import auth
from core.web_agent import finish_job, runtime_root
from tests.test_account_delete import _init_users_db, _init_forums_db


def test_retry_cleanup_and_late_agent(tmp_path, monkeypatch):
    config = tmp_path/'configs'
    config.mkdir()
    users = tmp_path/'users'
    workspace = users/'1'
    workspace.mkdir(parents=True)
    for key, value in dict(USERS_ROOT=users, USERS_DB=config/'users.db',
                           FORUMS_DB=config/'forums.db', SQUARE_DB=config/'square.db',
                           SUBSCRIPTIONS_FILE=config/'subscriptions.json',
                           DEVICE_ACCOUNTS_FILE=config/'devices.json').items():
        monkeypatch.setattr(auth, key, str(value))
    _init_users_db(auth.USERS_DB)
    _init_forums_db(auth.FORUMS_DB)
    with sqlite3.connect(auth.FORUMS_DB) as db:
        db.execute('CREATE TABLE forum_tasks(user_id INTEGER)')
        db.executemany('INSERT INTO forum_tasks VALUES(?)', [(1,), (2,)])
    app = Flask(__name__)
    app.secret_key = 'synthetic'
    app.register_blueprint(auth.auth_bp)
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user_id=1, logged_in=True)
    media = []
    monkeypatch.setattr('cos_utils.delete_user_objects', lambda uid: media.append(uid))
    remove = auth._safe_remove_user_workspace
    def fault(uid):
        raise OSError('injected interruption')
    monkeypatch.setattr(auth, '_safe_remove_user_workspace', fault)
    params = {'password': 'correct-pass', 'confirm_text': '注销账户'}
    assert client.post('/api/account/delete', json=params).status_code >= 500
    monkeypatch.setattr(auth, '_safe_remove_user_workspace', remove)
    assert client.post('/api/account/delete', json=params).status_code == 200
    finish_job(tmp_path, runtime_root(tmp_path), {'user_id': 1, 'char_id':'hero'})
    assert not workspace.exists()
    assert media == [1]
    with sqlite3.connect(auth.FORUMS_DB) as db:
        assert db.execute('SELECT user_id FROM forum_tasks').fetchall() == [(2,)]


def test_deletion_waits_for_work_and_blocks_late_writes(tmp_path):
    import pytest
    from core.account_lifecycle import DeletionPending, activity, deletion
    first = activity(7, tmp_path)
    second = activity(7, tmp_path)
    with pytest.raises(DeletionPending), deletion(7, tmp_path):
        pytest.fail('deleted while writes active')
    first.close()
    with pytest.raises(DeletionPending):
        activity(7, tmp_path)
    with pytest.raises(DeletionPending), deletion(7, tmp_path):
        pytest.fail('deleted while another writer was active')
    second.close()
    with deletion(7, tmp_path):
        independent = activity(8, tmp_path)
        independent.close()
    with pytest.raises(DeletionPending):
        activity(7, tmp_path)
