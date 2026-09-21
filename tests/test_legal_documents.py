import sqlite3
import uuid

from core.legal import init_legal_consents_table


def _clear_session(client):
    with client.session_transaction() as flask_session:
        flask_session.clear()


def test_legal_document_pages_are_public(app_client):
    _clear_session(app_client)

    terms = app_client.get("/terms")
    privacy = app_client.get("/privacy")

    assert terms.status_code == 200
    assert "用户协议" in terms.get_data(as_text=True)
    assert "版本 1.0" in terms.get_data(as_text=True)
    assert privacy.status_code == 200
    assert "隐私政策" in privacy.get_data(as_text=True)
    assert "第三方处理与数据传输" in privacy.get_data(as_text=True)


def test_register_page_has_explicit_legal_checkbox_and_links(app_client):
    _clear_session(app_client)
    response = app_client.get("/register")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'id="legal_accepted"' in html
    assert 'href="/terms"' in html
    assert 'href="/privacy"' in html


def test_register_rejects_missing_or_stale_legal_acceptance(app_client):
    missing = app_client.post(
        "/api/register",
        json={
            "email": "missing-legal@example.com",
            "password": "secret",
            "adult_confirmed": True,
        },
    )
    assert missing.status_code == 400
    assert "用户协议" in missing.get_json()["message"]

    stale = app_client.post(
        "/api/register",
        json={
            "email": "stale-legal@example.com",
            "password": "secret",
            "adult_confirmed": True,
            "legal_accepted": True,
            "terms_version": "0.9",
            "privacy_version": "1.0",
        },
    )
    assert stale.status_code == 400
    assert "版本已更新" in stale.get_json()["message"]


def test_successful_registration_records_both_current_documents(
    app_client, tmp_path, monkeypatch
):
    from blueprints import auth
    import core.config

    database_path = tmp_path / "users.db"
    conn = sqlite3.connect(database_path)
    conn.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT,
            created_at TEXT
        )
        """
    )
    init_legal_consents_table(conn)
    conn.commit()
    conn.close()

    monkeypatch.setattr(auth, "USERS_DB", str(database_path))
    monkeypatch.setattr(core.config, "USERS_DB", str(database_path))
    monkeypatch.setattr(auth, "init_user_workspace", lambda user_id: None)
    monkeypatch.setattr(
        auth,
        "_track_device_login",
        lambda user_id, email, display_name: "test-device",
    )

    _clear_session(app_client)
    email = f"legal-{uuid.uuid4().hex}@example.com"
    response = app_client.post(
        "/api/register",
        json={
            "email": email,
            "password": "secret-pass",
            "adult_confirmed": True,
            "legal_accepted": True,
            "terms_version": "1.0",
            "privacy_version": "1.0",
        },
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "success"

    status_response = app_client.get("/api/legal/status")
    assert status_response.status_code == 200
    assert status_response.get_json()["current"] is True

    conn = sqlite3.connect(database_path)
    rows = conn.execute(
        """
        SELECT document_type, document_version, accepted_at
        FROM legal_consents
        ORDER BY document_type
        """
    ).fetchall()
    conn.close()

    assert [(row[0], row[1]) for row in rows] == [
        ("privacy", "1.0"),
        ("terms", "1.0"),
    ]
    assert all(row[2] for row in rows)
    _clear_session(app_client)
