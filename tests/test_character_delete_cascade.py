import json

from flask import Flask

import blueprints.chat as chat_module
from blueprints.chat import chat_bp


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_delete_character_cascades_moments_position_and_references(tmp_path, monkeypatch):
    config_dir = tmp_path / "configs"
    chars_dir = tmp_path / "characters"
    characters_file = config_dir / "characters.json"
    positions_file = config_dir / "character_positions.json"
    groups_file = config_dir / "groups.json"
    read_file = config_dir / "read_status.json"
    moments_file = config_dir / "moments_data.json"
    last_post_file = config_dir / "moments_last_post.json"

    _write(characters_file, {
        "remove": {"name": "要删除", "remark": "删除备注"},
        "keep": {"name": "保留角色"},
    })
    _write(positions_file, {
        "remove": {"location_id": "home", "x": 0, "y": 0},
        "keep": {"location_id": "home", "x": 0, "y": 0},
    })
    _write(groups_file, {
        "g1": {"members": ["remove", "keep", "user"]},
    })
    _write(read_file, {"remove": "2026-01-01", "keep": "2026-01-02"})
    _write(last_post_file, {"remove": "2026-01-01", "keep": "2026-01-02"})
    _write(moments_file, [
        {
            "char_id": "remove",
            "content": "delete my post",
            "likers": [],
            "comments": [],
        },
        {
            "char_id": "keep",
            "content": "keep my post",
            "likers": [
                {"liker_id": "remove", "timestamp": "2026-01-01 00:00:00"},
                {"liker_id": "user", "timestamp": "2026-01-01 00:00:00"},
            ],
            "comments": [
                {"commenter_id": "remove", "content": "gone"},
                {"commenter_id": "user", "content": "stay", "reply_to": "remove"},
                {"commenter_id": "user", "content": "remain"},
            ],
        },
    ])
    keep_prompts = chars_dir / "keep" / "prompts"
    _write(keep_prompts / "2_relationship.json", {
        "要删除": {"role": "朋友", "score": 3, "description": ""},
        "其他": {"role": "同学", "score": 2, "description": ""},
    })
    removed_dir = chars_dir / "remove"
    removed_dir.mkdir(parents=True)
    (removed_dir / "chat.db").write_text("placeholder", encoding="utf-8")

    monkeypatch.setattr(chat_module, "get_current_user_id", lambda: "u1")
    monkeypatch.setattr(
        chat_module,
        "_get_characters_config_file",
        lambda user_id=None: str(characters_file),
    )
    monkeypatch.setattr(
        chat_module,
        "_get_character_positions_file",
        lambda user_id=None: str(positions_file),
    )
    monkeypatch.setattr(
        chat_module,
        "_get_groups_config_file",
        lambda user_id=None: str(groups_file),
    )
    monkeypatch.setattr(chat_module, "_get_read_status_file", lambda: str(read_file))
    monkeypatch.setattr(
        chat_module,
        "get_paths",
        lambda char_id, user_id=None: (
            str(chars_dir / char_id / "chat.db"),
            str(chars_dir / char_id / "prompts"),
        ),
    )

    app = Flask(__name__)
    app.secret_key = "test"
    app.register_blueprint(chat_bp)
    client = app.test_client()

    response = client.delete("/api/character/remove/delete")

    assert response.status_code == 200
    assert response.get_json()["deleted"] == {
        "posts": 1,
        "likes": 1,
        "comments": 2,
        "positions": 1,
        "relationships": 1,
        "groups": 1,
    }
    assert set(_read(characters_file)) == {"keep"}
    assert set(_read(positions_file)) == {"keep"}
    assert _read(groups_file)["g1"]["members"] == ["keep", "user"]
    assert set(_read(read_file)) == {"keep"}
    assert set(_read(last_post_file)) == {"keep"}
    remaining_post = _read(moments_file)[0]
    assert remaining_post["char_id"] == "keep"
    assert [like["liker_id"] for like in remaining_post["likers"]] == ["user"]
    assert [comment["content"] for comment in remaining_post["comments"]] == ["remain"]
    assert set(_read(keep_prompts / "2_relationship.json")) == {"其他"}
    assert not removed_dir.exists()
