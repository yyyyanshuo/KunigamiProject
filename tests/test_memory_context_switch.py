import app as app_module


def test_memory_context_change_is_kept_in_flask_session():
    flask_app = app_module.app

    with flask_app.test_request_context("/"):
        assert app_module._memory_context_changed(1, "single:kaiser") is True
        assert app_module._memory_context_changed(1, "single:kaiser") is False
        assert app_module._memory_context_changed(1, "group:bayern") is True
        assert app_module._memory_context_changed(1, "group:bayern") is False
