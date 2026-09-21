import json
import sqlite3

from flask import Flask
from werkzeug.security import generate_password_hash

import blueprints.auth as auth_module
from blueprints.auth import auth_bp

def _init_users_db(path):
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute('CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, display_name TEXT, created_at TEXT, provider TEXT, provider_user_id TEXT, is_frozen INTEGER DEFAULT 0)')
    cur.execute('CREATE TABLE circuit_breaker (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, route TEXT)')
    cur.execute("INSERT INTO users (id, email, password_hash, display_name) VALUES (?, ?, ?, ?)", (1, 'delete@example.com', generate_password_hash('correct-pass'), 'Delete Me'))
    cur.execute("INSERT INTO users (id, email, password_hash, display_name) VALUES (?, ?, ?, ?)", (2, 'keep@example.com', generate_password_hash('keep-pass'), 'Keep Me'))
    cur.execute("INSERT INTO circuit_breaker (user_id, route) VALUES (?, ?)", (1, '/api/test'))
    conn.commit()
    conn.close()

def _init_square_db(path):
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("CREATE TABLE characters (id TEXT PRIMARY KEY, author_email TEXT)")
    cur.execute("CREATE TABLE favorites (user_id INTEGER, character_id TEXT)")
    cur.execute("CREATE TABLE likes (user_id INTEGER, character_id TEXT)")
    cur.execute("INSERT INTO characters (id, author_email) VALUES (?, ?)", ('char_1', 'delete@example.com'))
    cur.execute("INSERT INTO favorites (user_id, character_id) VALUES (?, ?)", (1, 'char_1'))
    cur.execute("INSERT INTO likes (user_id, character_id) VALUES (?, ?)", (1, 'char_1'))
    cur.execute("INSERT INTO favorites (user_id, character_id) VALUES (?, ?)", (2, 'char_1'))
    conn.commit()
    conn.close()

def _init_forums_db(path):
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("CREATE TABLE forums (id TEXT PRIMARY KEY, user_id INTEGER, title TEXT)")
    cur.execute("INSERT INTO forums (id, user_id, title) VALUES (?, ?, ?)", ('forum_1', 1, 'mine'))
    cur.execute("INSERT INTO forums (id, user_id, title) VALUES (?, ?, ?)", ('forum_2', 2, 'other'))
    conn.commit()
    conn.close()

def test_delete_account_removes_private_data_and_anonymizes_square_posts(tmp_path, monkeypatch):
    configs_dir = tmp_path / 'configs'
    users_root = tmp_path / 'users'
    configs_dir.mkdir()
    users_root.mkdir()

    users_db = configs_dir / 'users.db'
    square_db = configs_dir / 'square.db'
    forums_db = configs_dir / 'forums.db'
    device_accounts_file = configs_dir / 'device_accounts.json'
    subscriptions_file = configs_dir / 'subscriptions.json'
    user_dir = users_root / '1'
    user_dir.mkdir()
    (user_dir / 'private.txt').write_text('secret', encoding='utf-8')

    _init_users_db(users_db)
    _init_square_db(square_db)
    _init_forums_db(forums_db)
    device_accounts_file.write_text(json.dumps({'device-a': {'1': {'email': 'delete@example.com'}, '2': {'email': 'keep@example.com'}}}), encoding='utf-8')
    subscriptions_file.write_text(json.dumps({'1': [{'endpoint': 'gone'}], '2': [{'endpoint': 'keep'}]}), encoding='utf-8')

    monkeypatch.setattr(auth_module, 'USERS_DB', str(users_db))
    monkeypatch.setattr(auth_module, 'USERS_ROOT', str(users_root))
    monkeypatch.setattr(auth_module, 'SQUARE_DB', str(square_db))
    monkeypatch.setattr(auth_module, 'FORUMS_DB', str(forums_db))
    monkeypatch.setattr(auth_module, 'DEVICE_ACCOUNTS_FILE', str(device_accounts_file))
    monkeypatch.setattr(auth_module, 'SUBSCRIPTIONS_FILE', str(subscriptions_file))

    app = Flask(__name__)
    app.secret_key = 'test-secret'
    app.register_blueprint(auth_bp)
    client = app.test_client()

    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['logged_in'] = True

    bad_resp = client.post('/api/account/delete', json={'password': 'wrong', 'confirm_text': '注销账户'})
    assert bad_resp.status_code == 403
    assert user_dir.exists()

    ok_resp = client.post('/api/account/delete', json={'password': 'correct-pass', 'confirm_text': '注销账户'})
    assert ok_resp.status_code == 200
    assert ok_resp.get_json()['status'] == 'success'

    conn = sqlite3.connect(users_db)
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM users WHERE id = 1')
    assert cur.fetchone()[0] == 0
    cur.execute('SELECT COUNT(*) FROM users WHERE id = 2')
    assert cur.fetchone()[0] == 1
    cur.execute('SELECT COUNT(*) FROM circuit_breaker WHERE user_id = 1')
    assert cur.fetchone()[0] == 0
    conn.close()

    assert not user_dir.exists()
    assert json.loads(device_accounts_file.read_text(encoding='utf-8')) == {'device-a': {'2': {'email': 'keep@example.com'}}}
    assert json.loads(subscriptions_file.read_text(encoding='utf-8')) == {'2': [{'endpoint': 'keep'}]}

    conn = sqlite3.connect(square_db)
    cur = conn.cursor()
    cur.execute("SELECT author_email FROM characters WHERE id = 'char_1'")
    assert cur.fetchone()[0] is None
    cur.execute('SELECT COUNT(*) FROM favorites WHERE user_id = 1')
    assert cur.fetchone()[0] == 0
    cur.execute('SELECT COUNT(*) FROM likes WHERE user_id = 1')
    assert cur.fetchone()[0] == 0
    cur.execute('SELECT COUNT(*) FROM favorites WHERE user_id = 2')
    assert cur.fetchone()[0] == 1
    conn.close()

    conn = sqlite3.connect(forums_db)
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM forums WHERE user_id = 1')
    assert cur.fetchone()[0] == 0
    cur.execute('SELECT COUNT(*) FROM forums WHERE user_id = 2')
    assert cur.fetchone()[0] == 1
    conn.close()

    with client.session_transaction() as sess:
        assert 'user_id' not in sess
        assert 'logged_in' not in sess
