import importlib
import json
from pathlib import Path

import pytest

from core.model_settings import defaults
from core.user_workspace import initialize_user_workspace


@pytest.mark.parametrize("entrypoint", ["app", "blueprints.auth"])
def test_initializers_ignore_shared_files_and_generate_defaults(tmp_path, monkeypatch, entrypoint):
    module = importlib.import_module(entrypoint)
    shared = tmp_path / "configs"
    shared.mkdir()
    (shared / "global_user_persona.md").write_text("PRIVATE GLOBAL PERSONA")
    (shared / "api_settings.json").write_text('{"active_route":"custom"}')
    (shared / "user_settings.json").write_text('{"gemini_api_key":"synthetic-secret"}')
    (shared / "square.db").write_bytes(b"shared database")
    (shared / "device_accounts.json").write_text('{"other-account":1}')
    (shared / "private").mkdir()
    (shared / "private" / "note.txt").write_text("private note")
    before = {p.relative_to(shared): p.read_bytes() for p in shared.rglob("*") if p.is_file()}
    monkeypatch.setattr(module, "USERS_ROOT", str(tmp_path / "users"))
    monkeypatch.setattr(module, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(module, "LOCAL_TEST", False)

    module.init_user_workspace(1)

    user_root = tmp_path / "users" / "1"
    configs = user_root / "configs"
    assert {p.name for p in configs.iterdir()} == {
        "characters.json", "groups.json", "user_settings.json", "read_status.json",
        "api_settings.json", "global_user_persona.md",
    }
    for name in ("characters.json", "groups.json", "user_settings.json", "read_status.json"):
        assert json.loads((configs / name).read_text()) == {}
    assert json.loads((configs / "api_settings.json").read_text()) == defaults()
    assert (configs / "global_user_persona.md").read_text() == ""
    for name in ("characters", "groups", "logs"):
        assert (user_root / name).is_dir()
    assert before == {p.relative_to(shared): p.read_bytes() for p in shared.rglob("*") if p.is_file()}


def test_existing_migrated_user_files_are_never_overwritten(tmp_path):
    users = tmp_path / "users"
    configs = users / "1" / "configs"
    configs.mkdir(parents=True)
    # Deliberately include malformed and empty legacy files: this is not a repair job.
    original = {
        "api_settings.json": b'{"active_route":"custom","legacy":true}',
        "user_settings.json": b'{"user_language":"en","timezone":"Europe/London"}',
        "characters.json": b'{"existing-character":{}}',
        "groups.json": b"legacy malformed JSON",
        "read_status.json": b"",
        "global_user_persona.md": "Existing personal notes".encode(),
        "credentials.json": b"existing encrypted credentials",
    }
    for name, value in original.items():
        (configs / name).write_bytes(value)
    initialize_user_workspace(users, 1)
    initialize_user_workspace(users, 1)
    assert {p.name: p.read_bytes() for p in configs.iterdir()} == original


def test_fresh_site_and_accounts_need_no_template_directory(tmp_path):
    users = tmp_path / "users"
    initialize_user_workspace(users, 1)
    first = users / "1" / "configs"
    (first / "global_user_persona.md").write_text("Only for user one")
    (first / "user_settings.json").write_text('{"user_language":"zh-CN"}')
    initialize_user_workspace(users, 2)
    second = users / "2" / "configs"
    assert (second / "global_user_persona.md").read_text() == ""
    assert json.loads((second / "user_settings.json").read_text()) == {}
    assert not (tmp_path / "configs").exists()


@pytest.mark.parametrize("user_id", [0, -1, "../other", True])
def test_invalid_user_id_cannot_create_workspace(tmp_path, user_id):
    with pytest.raises(ValueError):
        initialize_user_workspace(tmp_path / "users", user_id)
    assert not (tmp_path / "users").exists()

