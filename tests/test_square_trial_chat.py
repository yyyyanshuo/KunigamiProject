import json
import sqlite3
from pathlib import Path

from flask import Flask

import blueprints.square as square
import core.utils as core_utils
from services.ai_client import AIResponseText


def _response(result):
    if isinstance(result, tuple):
        response, status = result[0], result[1]
    else:
        response, status = result, result.status_code
    return response.get_json(), status


def _setup_trial(tmp_path, monkeypatch):
    square_db = tmp_path / "square.db"
    users_db = tmp_path / "users.db"
    users_root = tmp_path / "users"
    monkeypatch.setattr(square, "SQUARE_DB", str(square_db))
    monkeypatch.setattr(square, "USERS_DB", str(users_db))
    monkeypatch.setattr(square, "USERS_ROOT", str(users_root))
    monkeypatch.setattr(core_utils, "USERS_ROOT", str(users_root))
    monkeypatch.setattr(square, "get_current_user_id", lambda: 2)
    monkeypatch.setattr(square, "get_current_username", lambda: "测试用户")
    monkeypatch.setattr(square, "get_user_credential", lambda *args, **kwargs: "admin-gemini-key")
    monkeypatch.setattr(square, "TRIAL_RATE_LIMIT_PER_MINUTE", 100)

    with sqlite3.connect(users_db) as conn:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
        conn.executemany(
            "INSERT INTO users (id, email) VALUES (?, ?)",
            [(1, "admin@example.com"), (2, "user@example.com")],
        )
    square.init_square_db()
    with sqlite3.connect(square_db) as conn:
        conn.execute(
            """
            INSERT INTO characters (
                id, name, avatar, age, base_persona, relationship_graph,
                author_email, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "trial_hero",
                "试聊角色",
                "/static/default_avatar.png",
                20,
                "沉稳但很关心用户。",
                json.dumps({"其他角色": {"role": "宿敌", "score": 2, "description": "互相竞争"}}, ensure_ascii=False),
                "admin@example.com",
                "2026-08-13T00:00:00",
            ),
        )
    return square_db, users_root


def _save_relationship(app):
    with app.test_request_context(
        "/api/square/character/trial_hero/trial/relationship",
        method="PUT",
        headers={"X-Requested-With": "XMLHttpRequest"},
        json={
            "relationship": {
                "role": "朋友",
                "score": 3,
                "description": "刚认识，但比较聊得来",
            }
        },
    ):
        body, status = _response(square.api_square_trial_relationship("trial_hero"))
    assert status == 200
    return body


def test_trial_prompt_uses_only_direct_user_relationship(tmp_path, monkeypatch):
    square_db, _ = _setup_trial(tmp_path, monkeypatch)
    app = Flask(__name__)
    _save_relationship(app)
    captured = {}

    def fake_call(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return AIResponseText("很高兴见到你。")

    monkeypatch.setattr(square, "call_gemini", fake_call)
    with app.test_request_context(
        "/api/square/character/trial_hero/trial/chat",
        method="POST",
        headers={"X-Requested-With": "XMLHttpRequest"},
        json={"message": "你好", "request_id": "request_0001"},
    ):
        body, status = _response(square.api_square_trial_chat("trial_hero"))

    assert status == 200
    assert body["turn"] == 1
    assert captured["kwargs"]["user_id"] == 1
    system_prompt = captured["messages"][0]["content"]
    assert "朋友" in system_prompt
    assert "刚认识，但比较聊得来" in system_prompt
    assert "其他角色" not in system_prompt
    assert "宿敌" not in system_prompt
    with sqlite3.connect(square_db) as conn:
        assert conn.execute("SELECT completed_turns FROM square_trial_sessions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM square_trial_messages").fetchone()[0] == 2


def test_trial_is_lifetime_limited_to_ten_completed_turns(tmp_path, monkeypatch):
    _setup_trial(tmp_path, monkeypatch)
    app = Flask(__name__)
    _save_relationship(app)
    calls = []

    def fake_call(messages, **kwargs):
        calls.append(messages)
        return AIResponseText(f"回复{len(calls)}")

    monkeypatch.setattr(square, "call_gemini", fake_call)
    for index in range(10):
        with app.test_request_context(
            "/api/square/character/trial_hero/trial/chat",
            method="POST",
            headers={"X-Requested-With": "XMLHttpRequest"},
            json={"message": f"消息{index + 1}", "request_id": f"request_{index:04d}"},
        ):
            body, status = _response(square.api_square_trial_chat("trial_hero"))
        assert status == 200
        assert body["turn"] == index + 1

    with app.test_request_context(
        "/api/square/character/trial_hero/trial/chat",
        method="POST",
        headers={"X-Requested-With": "XMLHttpRequest"},
        json={"message": "第十一条", "request_id": "request_9999"},
    ):
        body, status = _response(square.api_square_trial_chat("trial_hero"))
    assert status == 409
    assert body["error"] == "trial_limit_reached"
    assert len(calls) == 10
    assert len(calls[-1]) == 20  # system + 18 prior messages + current user message


def test_failed_generation_does_not_consume_turn(tmp_path, monkeypatch):
    square_db, _ = _setup_trial(tmp_path, monkeypatch)
    app = Flask(__name__)
    _save_relationship(app)
    monkeypatch.setattr(square, "call_gemini", lambda *args, **kwargs: "（系统提示：连接超时。）")
    with app.test_request_context(
        "/api/square/character/trial_hero/trial/chat",
        method="POST",
        headers={"X-Requested-With": "XMLHttpRequest"},
        json={"message": "你好", "request_id": "request_fail"},
    ):
        body, status = _response(square.api_square_trial_chat("trial_hero"))
    assert status == 503
    assert body["error"] == "generation_failed"
    with sqlite3.connect(square_db) as conn:
        assert conn.execute("SELECT completed_turns FROM square_trial_sessions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM square_trial_messages").fetchone()[0] == 0


def test_add_to_local_imports_relationship_and_trial_history(tmp_path, monkeypatch):
    _, users_root = _setup_trial(tmp_path, monkeypatch)
    app = Flask(__name__)
    _save_relationship(app)
    monkeypatch.setattr(square, "call_gemini", lambda *args, **kwargs: AIResponseText("你好，很高兴见到你。"))
    with app.test_request_context(
        "/api/square/character/trial_hero/trial/chat",
        method="POST",
        headers={"X-Requested-With": "XMLHttpRequest"},
        json={"message": "你好", "request_id": "request_import"},
    ):
        _, status = _response(square.api_square_trial_chat("trial_hero"))
    assert status == 200

    config_path = users_root / "2" / "configs" / "characters.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(square, "_get_characters_config_file", lambda: str(config_path))

    def fake_init_char_db(char_id):
        db_path, _ = square.get_paths(char_id, user_id=2)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    monkeypatch.setattr("app.init_char_db", fake_init_char_db)
    with app.test_request_context(
        "/api/square/add_to_local",
        method="POST",
        json={"id": "trial_hero", "import_trial_history": True},
    ):
        body, status = _response(square.api_square_add_to_local())
    assert status == 200
    assert body["imported_message_count"] == 2
    prompts = users_root / "2" / "characters" / body["local_id"] / "prompts"
    relationship = json.loads((prompts / "2_relationship.json").read_text(encoding="utf-8"))
    assert relationship["2"]["role"] == "朋友"
    assert relationship["其他角色"]["role"] == "宿敌"
    db_path, _ = square.get_paths(body["local_id"], user_id=2)
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT role, content FROM messages ORDER BY id").fetchall()
    assert rows == [("user", "你好"), ("assistant", "你好，很高兴见到你。")]
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config[body["local_id"]]["square_trial_history_imported"] is True


def test_trial_template_uses_chat_style_text_and_image_export():
    source = (
        Path(__file__).resolve().parents[1] / "templates" / "square" / "trial_chat.html"
    ).read_text(encoding="utf-8")
    assert "已选择 0 条" in source
    assert "生成记录" in source
    assert "文字版" in source
    assert "图片版" in source
    assert "复制图片" in source
    assert "下载图片" in source
    assert "KunigamiAiDisclosure" in source
    assert "由 Sakura樱语🌸 AI 生成" in source
    assert "https://kunigami-project-api.online" in source
    assert "hideCharIdentity=hide&&!isUser" in source
    assert "if(!hideCharIdentity)row.appendChild(avatar)" in source
    assert "if(!hideCharIdentity)stack.appendChild(name)" in source
    assert "转发" not in source
    assert "删除" not in source
