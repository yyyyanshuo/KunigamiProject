import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from flask import Flask

import blueprints.chat as chat
import app as application


def test_duplicate_requests_replay_one_reply(tmp_path, monkeypatch):
    db = tmp_path / 'chat.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT,role,content,timestamp)')
    app = Flask(__name__)
    app.register_blueprint(chat.chat_bp)
    monkeypatch.setattr(chat, 'get_paths', lambda *a, **kw: (str(db), str(tmp_path)))
    monkeypatch.setattr(chat, 'get_current_user_id', lambda: 7)
    monkeypatch.setattr(chat, '_get_characters_config_file', lambda: str(tmp_path/'missing'))
    monkeypatch.setattr(chat, '_live_agent_state', lambda *a: {})
    monkeypatch.setattr(application, '_memory_context_changed', lambda *a: False)
    monkeypatch.setattr(chat, 'build_messages_for_chat_v2', lambda *a, **kw: [])
    monkeypatch.setattr(chat, 'get_model_config', lambda *a, **kw: ('gemini','fake'))
    monkeypatch.setattr(chat, 'get_circuit_breaker_info', lambda: None)
    monkeypatch.setattr(chat, 'process_agent_actions', lambda *a, **kw: ('reply',None,None,[]))
    monkeypatch.setattr(application,'process_ai_media_tags', lambda text,*a,**kw:text)
    entered, release = threading.Event(), threading.Event()
    calls=[]
    def fake(*a, **kw):
        calls.append(True)
        entered.set()
        assert release.wait(8)
        return 'reply'
    monkeypatch.setattr(chat, 'call_gemini', fake)
    payload=dict(message='hello',request_id='0123456789abcdef')
    def send(route, data):
        return app.test_client().post('/api/demo/'+route,json=data)
    msg=send('messages',payload).json['user_id']
    assert send('messages',payload).json['user_id']==msg
    assert send('messages',dict(payload,message='other')).status_code==409
    body=dict(message='hello',user_message_id=msg)
    with ThreadPoolExecutor(1) as pool:
        pending=pool.submit(send,'chat_v2',body)
        assert entered.wait(8)
        try:
            assert send('chat_v2',body).json['error']=='generation_busy'
        finally:
            release.set()
        result=pending.result().json
    replay=send('chat_v2',body).json
    assert replay['replayed'] and replay['id']==result['id']
    assert len(calls)==1
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT role FROM messages ORDER BY id').fetchall()==[('user',),('assistant',)]


def test_uncertain_and_refused_attempts(tmp_path):
    from contextlib import closing
    from services import chat_generation as delivery
    path=tmp_path/'chat.db'
    assert delivery.begin_generation(path, 1, False) is None
    assert delivery.begin_generation(path, 1, True) == ('started', None)
    with closing(delivery.connect(path)) as db, db:
        delivery.finish(db, 1, 'failed')
    assert delivery.begin_generation(path, 1, False) == ('failed', None)
    assert delivery.begin_generation(path, 1, True) is None
