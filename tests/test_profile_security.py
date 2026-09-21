import json
import sqlite3

from flask import Flask
from werkzeug.security import check_password_hash, generate_password_hash


def test_profile_get_never_returns_plaintext_credentials(monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "get_current_user_id", lambda: 42)
    monkeypatch.setattr(
        app_module,
        "_load_user_settings",
        lambda: {
            "current_user_name": "Tester",
            "gemini_api_key": "must-not-leak-gemini",
            "openrouter_api_key": "must-not-leak-router",
        },
    )
    monkeypatch.setattr(
        app_module,
        "get_credential_statuses",
        lambda *_args, **_kwargs: {
            "gemini": {"configured": True, "last_four": "mini", "storage": "legacy"},
            "openrouter": {"configured": True, "last_four": "uter", "storage": "encrypted"},
            "elevenlabs": {"configured": True, "last_four": "labs", "storage": "encrypted"},
        },
    )

    with app_module.app.test_request_context("/api/user/profile_settings"):
        response = app_module.user_profile_settings()
        body = response.get_data(as_text=True)

    assert "must-not-leak" not in body
    assert "gemini_api_key" not in body
    assert "openrouter_api_key" not in body
    assert "elevenlabs_api_key" not in body
    assert '"last_four":"labs"' in body
    assert response.headers["Cache-Control"] == "no-store"


def test_username_falls_back_to_database_display_name(tmp_path, monkeypatch):
    import core.utils as utils_module

    users_db = tmp_path / "users.db"
    conn = sqlite3.connect(users_db)
    conn.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, display_name TEXT)"
    )
    conn.execute(
        "INSERT INTO users (id, email, display_name) VALUES (42, 'tester@example.com', '樱')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(utils_module, "USERS_DB", str(users_db))
    monkeypatch.setattr(utils_module, "get_current_user_id", lambda: 42)
    monkeypatch.setattr(utils_module, "_load_user_settings", lambda: {})

    assert utils_module.get_current_username() == "樱"


def test_credential_write_requires_custom_request_header(monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "get_current_user_id", lambda: 42)
    with app_module.app.test_request_context(
        "/api/user/credentials/gemini",
        method="PUT",
        json={"api_key": "secret"},
    ):
        response, status = app_module.user_credentials("gemini")
    assert status == 403
    assert response.get_json()["status"] == "error"


def test_password_change_hashes_password_and_revokes_session(tmp_path, monkeypatch):
    import blueprints.auth as auth_module

    users_db = tmp_path / "users.db"
    users_root = tmp_path / "users"
    conn = sqlite3.connect(users_db)
    conn.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, password_hash TEXT, auth_version INTEGER DEFAULT 1)"
    )
    conn.execute(
        "INSERT INTO users (id, password_hash, auth_version) VALUES (1, ?, 1)",
        (generate_password_hash("old-password"),),
    )
    conn.commit()
    conn.close()
    settings_path = users_root / "1" / "configs" / "user_settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"password": "old-password", "name": "Tester"}), encoding="utf-8")

    monkeypatch.setattr(auth_module, "USERS_DB", str(users_db))
    monkeypatch.setattr(auth_module, "USERS_ROOT", str(users_root))
    test_app = Flask(__name__)
    test_app.config.update(TESTING=True, SECRET_KEY="test-secret")
    test_app.register_blueprint(auth_module.auth_bp)
    client = test_app.test_client()
    with client.session_transaction() as current_session:
        current_session["user_id"] = 1
        current_session["logged_in"] = True
        current_session["auth_version"] = 1

    response = client.post(
        "/api/user/change_password",
        json={"current_password": "old-password", "new_password": "new-password"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 200
    conn = sqlite3.connect(users_db)
    password_hash, auth_version = conn.execute(
        "SELECT password_hash, auth_version FROM users WHERE id = 1"
    ).fetchone()
    conn.close()
    assert check_password_hash(password_hash, "new-password")
    assert auth_version == 2
    assert "password" not in json.loads(settings_path.read_text(encoding="utf-8"))
    with client.session_transaction() as current_session:
        assert "user_id" not in current_session
