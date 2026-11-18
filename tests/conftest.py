import pytest
import os
import sys
import tempfile

def pytest_configure(config):
    # Collection imports application modules which can initialize databases.
    # Require offline runtime before collection, even for --collect-only.
    from pathlib import Path
    runtime = sys.modules.get("core.runtime")
    if runtime is not None and runtime.LOCAL_TEST:
        root = Path(runtime.DATA_ROOT).resolve()
        if len(root.parts) > 2 and root.is_relative_to(Path("/tmp")) and root.parts[2].startswith("saki-"):
            return
    raise pytest.UsageError("请在 Linux/WSL 使用 python scripts/check_core.py，从临时离线环境运行核心测试。")


@pytest.fixture(scope="session")
def project_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

@pytest.fixture(scope="session")
def app_client():
    """Create a Flask test client. Runs once per session."""
    from app import app
    app.config["TESTING"] = True
    app.config["SERVER_NAME"] = "localhost"
    with app.test_client() as client:
        with app.app_context():
            yield client

@pytest.fixture
def tmp_json_file():
    """Create a temporary JSON file for testing safe_save_json."""
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def accounts_client(tmp_path, monkeypatch):
    """Real app authentication against an isolated account database/workspace."""
    import sqlite3
    from werkzeug.security import generate_password_hash
    import app as app_module
    import core.config
    import core.context
    import core.localization
    from blueprints import auth

    database = str(tmp_path / "users.db")
    users_root = str(tmp_path / "users")
    for module in (core.config, core.context, auth):
        monkeypatch.setattr(module, "USERS_DB", database)
    for module in (core.config, auth, app_module):
        monkeypatch.setattr(module, "USERS_ROOT", users_root)
    monkeypatch.setattr(core.localization, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(auth, "DEVICE_ACCOUNTS_FILE", str(tmp_path / "devices.json"))
    monkeypatch.setenv("SAKI_ADMIN_USER_IDS", "1")
    core.context.init_users_db()
    with sqlite3.connect(database) as conn:
        conn.executemany(
            "INSERT INTO users (id, email, password_hash, display_name) VALUES (?, ?, ?, ?)",
            [(1, "admin@example.com", generate_password_hash("synthetic-admin-password"), "Admin"),
             (2, "existing@example.com", generate_password_hash("synthetic-existing-password"), "Existing")],
        )
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        with client.session_transaction() as sess:
            sess.update(user_id=1, auth_version=1, logged_in=True, pending_ui_language="zh-CN")
        yield client
