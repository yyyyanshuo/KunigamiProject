import json
import sqlite3
from pathlib import Path

from flask import Flask

import blueprints.square as square
import core.utils as core_utils


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _setup_square(tmp_path, monkeypatch):
    users_root = tmp_path / "users"
    square_db = tmp_path / "square.db"
    users_db = tmp_path / "users.db"
    avatars_dir = tmp_path / "square_avatars"
    monkeypatch.setattr(square, "USERS_ROOT", str(users_root))
    monkeypatch.setattr(square, "SQUARE_DB", str(square_db))
    monkeypatch.setattr(square, "USERS_DB", str(users_db))
    monkeypatch.setattr(square, "SQUARE_AVATARS_DIR", str(avatars_dir))
    monkeypatch.setattr(core_utils, "USERS_ROOT", str(users_root))

    with sqlite3.connect(users_db) as conn:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
        conn.executemany(
            "INSERT INTO users (id, email) VALUES (?, ?)",
            [(1, "author@example.com"), (2, "other@example.com")],
        )
    square.init_square_db()

    config_path = users_root / "1" / "configs" / "characters.json"
    prompts_dir = users_root / "1" / "characters" / "hero" / "prompts"
    _write_json(
        config_path,
        {
            "hero": {
                "name": "本地新版角色",
                "avatar": "/char_assets/hero/avatar.png",
                "age": 22,
                "no_age_increase": True,
                "emotion": 5,
                "timezone": "Asia/Tokyo",
            }
        },
    )
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "1_base_persona.md").write_text("本地新版人设", encoding="utf-8")
    _write_json(
        prompts_dir / "2_relationship.json",
        {"朋友": {"role": "好友", "score": 4, "description": "并肩作战"}},
    )
    _write_json(prompts_dir / "6_memory_short.json", {"private": "绝不上传"})
    _write_json(prompts_dir / "7_schedule.json", {"private": "绝不上传"})
    return users_root, square_db, config_path


def _response(result):
    if isinstance(result, tuple):
        response, status = result[0], result[1]
    else:
        response, status = result, result.status_code
    return response.get_json(), status


