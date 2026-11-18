"""Cloudflare errors must not pause other accounts or their other routes."""
import sqlite3
import core.context as context
import core.circuit_breaker as breaker
from services import model_provider as provider


def test_cloudflare_error_is_scoped_to_user_and_route(tmp_path, monkeypatch):
    database = str(tmp_path / 'users.db')
    monkeypatch.setattr(context, 'USERS_DB', database)
    monkeypatch.setattr(breaker, 'USERS_DB', database)
    context.init_users_db()
    with sqlite3.connect(database) as conn:
        conn.executemany('INSERT INTO users(id,email,password_hash) VALUES (?,?,?)', [(1,'one@tests.invalid','synthetic'),(2,'two@tests.invalid','synthetic')])
    monkeypatch.setattr(provider, 'LOCAL_TEST', False)
    monkeypatch.setattr(provider, 'connection', lambda uid: ('custom','openai_chat','https://relay.invalid/v1','synthetic-key'))
    monkeypatch.setattr(provider, '_usage', lambda *a, **kw: None)
    class Response:
        status_code = 403
        text = '<!DOCTYPE html><html>Cloudflare</html>'
        def __enter__(self): return self
        def __exit__(self, *a): pass
    calls = []
    def post(*args, **kwargs):
        calls.append(1)
        return Response()
    monkeypatch.setattr(provider.public_https, 'open_response', post)
    for _ in range(2):
        provider.call_provider([{'role':'user','content':'hello'}], model_name='test', user_id=1)
    assert breaker.check_circuit_breaker(1,'custom')['type'] == 'cooldown'
    assert breaker.check_circuit_breaker(2,'custom') is None
    assert breaker.check_circuit_breaker(1,'openai') is None
    provider.call_provider([{'role':'user','content':'hello'}], model_name='test', user_id=2)
    assert len(calls) == 3
