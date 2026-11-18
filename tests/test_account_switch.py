"""Account switching survives responses from requests started before the switch."""
from http.cookies import SimpleCookie
from threading import Event, Thread

import pytest


def _login(client, account):
    credentials = {
        1: ('admin@example.com', 'synthetic-admin-password'),
        2: ('existing@example.com', 'synthetic-existing-password'),
    }
    username, password = credentials[account]
    response = client.post('/api/login', json={'username': username, 'password': password})
    assert response.status_code == 200


def _register_accounts(client):
    _login(client, 2)
    _login(client, 1)


def _current_account(client):
    response = client.get('/api/accounts/recent')
    assert response.status_code == 200
    return next(row['user_id'] for row in response.json if row['is_current'])


@pytest.mark.parametrize('with_memory_context', [False, True])
def test_late_read_response_cannot_undo_switch(accounts_client, monkeypatch, with_memory_context):
    from blueprints import auth
    app = accounts_client.application
    _register_accounts(accounts_client)
    cookie_name = app.config['SESSION_COOKIE_NAME']
    old_cookie = accounts_client.get_cookie(cookie_name).value
    entered, finish = Event(), Event()
    original = auth._get_recent_device_accounts
    responses = []

    def delayed_accounts(*args, **kwargs):
        if not entered.is_set():
            if with_memory_context:
                import app as app_module
                app_module._memory_context_changed(1, 'single:old-account')
            entered.set()
            assert finish.wait(10), 'Switch did not finish'
        return original(*args, **kwargs)

    monkeypatch.setattr(auth, '_get_recent_device_accounts', delayed_accounts)

    def old_request():
        with app.test_client(use_cookies=False) as old_client:
            responses.append(old_client.get('/api/accounts/recent', headers={
                'Cookie': f'{cookie_name}={old_cookie}',
            }))

    thread = Thread(target=old_request)
    thread.start()
    try:
        assert entered.wait(10)
        switched = accounts_client.post('/api/accounts/switch', json={'user_id': 2})
        assert switched.status_code == 200 and switched.json['user_id'] == 2
    finally:
        finish.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert responses and responses[0].status_code == 200
    # Apply late Set-Cookie headers like a browser, then verify actual identity.
    for header in responses[0].headers.getlist('Set-Cookie'):
        cookies = SimpleCookie()
        cookies.load(header)
        for name, value in cookies.items():
            accounts_client.set_cookie(name, value.value)
    assert _current_account(accounts_client) == 2
    assert not any(header.startswith(cookie_name + '=') for header in responses[0].headers.getlist('Set-Cookie'))


def test_round_trip_switch_clears_previous_account_context(accounts_client):
    _register_accounts(accounts_client)
    with accounts_client.session_transaction() as session:
        session['_last_memory_context'] = 'group:old-account'
        session['pending_ui_language'] = 'en'
    for account in (2, 1, 2, 1):
        response = accounts_client.post('/api/accounts/switch', json={'user_id': account})
        assert response.status_code == 200
        assert response.headers['Cache-Control'] == 'no-store'
        assert _current_account(accounts_client) == account
        with accounts_client.session_transaction() as session:
            assert session['user_id'] == account
            assert session['auth_version'] == 1
            assert session.permanent
            assert '_last_memory_context' not in session
            assert 'pending_ui_language' not in session


def test_switch_rejects_unregistered_account_and_keeps_identity(accounts_client):
    _login(accounts_client, 1)
    assert accounts_client.post('/api/accounts/switch', json={'user_id': 2}).status_code == 403
    assert _current_account(accounts_client) == 1


def test_deleted_target_does_not_break_current_session(accounts_client):
    import sqlite3
    import core.config
    _register_accounts(accounts_client)
    with sqlite3.connect(core.config.USERS_DB) as db:
        db.execute('DELETE FROM users WHERE id=2')
    assert accounts_client.post('/api/accounts/switch', json={'user_id': 2}).status_code == 404
    assert _current_account(accounts_client) == 1


def test_failed_session_setup_preserves_current_account(accounts_client, monkeypatch):
    from blueprints import auth
    _register_accounts(accounts_client)

    def fail(_):
        raise OSError('synthetic session setup failure')

    monkeypatch.setattr(auth, 'establish_authenticated_session', fail)
    assert accounts_client.post('/api/accounts/switch', json={'user_id': 2}).status_code == 500
    assert _current_account(accounts_client) == 1
