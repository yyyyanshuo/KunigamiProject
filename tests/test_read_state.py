import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from flask import Flask, session

import blueprints.chat as chat
import services.read_state as reads
from services.memory_store import load_json_object


def make_db(path, messages):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT, content TEXT, timestamp TEXT)')
        conn.executemany('INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)', messages)
        conn.commit()
    return str(path)


def unread(db, state, kind='chat', cid='hero'):
    with closing(sqlite3.connect(db)) as conn:
        return reads.count_unread(conn.cursor(), state, kind, cid)


def test_message_ids_handle_future_backdated_and_same_second_messages(tmp_path):
    db = make_db(tmp_path / 'chat.db', [
        ('assistant', 'one', '2099-01-01 00:00:00'),
        ('assistant', 'two', '2099-01-01 00:00:00'),
        ('user', 'mine', '2026-09-05 00:00:00'),
    ])
    status = str(tmp_path / 'read.json')
    reads.mark_conversations_read(status, [('chat', 'hero', db, 1)])
    assert unread(db, load_json_object(status)) == 1
    reads.mark_conversations_read(status, [('chat', 'hero', db, 3)])
    assert unread(db, load_json_object(status)) == 0
    make_db(tmp_path / 'chat.db', [('assistant', 'later arrival', '2000-01-01 00:00:00')])
    assert unread(db, load_json_object(status)) == 1


def test_legacy_timestamp_stays_readable_until_explicit_receipt(tmp_path):
    db = make_db(tmp_path / 'chat.db', [
        ('assistant', 'old', '2026-09-01 00:00:00'),
        ('assistant', 'new', '2026-09-05 00:00:00'),
    ])
    state = {'hero': '2026-09-02 00:00:00'}
    assert unread(db, state) == 1
    status = tmp_path / 'read.json'
    status.write_text(json.dumps(state))
    reads.mark_conversations_read(str(status), [('chat', 'hero', db, 2)])
    assert unread(db, load_json_object(str(status))) == 0


def test_batch_snapshot_never_consumes_new_arrivals_or_retreats(tmp_path):
    db = make_db(tmp_path / 'chat.db', [('assistant', str(i), '2026-09-05 00:00:00') for i in range(5)])
    status = str(tmp_path / 'read.json')
    reads.mark_conversations_read(status, [('chat', 'hero', db, 4), ('group', 'hero', db, 2)])
    reads.mark_conversations_read(status, [('chat', 'hero', db, 1)])
    assert unread(db, load_json_object(status)) == 1
    assert unread(db, load_json_object(status), 'group') == 3
    reads.remove_read_state(status, 'chat', 'hero')
    assert load_json_object(status)[reads.CURSORS_KEY]['group']['hero'] == 2


def test_concurrent_receipts_merge_without_lost_updates(tmp_path):
    db = make_db(tmp_path / 'chat.db', [('assistant', str(i), '2026-09-05 00:00:00') for i in range(12)])
    status = str(tmp_path / 'read.json')
    targets = [('chat', 'hero', db, i) for i in range(1, 13)] + [('group', f'g{i}', db, i) for i in range(1, 13)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda target: reads.mark_conversations_read(status, [target]), targets))
    data = load_json_object(status)[reads.CURSORS_KEY]
    assert data['chat']['hero'] == 12
    assert data['group'] == {f'g{i}': i for i in range(1, 13)}


@pytest.mark.parametrize('bad_id', [-1, True, '2', 1.5])
def test_bad_batch_does_not_partially_save(tmp_path, bad_id):
    status = str(tmp_path / 'read.json')
    db = make_db(tmp_path / 'chat.db', [('assistant', 'x', '2026-09-05 00:00:00')])
    with pytest.raises(ValueError):
        reads.mark_conversations_read(status, [('chat', 'hero', db, 1), ('group', 'g', db, bad_id)])
    assert not (tmp_path / 'read.json').exists()


def test_corrupt_or_failed_storage_does_not_report_success(tmp_path, monkeypatch):
    status = tmp_path / 'read.json'
    status.write_text('broken json')
    with pytest.raises(Exception):
        reads.mark_conversations_read(str(status), [('chat', 'hero', str(tmp_path / 'none.db'), 0)])
    assert status.read_text() == 'broken json'
    status.write_text('{}')
    monkeypatch.setattr(reads, 'atomic_write_json', lambda *args: (_ for _ in ()).throw(OSError('failed')))
    with pytest.raises(OSError):
        reads.mark_conversations_read(str(status), [('chat', 'hero', str(tmp_path / 'none.db'), 0)])
    assert status.read_text() == '{}'


