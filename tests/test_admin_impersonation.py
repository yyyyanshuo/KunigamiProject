import sqlite3
import time

import core.config
from flask import Flask
from blueprints.admin import admin_bp


def _make_app(tmp_path, monkeypatch):
    db_path = tmp_path / "users.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, display_name TEXT)")
    conn.executemany("INSERT INTO users (id, email, display_name) VALUES (?, ?, ?)", [(1, "admin@example.com", "Admin"), (7, "user@example.com", "User Seven")])
    conn.commit()
    conn.close()
    monkeypatch.setattr(core.config, "USERS_DB", str(db_path))
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="test-secret")
    app.register_blueprint(admin_bp)
    return app


def test_only_admin_can_start_impersonation(tmp_path, monkeypatch):
    client = _make_app(tmp_path, monkeypatch).test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = 7
    assert client.post("/api/admin/impersonate", json={"user_id": 7}).status_code == 403


def test_admin_can_impersonate_and_restore(tmp_path, monkeypatch):
    client = _make_app(tmp_path, monkeypatch).test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = 1
        sess["logged_in"] = True
    assert client.post("/api/admin/impersonate", json={"user_id": 7}).status_code == 200
    status = client.get("/api/admin/impersonation/status").get_json()
    assert status["active"] is True and status["user_id"] == 7
    assert client.post("/api/admin/impersonation/exit").status_code == 200
    with client.session_transaction() as sess:
        assert sess["user_id"] == 1
        assert "impersonator_user_id" not in sess


def test_missing_target_is_rejected(tmp_path, monkeypatch):
    client = _make_app(tmp_path, monkeypatch).test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = 1
    assert client.post("/api/admin/impersonate", json={"user_id": 999}).status_code == 404


def test_expired_impersonation_restores_admin(tmp_path, monkeypatch):
    client = _make_app(tmp_path, monkeypatch).test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = 7
        sess["impersonator_user_id"] = 1
        sess["impersonated_user_id"] = 7
        sess["impersonation_expires_at"] = time.time() - 1
    response = client.get("/api/admin/impersonation/status")
    assert response.status_code == 440
    with client.session_transaction() as sess:
        assert sess["user_id"] == 1
        assert sess["impersonation_expired"] is True
