import app as app_module


def test_memory_context_change_is_kept_within_request():
    flask_app = app_module.app

    with flask_app.test_request_context("/"):
        assert app_module._memory_context_changed(1, "single:kaiser") is True
        assert app_module._memory_context_changed(1, "single:kaiser") is False
        assert app_module._memory_context_changed(1, "group:bayern") is True
        assert app_module._memory_context_changed(1, "group:bayern") is False


def test_context_cookie_is_signed_account_scoped_and_separate_from_login():
    from flask import session
    from http.cookies import SimpleCookie
    from core.session_security import save_memory_context_cookie
    flask_app = app_module.app
    with flask_app.test_request_context("/"):
        session.update(user_id=1, auth_version=1, logged_in=True)
        session.modified = False
        assert app_module._memory_context_changed(1, "single:hero") is True
        assert not session.modified
        response = save_memory_context_cookie(flask_app.response_class())
    cookies = SimpleCookie()
    cookies.load(response.headers['Set-Cookie'])
    name = flask_app.config['SESSION_COOKIE_NAME'] + '_memory_context'
    assert list(cookies) == [name]
    header = f"{name}={cookies[name].value}"
    with flask_app.test_request_context('/', headers={'Cookie': header}):
        assert app_module._memory_context_changed(1, 'single:hero') is False
        assert app_module._memory_context_changed(2, 'single:hero') is True
    with flask_app.test_request_context('/', headers={'Cookie': header + 'invalid'}):
        assert app_module._memory_context_changed(1, 'single:hero') is True
