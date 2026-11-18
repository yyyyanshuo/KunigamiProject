import sqlite3
import json

from core import utils
from core import user_resources


def test_ensure_group_chat_storage_repairs_missing_group_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(utils, "USERS_ROOT", str(tmp_path / "users"))
    monkeypatch.setattr(user_resources, "USERS_ROOT", str(tmp_path / "users"))
    catalog = tmp_path / 'users/7/configs/groups.json'
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps({'new_group': {'members': []}}))

    group_dir, db_path = utils.ensure_group_chat_storage("new_group", user_id=7)

    assert group_dir == str(tmp_path / "users" / "7" / "groups" / "new_group")
    conn = sqlite3.connect(db_path)
    try:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
        ).fetchone()
    finally:
        conn.close()
    assert table == ("messages",)
