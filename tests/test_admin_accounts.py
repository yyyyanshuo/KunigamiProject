import json
import sqlite3
from pathlib import Path

import pytest
from werkzeug.security import check_password_hash

import core.config
from core.legal import get_user_legal_status
from core.model_settings import defaults


HEADERS = {"X-Requested-With": "XMLHttpRequest"}


def create(client, **data):
    return client.post("/api/admin/users", json={"email": "new@example.com", **data}, headers=HEADERS)


def test_creation_password_login_workspace_and_session(accounts_client, caplog):
    response = create(accounts_client, email="  New@Example.COM  ", display_name="  New user  ",
                      password="must-not-be-used", is_admin=True)
    assert response.status_code == 201
    assert response.headers["Cache-Control"] == "no-store"
    result = response.get_json()
    assert result["email"] == "new@example.com"
    assert result["display_name"] == "New user"
    password = result["initial_password"]
    assert len(password) == 24
    assert all(any(test(char) for char in password) for test in (
        str.islower, str.isupper, str.isdigit, lambda c: c in "!@#$%_-+"))
    assert password not in caplog.text
    with accounts_client.session_transaction() as sess:
        assert sess["user_id"] == 1 and sess["auth_version"] == 1
        assert password not in str(dict(sess))
    with sqlite3.connect(core.config.USERS_DB) as conn:
        row = conn.execute("SELECT password_hash, auth_version FROM users WHERE id=?", (result["user_id"],)).fetchone()
        assert row[0] != password and check_password_hash(row[0], password)
        assert row[1] == 1
    configs = Path(core.config.USERS_ROOT) / str(result["user_id"]) / "configs"
    assert json.loads((configs / "api_settings.json").read_text()) == defaults()
    assert (configs / "global_user_persona.md").read_text() == ""
    assert json.loads((configs / "user_settings.json").read_text()) == {}
    assert get_user_legal_status(result["user_id"], db_path=core.config.USERS_DB)["current"] is False
    assert accounts_client.post("/api/login", json={"username": result["email"], "password": password}).status_code == 200
    assert create(accounts_client, email="forbidden@example.com").status_code == 403
    assert accounts_client.post("/api/user/change_password", json={
        "current_password": password, "new_password": "new-synthetic-password",
    }, headers=HEADERS).status_code == 200
    assert accounts_client.post("/api/login", json={"username": result["email"], "password": password}).status_code == 401
    assert accounts_client.post("/api/login", json={"username": result["email"], "password": "new-synthetic-password"}).status_code == 200


def test_passwords_are_independent_and_name_defaults_to_email(accounts_client):
    first = create(accounts_client).get_json()
    second = create(accounts_client, email="second@example.com").get_json()
    assert first["initial_password"] != second["initial_password"]
    assert first["display_name"] == first["email"]


@pytest.mark.parametrize("state,expected", [("anonymous", 401), ("ordinary", 403), ("impersonation", 403), ("revoked", 401)])
def test_creation_requires_current_admin_session(accounts_client, state, expected):
    with accounts_client.session_transaction() as sess:
        if state == "anonymous":
            sess.clear()
        elif state == "ordinary":
            sess["user_id"] = 2
        elif state == "revoked":
            sess["auth_version"] = 0
        else:
            sess.update(user_id=2, impersonator_user_id=1, impersonated_user_id=2)
    assert create(accounts_client).status_code == expected
    with sqlite3.connect(core.config.USERS_DB) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2


def test_creation_rejects_cross_site_form_and_missing_header(accounts_client):
    assert accounts_client.post("/api/admin/users", json={"email": "new@example.com"}).status_code == 403
    assert accounts_client.post("/api/admin/users", data={"email": "new@example.com"}, headers=HEADERS).status_code == 400


@pytest.mark.parametrize("payload", [None, [], "email", {}, {"email": None}, {"email": []},
    {"email": "bad"}, {"email": "a@@example.com"}, {"email": "a b@example.com"},
    {"email": "a@.com"}, {"email": "a" * 250 + "@example.com"},
    {"email": "<script>@example.com"}, {"email": "user@<script>.com"},
    {"email": "valid@example.com", "display_name": 123},
    {"email": "valid@example.com", "display_name": "x" * 101}])
def test_validation_does_not_create_accounts(accounts_client, payload):
    assert accounts_client.post("/api/admin/users", json=payload, headers=HEADERS).status_code == 400
    with sqlite3.connect(core.config.USERS_DB) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2


def test_duplicate_email_does_not_reset_password(accounts_client):
    first = create(accounts_client).get_json()
    assert create(accounts_client, email=" NEW@EXAMPLE.COM ").status_code == 409
    with sqlite3.connect(core.config.USERS_DB) as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE id=?", (first["user_id"],)).fetchone()
        assert check_password_hash(row[0], first["initial_password"])
        conn.execute("UPDATE users SET email='NEW@EXAMPLE.COM' WHERE id=?", (first["user_id"],))
    assert create(accounts_client).status_code == 409


def test_failed_workspace_rolls_back_database_and_files(accounts_client, monkeypatch):
    from blueprints import admin
    original = admin.initialize_user_workspace
    def fail(root, user_id):
        original(root, user_id)
        raise OSError("synthetic initialization failure")
    monkeypatch.setattr(admin, "initialize_user_workspace", fail)
    assert create(accounts_client).status_code == 500
    with sqlite3.connect(core.config.USERS_DB) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2
    assert not (Path(core.config.USERS_ROOT) / "3").exists()
    monkeypatch.setattr(admin, "initialize_user_workspace", original)
    assert create(accounts_client).status_code == 201


def test_orphaned_workspace_is_not_reused_or_deleted(accounts_client):
    orphan = Path(core.config.USERS_ROOT) / "3"
    orphan.mkdir(parents=True)
    (orphan / "private.txt").write_text("private existing data")
    assert create(accounts_client).status_code == 500
    assert (orphan / "private.txt").read_text() == "private existing data"
    with sqlite3.connect(core.config.USERS_DB) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2


def test_login_language_belongs_to_new_user(accounts_client):
    created = create(accounts_client).get_json()
    with accounts_client.session_transaction() as sess:
        sess.clear()
    accounts_client.post("/api/ui/language", json={"user_language": "en"})
    assert accounts_client.post("/api/login", json={"username": created["email"], "password": created["initial_password"]}).status_code == 200
    settings = Path(core.config.USERS_ROOT) / str(created["user_id"]) / "configs/user_settings.json"
    assert json.loads(settings.read_text())["user_language"] == "en"


@pytest.mark.parametrize("language", ["zh-CN", "en", "ja"])
def test_creation_ui_and_public_pages(accounts_client, language):
    accounts_client.post("/api/ui/language", json={"user_language": language})
    response = accounts_client.get("/admin/dashboard")
    assert response.status_code == 200
    source = response.get_data(as_text=True)
    assert 'id="create-account-form"' in source
    assert source.index('id="create-account-form"') < source.index('id="dashboard-content"')
    with accounts_client.session_transaction() as sess:
        sess.clear()
    for path in ("/login", "/forgot_password", "/guide"):
        response = accounts_client.get(path)
        assert response.status_code == 200
        assert 'href="/register"' not in response.get_data(as_text=True)