@pytest.fixture
def read_client(tmp_path, monkeypatch):
    import app as main
    paths = {}
    for uid in ('alice', 'bob'):
        root = tmp_path / uid
        root.mkdir()
        (root / 'characters.json').write_text(json.dumps({'hero': {'name': 'hero'}}))
        (root / 'groups.json').write_text(json.dumps({'hero': {'name': 'group hero'}}))
        for kind in ('chat', 'group'):
            make_db(root / kind / 'hero' / 'chat.db', [('assistant', str(i), '2026-09-05 00:00:00') for i in range(3)])
        paths[uid] = root

    def root(): return paths[session.get('user_id', 'alice')]
    for module in (chat, main):
        monkeypatch.setattr(module, 'get_current_user_id', lambda: session.get('user_id'))
        monkeypatch.setattr(module, '_get_read_status_file', lambda: str(root() / 'read.json'))
        monkeypatch.setattr(module, '_get_characters_config_file', lambda: str(root() / 'characters.json'))
        monkeypatch.setattr(module, '_get_groups_config_file', lambda: str(root() / 'groups.json'))
        monkeypatch.setattr(module, 'get_paths', lambda cid: (str(root() / 'chat' / cid / 'chat.db'), ''))
        monkeypatch.setattr(module, 'get_group_dir', lambda cid: str(root() / 'group' / cid))
    app = Flask(__name__)
    app.secret_key = 'test'
    app.register_blueprint(chat.chat_bp)
    app.add_url_rule('/api/contacts', view_func=main.get_contacts)
    client = app.test_client()
    with client.session_transaction() as state: state['user_id'] = 'alice'
    return client, paths


def test_end_to_end_contacts_single_group_batch_and_user_isolation(read_client):
    client, paths = read_client
    data = client.get('/api/contacts').get_json()
    assert len(data) == 2
    assert all(c['unread'] == 3 and c['last_message_id'] == 3 for c in data)
    response = client.post('/api/hero/mark_read', json={'type': 'group', 'last_message_id': 2})
    assert response.status_code == 200
    data = {c['type']: c for c in client.get('/api/contacts').get_json()}
    assert data['group']['unread'] == 1
    assert data['chat']['unread'] == 3
    snapshots = [{'type': c['type'], 'id': c['id'], 'last_message_id': c['last_message_id']} for c in data.values()]
    make_db(paths['alice'] / 'group' / 'hero' / 'chat.db', [('assistant', 'concurrent', '2026-09-05 00:00:00')])
    assert client.post('/api/contacts/mark_read', json={'conversations': snapshots}).status_code == 200
    data = {c['type']: c for c in client.get('/api/contacts').get_json()}
    assert data['group']['unread'] == 1
    assert data['chat']['unread'] == 0
    with client.session_transaction() as state: state['user_id'] = 'bob'
    assert all(c['unread'] == 3 for c in client.get('/api/contacts').get_json())


def test_read_api_auth_validation_and_write_failure(read_client, monkeypatch):
    client, paths = read_client
    assert client.post('/api/hero/mark_read', json=[]).status_code == 400
    assert client.post('/api/hero/mark_read', data='broken', content_type='application/json').status_code == 400
    assert client.post('/api/hero/mark_read', json={'type': 'group', 'last_message_id': True}).status_code == 400
    assert client.post('/api/unknown/mark_read', json={}).status_code == 400
    assert client.post('/api/contacts/mark_read', json={'conversations': [{'id': 'hero'}]}).status_code == 400
    assert not (paths['alice'] / 'read.json').exists()
    monkeypatch.setattr(reads, 'atomic_write_json', lambda *args: (_ for _ in ()).throw(OSError('failed')))
    assert client.post('/api/hero/mark_read', json={'last_message_id': 3}).status_code == 500
    with client.session_transaction() as state: state.clear()
    assert client.post('/api/hero/mark_read', json={}).status_code == 401
    assert client.post('/api/contacts/mark_read', json={}).status_code == 401


def test_deleted_group_cleans_only_its_read_cursor(read_client, monkeypatch):
    import blueprints.group as group
    client, paths = read_client
    assert client.post('/api/contacts/mark_read', json={'conversations': [
        {'type': 'group', 'id': 'hero', 'last_message_id': 3},
        {'type': 'chat', 'id': 'hero', 'last_message_id': 2},
    ]}).status_code == 200
    root = paths['alice']
    monkeypatch.setattr(group, '_get_groups_config_file', lambda: str(root / 'groups.json'))
    monkeypatch.setattr(group, '_get_read_status_file', lambda: str(root / 'read.json'))
    monkeypatch.setattr(group, 'get_group_dir', lambda cid: str(root / 'group' / cid))
    with client.application.test_request_context(method='DELETE'):
        response = group.delete_group_api('hero')
    assert response.status_code == 200
    state = load_json_object(str(root / 'read.json'))[reads.CURSORS_KEY]
    assert 'hero' not in state['group']
    assert state['chat']['hero'] == 2


def test_legacy_empty_body_caller_uses_group_when_no_character_matches(read_client):
    client, paths = read_client
    (paths['alice'] / 'characters.json').write_text('{}')
    assert client.post('/api/hero/mark_read').status_code == 200
    assert client.get('/api/contacts').get_json()[0]['unread'] == 0