def test_existing_square_schema_gains_local_source_columns(tmp_path, monkeypatch):
    square_db = tmp_path / "legacy_square.db"
    monkeypatch.setattr(square, "SQUARE_DB", str(square_db))
    monkeypatch.setattr(square, "SQUARE_AVATARS_DIR", str(tmp_path / "avatars"))
    with sqlite3.connect(square_db) as conn:
        conn.execute("CREATE TABLE characters (id TEXT PRIMARY KEY, name TEXT NOT NULL)")

    square.init_square_db()

    with sqlite3.connect(square_db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(characters)")}
    assert {"author_user_id", "source_character_id", "updated_at"} <= columns


def test_local_publish_preview_excludes_private_state(tmp_path, monkeypatch):
    _setup_square(tmp_path, monkeypatch)

    snapshot = square._load_local_publish_snapshot(1, "hero")

    assert snapshot["base_persona"] == "本地新版人设"
    assert snapshot["relationship_graph"]["朋友"]["role"] == "好友"
    assert "emotion" not in snapshot
    assert "timezone" not in snapshot
    assert "memory" not in snapshot
    assert "schedule" not in snapshot


def test_author_can_update_existing_square_from_local_without_losing_social_data(
    tmp_path, monkeypatch
):
    _, square_db, config_path = _setup_square(tmp_path, monkeypatch)
    with sqlite3.connect(square_db) as conn:
        conn.execute(
            """
            INSERT INTO characters (
                id, name, avatar, age, base_persona, relationship_graph,
                tags, ip, author_email, likes_count, favorites_count,
                comment_count, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "public_hero", "旧角色", "/static/old.png", 20, "旧人设", "{}",
                "旧标签", "旧IP", "author@example.com", 7, 5, 3, "2026-01-01",
            ),
        )
        conn.execute("INSERT INTO likes (user_id, character_id) VALUES (2, 'public_hero')")
        conn.execute("INSERT INTO favorites (user_id, character_id) VALUES (2, 'public_hero')")
        conn.execute(
            "INSERT INTO comments (character_id, content, created_at) VALUES ('public_hero', '保留评论', '2026-01-02')"
        )

    monkeypatch.setattr(square, "get_current_user_id", lambda: 1)
    monkeypatch.setattr(
        square,
        "_materialize_square_avatar",
        lambda *args, **kwargs: "/static/square_avatars/new.png",
    )
    app = Flask(__name__)
    with app.test_request_context(
        "/api/square/update",
        method="POST",
        data={
            "id": "public_hero",
            "source_character_id": "hero",
            "name": "本地新版角色",
            "age": "22",
            "no_age_increase": "true",
            "ip": "旧IP",
            "tags": "旧标签",
            "base_persona": "本地新版人设",
            "relationship_graph": json.dumps(
                {"朋友": {"role": "好友", "score": 4, "description": "并肩作战"}},
                ensure_ascii=False,
            ),
            "avatar_url": "/char_assets/hero/avatar.png",
        },
    ):
        body, status = _response(square.api_square_update())

    assert status == 200
    assert body["status"] == "success"
    with sqlite3.connect(square_db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM characters WHERE id = 'public_hero'").fetchone()
        assert row["name"] == "本地新版角色"
        assert row["source_character_id"] == "hero"
        assert row["author_user_id"] == 1
        assert row["likes_count"] == 7
        assert row["favorites_count"] == 5
        assert row["comment_count"] == 3
        assert row["created_at"] == "2026-01-01"
        assert conn.execute("SELECT COUNT(*) FROM likes WHERE character_id = 'public_hero'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM favorites WHERE character_id = 'public_hero'").fetchone()[0] == 1
        assert conn.execute("SELECT content FROM comments WHERE character_id = 'public_hero'").fetchone()[0] == "保留评论"

    local_config = json.loads(config_path.read_text(encoding="utf-8"))
    assert local_config["hero"]["square_published_id"] == "public_hero"
    assert local_config["hero"]["square_last_synced_at"]


def test_non_author_cannot_replace_square_from_local(tmp_path, monkeypatch):
    _, square_db, _ = _setup_square(tmp_path, monkeypatch)
    with sqlite3.connect(square_db) as conn:
        conn.execute(
            "INSERT INTO characters (id, name, author_email, author_user_id) VALUES (?, ?, ?, ?)",
            ("public_hero", "角色", "author@example.com", 1),
        )
    monkeypatch.setattr(square, "get_current_user_id", lambda: 2)
    app = Flask(__name__)
    with app.test_request_context(
        "/api/square/update",
        method="POST",
        data={"id": "public_hero", "name": "恶意覆盖"},
    ):
        body, status = _response(square.api_square_update())

    assert status == 403
    assert "无权" in body["error"]


def test_reuploading_linked_local_character_redirects_to_existing_publication(
    tmp_path, monkeypatch
):
    _, square_db, _ = _setup_square(tmp_path, monkeypatch)
    with sqlite3.connect(square_db) as conn:
        conn.execute(
            """
            INSERT INTO characters (
                id, name, author_email, author_user_id, source_character_id
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("public_hero", "角色", "author@example.com", 1, "hero"),
        )
    monkeypatch.setattr(square, "get_current_user_id", lambda: 1)
    app = Flask(__name__)
    with app.test_request_context(
        "/api/square/upload",
        method="POST",
        data={
            "id": "hero",
            "source_character_id": "hero",
            "name": "本地新版角色",
            "relationship_graph": "{}",
            "base_persona": "新版",
        },
    ):
        body, status = _response(square.api_square_upload())

    assert status == 409
    assert body == {
        "status": "already_published",
        "square_id": "public_hero",
        "message": "该本地角色已经发布，可更新原作品",
    }


def test_square_edit_template_keeps_public_id_when_importing_local_character():
    source = (
        Path(__file__).resolve().parents[1] / "templates" / "square" / "upload.html"
    ).read_text(encoding="utf-8")

    assert "从本地角色重新同步" in source
    assert "if (!isEdit) document.querySelector('[name=\"id\"]').value = data.id;" in source
    assert "formData.set('source_character_id', window.importedLocalCharacterId);" in source
    assert "document.getElementById('import-section').style.display = 'none'" not in source
