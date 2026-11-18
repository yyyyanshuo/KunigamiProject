import sqlite3
import threading
from datetime import datetime
import pytest
from services.character_state import character_state_lock


@pytest.fixture
def active_chat(tmp_path, monkeypatch):
    import app
    from services import voice_calls
    db = tmp_path / 'chat.db'
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY, role TEXT, content TEXT, timestamp TEXT)')
        c.execute("INSERT INTO messages VALUES (1,'assistant','old','2026-11-08 16:14:39')")
    monkeypatch.setattr(app, 'get_paths', lambda *a, **k: (str(db), str(tmp_path)))
    monkeypatch.setattr(app, 'set_background_user', lambda *a: None)
    monkeypatch.setattr(app, 'get_current_user_id', lambda: 1)
    monkeypatch.setattr(voice_calls, 'get_live_call', lambda *a: None)
    monkeypatch.setattr(app, 'beijing_now', lambda: datetime(2026, 11, 8, 17, 30))
    monkeypatch.setattr(app, 'get_character_local_now', lambda *a, **k: datetime(2026, 11, 8, 17, 30))
    monkeypatch.setattr(app, 'sync_memory_before_single_chat', lambda *a, **k: (True, None))
    monkeypatch.setattr(app, 'build_system_prompt_v2', lambda *a, **k: 'synthetic')
    monkeypatch.setattr(app, 'get_ai_language', lambda *a, **k: 'en')
    monkeypatch.setattr(app, 'get_model_config', lambda *a, **k: ('relay', 'test'))
    calls = []
    monkeypatch.setattr(app, 'call_openrouter', lambda *a, **k: calls.append('model') or 'hello')
    monkeypatch.setattr(app, 'get_circuit_breaker_info', lambda: None)
    monkeypatch.setattr(app, 'process_agent_actions', lambda *a: (a[1], None, None))
    monkeypatch.setattr(app, 'process_ai_media_tags', lambda text, *a, **k: text)
    monkeypatch.setattr(app, '_sticker_content_from_ai', lambda text: text)
    monkeypatch.setattr(app, 'send_push_notification', lambda **k: calls.append('push'))
    monkeypatch.setattr(app, 'send_email_notification', lambda *a, **k: calls.append('email'))
    monkeypatch.setattr(app, 'user_language', lambda *a: 'en')
    return app, db, calls


def test_normal_reply_in_progress_skips_proactive(active_chat):
    app, db, calls = active_chat
    results = []
    with character_state_lock(str(db) + '.generation'):
        thread = threading.Thread(target=lambda: results.append(app.trigger_active_chat('test', 1)))
        thread.start()
        thread.join(5)
        assert not thread.is_alive()
    assert results == [False]
    assert calls == []


@pytest.mark.parametrize('role', ['user', 'assistant'])
def test_recent_message_skips_proactive_even_after_lock_released(active_chat, role):
    app, db, calls = active_chat
    with sqlite3.connect(db) as c:
        c.execute('INSERT INTO messages VALUES (2,?,?,?)', (role, 'recent', '2026-11-08 17:27:12'))
    assert app.trigger_active_chat('test', 1) is False
    assert calls == []


def test_new_user_message_during_model_discards_proactive(active_chat, monkeypatch):
    app, db, calls = active_chat
    def model(*a, **k):
        with sqlite3.connect(db) as c:
            c.execute("INSERT INTO messages VALUES (2,'user','new','2026-11-08 17:30:01')")
        acquired = []

        def normal_reply():
            with character_state_lock(str(db) + '.generation', timeout=0):
                acquired.append(True)

        thread = threading.Thread(target=normal_reply)
        thread.start()
        thread.join(5)
        assert acquired == [True]
        return 'stale response'
    monkeypatch.setattr(app, 'call_openrouter', model)
    monkeypatch.setattr(app, 'process_agent_actions', lambda *a: pytest.fail('stale actions'))
    assert app.trigger_active_chat('test', 1) is False
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT count(*) FROM messages').fetchone()[0] == 2
    assert calls == []


def test_idle_proactive_still_delivers(active_chat):
    app, db, calls = active_chat
    assert app.trigger_active_chat('test', 1) is True
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT role,content FROM messages ORDER BY id DESC LIMIT 1').fetchone() == ('assistant', 'hello')
    assert calls == ['model', 'push', 'email']
