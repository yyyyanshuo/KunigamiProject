"""Single/group date navigation, timezone boundaries, and adjacent history pages."""
import sqlite3
import sys
from datetime import timedelta
from types import SimpleNamespace

import pytest
from flask import Flask, session

from core.time_utils import parse_beijing_timestamp
from services.chat_history import history_date_bounds


@pytest.fixture(params=['chat', 'group'])
def history_client(request, tmp_path, monkeypatch):
    from blueprints import chat, group
    from core import utils

    module = chat if request.param == 'chat' else group
    db_path = tmp_path / 'chat.db'
    with sqlite3.connect(db_path) as conn:
        conn.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, role TEXT, content TEXT, timestamp TEXT)')
    monkeypatch.setattr(chat, 'get_current_user_id', lambda: session.get('user_id'))
    monkeypatch.setattr(chat, 'get_paths', lambda _, user_id=None: (
        str(db_path if user_id == 1 else tmp_path / 'other.db'), str(tmp_path)))
    monkeypatch.setattr(group, 'get_group_dir', lambda _: str(tmp_path if session.get('user_id') == 1 else tmp_path / 'other'))
    monkeypatch.setattr(module, '_add_furigana_to_japanese', lambda content: content)
    settings = {'timezone': 'Asia/Tokyo'}
    monkeypatch.setattr(utils, '_load_user_settings', lambda: settings)
    monkeypatch.setitem(sys.modules, 'app', SimpleNamespace(
        init_char_db=lambda _: None, _sticker_content_from_ai=lambda text: text, get_ai_language=lambda: 'en'))
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY='date-navigation-test')
    app.register_blueprint(module.chat_bp if request.param == 'chat' else module.group_bp)
    with app.test_client() as client:
        with client.session_transaction() as state:
            state['user_id'] = 1
        prefix = '/api/demo' if request.param == 'chat' else '/api/group/demo'
        yield client, prefix, db_path, settings


def seed(db_path, timestamps):
    with sqlite3.connect(db_path) as conn:
        conn.executemany('INSERT INTO messages VALUES (?, ?, ?, ?)', [
            (i, 'user' if i % 2 else 'assistant', f'message {i}', ts)
            for i, ts in enumerate(timestamps, 1)
        ])


def test_date_returns_first_message_with_context_and_bidirectional_pages(history_client):
    client, prefix, db, _ = history_client
    seed(db, ['2026-10-29 22:00:00'] * 9 + ['2026-10-29 23:00:00'] * 20 + ['2026-10-30 23:00:00'] * 20)
    data = client.get(prefix + '/history?target_date=2026-10-30').get_json()
    assert data['target_id'] == 10  # midnight in the display zone, not storage zone
    assert [m['id'] for m in data['messages']] == list(range(5, 16))
    assert data['has_before'] and data['has_after']
    assert data['total'] == 49
    assert all(m['raw_content'] == m['content'] for m in data['messages'])
    before = client.get(prefix + '/history?before_id=5').get_json()
    after = client.get(prefix + '/history?after_id=15&limit=20').get_json()
    assert [m['id'] for m in before['messages']] == [1, 2, 3, 4]
    assert not before['has_before']
    assert [m['id'] for m in after['messages']] == list(range(16, 36))
    assert after['has_after']
    latest = client.get(prefix + '/history').get_json()
    assert latest['messages'][-1]['id'] == 49 and not latest['has_after']


def test_day_end_is_exclusive_and_missing_day_preserves_empty_result(history_client):
    client, prefix, db, _ = history_client
    seed(db, ['2026-10-29 22:59:59', '2026-10-30 23:00:00'])
    data = client.get(prefix + '/history?target_date=2026-10-30').get_json()
    assert data['messages'] == [] and data['target_id'] is None
    assert data['total'] == 2
    assert not data['has_before'] and not data['has_after']
    for day in ['2020-01-01', '2099-12-31']:
        assert client.get(prefix + '/history?target_date=' + day).get_json()['messages'] == []


def test_empty_conversation(history_client):
    client, prefix, _, _ = history_client
    data = client.get(prefix + '/history?target_date=2026-10-30').get_json()
    assert data == {'messages': [], 'total': 0, 'target_id': None, 'has_before': False, 'has_after': False}


@pytest.mark.parametrize('value', ['', '2026-02-30', '20261030', '2026-1-1', 'bad', '9999-12-31', '2026-10-30T00:00:00'])
def test_invalid_dates_are_rejected(history_client, value):
    client, prefix, _, _ = history_client
    response = client.get(prefix + '/history', query_string={'target_date': value})
    assert response.status_code == 400
    assert response.get_json()['error'] == 'invalid_target_date'


@pytest.mark.parametrize('key', ['target_id', 'before_id', 'after_id'])
def test_conflicting_cursors_are_rejected(history_client, key):
    client, prefix, _, _ = history_client
    assert client.get(prefix + '/history', query_string={'target_date': '2026-10-30', key: 1}).status_code == 400


@pytest.mark.parametrize('day,hours', [('2026-03-08', 23), ('2026-11-01', 25)])
def test_dst_day_selects_messages_inside_actual_user_day(history_client, day, hours):
    client, prefix, db, settings = history_client
    settings['timezone'] = 'America/Los_Angeles'
    start, end = history_date_bounds(day, settings['timezone'])
    left, right = parse_beijing_timestamp(start), parse_beijing_timestamp(end)
    assert right - left == timedelta(hours=hours)
    seed(db, [(left - timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S'), start, end])
    data = client.get(prefix + '/history', query_string={'target_date': day}).get_json()
    assert data['target_id'] == 2


def test_user_scoped_storage_is_retained(history_client):
    client, prefix, db, _ = history_client
    seed(db, ['2026-10-30 12:00:00'])
    with sqlite3.connect(db.parent / 'other.db') as conn:
        conn.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, role TEXT, content TEXT, timestamp TEXT)')
    with client.session_transaction() as state:
        state['user_id'] = 2
    data = client.get(prefix + '/history?target_date=2026-10-30').get_json()
    assert data['messages'] == [] and data['total'] == 0
