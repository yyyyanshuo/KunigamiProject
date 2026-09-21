import json
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from core.credentials import CredentialDecryptError, CredentialStore
from scripts.migrate_user_credentials import migrate_global_settings, migrate_user


def _write_settings(users_root: Path, user_id: int, data: dict) -> Path:
    path = users_root / str(user_id) / "configs" / "user_settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_encrypted_store_removes_legacy_plaintext(tmp_path):
    users_root = tmp_path / "users"
    settings_path = _write_settings(
        users_root,
        7,
        {"current_user_name": "Test", "gemini_api_key": "legacy-secret-1234"},
    )
    key = Fernet.generate_key().decode()
    store = CredentialStore(users_root=users_root, encryption_keys=key)

    assert store.status(7, "gemini") == {
        "configured": True,
        "last_four": "1234",
        "storage": "legacy",
    }
    assert store.get(7, "gemini") == "legacy-secret-1234"

    status = store.set(7, "gemini", "new-secret-5678")

    assert status["storage"] == "encrypted"
    assert status["last_four"] == "5678"
    assert store.get(7, "gemini") == "new-secret-5678"
    assert "gemini_api_key" not in json.loads(settings_path.read_text(encoding="utf-8"))
    credential_text = store.credential_path(7).read_text(encoding="utf-8")
    assert "new-secret-5678" not in credential_text
    assert "legacy-secret-1234" not in credential_text


def test_wrong_encryption_key_fails_closed(tmp_path):
    users_root = tmp_path / "users"
    first = CredentialStore(users_root=users_root, encryption_keys=Fernet.generate_key().decode())
    first.set(3, "openrouter", "sk-or-private")
    second = CredentialStore(users_root=users_root, encryption_keys=Fernet.generate_key().decode())

    with pytest.raises(CredentialDecryptError):
        second.get(3, "openrouter")


def test_delete_removes_encrypted_and_legacy_values(tmp_path):
    users_root = tmp_path / "users"
    settings_path = _write_settings(users_root, 5, {"openrouter_api_key": "legacy-value"})
    store = CredentialStore(users_root=users_root, encryption_keys=Fernet.generate_key().decode())
    store.set(5, "openrouter", "encrypted-value")

    store.delete(5, "openrouter")

    assert store.get(5, "openrouter") == ""
    assert "openrouter_api_key" not in json.loads(settings_path.read_text(encoding="utf-8"))


def test_migration_dry_run_does_not_change_files(tmp_path):
    users_root = tmp_path / "users"
    settings_path = _write_settings(users_root, 11, {"gemini_api_key": "dry-run-secret"})
    original = settings_path.read_bytes()
    store = CredentialStore(users_root=users_root, encryption_keys=Fernet.generate_key().decode())

    result = migrate_user(
        store,
        11,
        settings_path,
        apply_changes=False,
        users_root=users_root,
        backup_root=None,
    )

    assert result["migrated"] == ["gemini"]
    assert settings_path.read_bytes() == original
    assert not store.credential_path(11).exists()


def test_migration_apply_encrypts_and_creates_restricted_backup(tmp_path):
    users_root = tmp_path / "users"
    settings_path = _write_settings(
        users_root,
        12,
        {
            "gemini_api_key": "gem-secret",
            "openrouter_api_key": "router-secret",
            "password": "legacy-login-password",
        },
    )
    backup_root = tmp_path / "backups"
    store = CredentialStore(users_root=users_root, encryption_keys=Fernet.generate_key().decode())

    result = migrate_user(
        store,
        12,
        settings_path,
        apply_changes=True,
        users_root=users_root,
        backup_root=backup_root,
        password_hash_ready=True,
    )

    assert set(result["migrated"]) == {"gemini", "openrouter"}
    migrated_settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "gemini_api_key" not in migrated_settings
    assert "openrouter_api_key" not in migrated_settings
    assert "password" not in migrated_settings
    assert result["password_cleanup"] == "ready"
    assert store.get(12, "gemini", allow_legacy=False) == "gem-secret"
    assert store.get(12, "openrouter", allow_legacy=False) == "router-secret"
    backup = backup_root / "12" / "configs" / "user_settings.json"
    assert backup.exists()
    assert "gem-secret" in backup.read_text(encoding="utf-8")


def test_global_migration_requires_explicit_target_and_removes_source_plaintext(tmp_path):
    users_root = tmp_path / "users"
    global_settings = tmp_path / "configs" / "user_settings.json"
    global_settings.parent.mkdir(parents=True)
    global_settings.write_text(
        json.dumps({"gemini_api_key": "global-secret", "password": "legacy-password"}),
        encoding="utf-8",
    )
    backup_root = tmp_path / "backups"
    store = CredentialStore(users_root=users_root, encryption_keys=Fernet.generate_key().decode())

    result = migrate_global_settings(
        store,
        21,
        global_settings,
        apply_changes=True,
        backup_root=backup_root,
        password_hash_ready=True,
    )

    assert result["migrated"] == ["gemini"]
    assert store.get(21, "gemini", allow_legacy=False) == "global-secret"
    assert "gemini_api_key" not in json.loads(global_settings.read_text(encoding="utf-8"))
    assert "password" not in json.loads(global_settings.read_text(encoding="utf-8"))
    assert (backup_root / "_global" / "configs" / "user_settings.json").exists()


def test_migration_never_removes_password_without_database_hash(tmp_path):
    users_root = tmp_path / "users"
    settings_path = _write_settings(users_root, 30, {"password": "only-plaintext-copy"})
    store = CredentialStore(users_root=users_root, encryption_keys=Fernet.generate_key().decode())

    result = migrate_user(
        store,
        30,
        settings_path,
        apply_changes=True,
        users_root=users_root,
        backup_root=tmp_path / "backups",
        password_hash_ready=False,
    )

    assert result["password_cleanup"] == "blocked"
    assert json.loads(settings_path.read_text(encoding="utf-8"))["password"] == "only-plaintext-copy"
