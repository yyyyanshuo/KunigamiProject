import json
from pathlib import Path

from flask import Flask

import blueprints.chat as chat


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _prepare_relationship_files(tmp_path, monkeypatch):
    config_file = tmp_path / "configs" / "characters.json"
    characters = {
        "hero": {"name": "主角", "remark": "英雄"},
        "isagi": {"name": "洁世一", "remark": "洁"},
        "bachira": {"name": "蜂乐回", "remark": "蜂乐"},
    }
    _write(config_file, characters)

    def fake_paths(char_id, user_id=None):
        root = tmp_path / "characters" / char_id
        prompts = root / "prompts"
        prompts.mkdir(parents=True, exist_ok=True)
        return str(root / "chat.db"), str(prompts)

    monkeypatch.setattr(chat, "get_current_user_id", lambda: 7)
    monkeypatch.setattr(
        chat,
        "_get_characters_config_file",
        lambda user_id=None: str(config_file),
    )
    monkeypatch.setattr(chat, "get_paths", fake_paths)

    hero_file = tmp_path / "characters" / "hero" / "prompts" / "2_relationship.json"
    isagi_file = tmp_path / "characters" / "isagi" / "prompts" / "2_relationship.json"
    bachira_file = tmp_path / "characters" / "bachira" / "prompts" / "2_relationship.json"
    _write(hero_file, {"洁世一": {"role": "队友", "score": 2, "description": "旧"}})
    _write(
        isagi_file,
        {
            "主角": {"role": "队友", "score": 1, "description": "旧看法"},
            "蜂乐回": {"role": "朋友", "score": 5, "description": "保留"},
        },
    )
    _write(bachira_file, {})
    return characters, hero_file, isagi_file, bachira_file


def test_reverse_json_parser_resolves_id_name_and_remark(tmp_path, monkeypatch):
    characters, *_ = _prepare_relationship_files(tmp_path, monkeypatch)
    result = chat._resolve_reverse_relationship_graph(
        "hero",
        {
            "isagi": {"role": "队友", "score": 4, "description": "A"},
            "蜂乐": {"role": "朋友", "score": 3, "description": "B"},
        },
        characters,
    )

    assert set(result) == {"isagi", "bachira"}
    assert result["bachira"]["description"] == "B"


def test_save_all_relationship_views_writes_separate_files(tmp_path, monkeypatch):
    _, hero_file, isagi_file, bachira_file = _prepare_relationship_files(tmp_path, monkeypatch)
    app = Flask(__name__)
    payload = {
        "normal": {
            "洁世一": {"role": "劲敌", "score": 4.5, "description": "我的新看法"}
        },
        "reverse": {
            "isagi": {"role": "队友", "score": 4, "description": "洁的新看法"},
            "bachira": {"role": "朋友", "score": 3, "description": "蜂乐的新看法"},
        },
        "deleted_reverse_ids": [],
    }

    with app.test_request_context(json=payload):
        response = chat.save_all_relationship_views("hero")
        assert response.get_json()["status"] == "success"

    hero = json.loads(hero_file.read_text(encoding="utf-8"))
    isagi = json.loads(isagi_file.read_text(encoding="utf-8"))
    bachira = json.loads(bachira_file.read_text(encoding="utf-8"))
    assert hero["洁世一"]["description"] == "我的新看法"
    assert "isagi" not in hero and "bachira" not in hero
    assert isagi["主角"]["description"] == "洁的新看法"
    assert isagi["蜂乐回"]["description"] == "保留"
    assert bachira["主角"]["description"] == "蜂乐的新看法"
    assert "char_name" not in bachira["主角"]


def test_save_all_relationship_views_deletes_missing_reverse_entry_only(tmp_path, monkeypatch):
    _, _, isagi_file, _ = _prepare_relationship_files(tmp_path, monkeypatch)
    app = Flask(__name__)
    payload = {
        "normal": {},
        "reverse": {},
        "deleted_reverse_ids": ["isagi"],
    }

    with app.test_request_context(json=payload):
        response = chat.save_all_relationship_views("hero")
        assert response.get_json()["status"] == "success"

    isagi = json.loads(isagi_file.read_text(encoding="utf-8"))
    assert "主角" not in isagi
    assert isagi["蜂乐回"]["description"] == "保留"


def test_save_all_relationship_views_rolls_back_on_late_failure(tmp_path, monkeypatch):
    _, hero_file, isagi_file, bachira_file = _prepare_relationship_files(tmp_path, monkeypatch)
    originals = {
        path: path.read_text(encoding="utf-8")
        for path in (hero_file, isagi_file, bachira_file)
    }
    real_write = chat._write_json_atomic
    call_count = 0

    def fail_third_write(path, value):
        nonlocal call_count
        call_count += 1
        if call_count == 3:
            raise OSError("simulated write failure")
        return real_write(path, value)

    monkeypatch.setattr(chat, "_write_json_atomic", fail_third_write)
    app = Flask(__name__)
    payload = {
        "normal": {"洁世一": {"role": "新", "score": 4, "description": "新"}},
        "reverse": {
            "isagi": {"role": "新", "score": 4, "description": "新"},
            "bachira": {"role": "新", "score": 4, "description": "新"},
        },
    }

    with app.test_request_context(json=payload):
        response, status = chat.save_all_relationship_views("hero")
        assert status == 500
        assert "simulated write failure" in response.get_json()["error"]

    for path, original in originals.items():
        assert json.loads(path.read_text(encoding="utf-8")) == json.loads(original)


def test_reverse_ai_generation_returns_preview_without_writing(tmp_path, monkeypatch):
    _, hero_file, isagi_file, _ = _prepare_relationship_files(tmp_path, monkeypatch)
    originals = {
        hero_file: hero_file.read_text(encoding="utf-8"),
        isagi_file: isagi_file.read_text(encoding="utf-8"),
    }
    monkeypatch.setattr(chat, "get_model_config", lambda *args, **kwargs: ("relay", "test-model"))
    monkeypatch.setattr(
        chat,
        "call_openrouter",
        lambda *args, **kwargs: json.dumps(
            {
                "isagi": {
                    "role": "队友",
                    "score": 4,
                    "description": "AI 生成的洁视角",
                }
            },
            ensure_ascii=False,
        ),
    )
    app = Flask(__name__)
    with app.test_request_context(
        json={"source_character_ids": ["isagi"], "current_relationships": {}}
    ):
        response = chat.ai_generate_relationship_reverse("hero")
        data = response.get_json()

    assert data["status"] == "success"
    assert data["graph"]["isagi"]["char_name"] == "洁世一"
    assert data["graph"]["isagi"]["description"] == "AI 生成的洁视角"
    for path, original in originals.items():
        assert path.read_text(encoding="utf-8") == original


def test_memory_template_uses_one_draft_and_one_save_request():
    source = (Path(__file__).resolve().parents[1] / "templates" / "memory.html").read_text(encoding="utf-8")
    assert "let relationDraft = null;" in source
    assert "captureCurrentRelationDraft();" in source
    assert "relationships/save_all" in source
    assert "relationship_reverse/parse" in source
    assert "relationship_reverse/ai_generate" in source
    assert "Promise.all(promiseList)" not in source
    assert "保存全部关系视角" in source


def test_memory_template_uses_complete_json_editor_without_merge_selector():
    source = (Path(__file__).resolve().parents[1] / "templates" / "memory.html").read_text(encoding="utf-8")

    assert "卡片编辑" in source
    assert "JSON 编辑" in source
    assert "完整编辑当前“我的视角”关系 JSON" in source
    assert "完整编辑“其他角色如何看我”的 JSON" in source
    assert "relationDraft.jsonText" in source
    assert "relationDraft.editorMode" in source
    assert "validateRelationJsonEditor" in source
    assert "JSON 格式有误，无法切换或保存" in source
    assert "relation-json-mode" not in source
    assert "合并现有关系" not in source
    assert "覆盖现有关系" not in source


def test_memory_template_has_copyable_ai_prompt_with_schema_and_current_json():
    source = (Path(__file__).resolve().parents[1] / "templates" / "memory.html").read_text(encoding="utf-8")

    assert "复制 AI Prompt" in source
    assert "function buildRelationshipPrompt" in source
    assert "function copyRelationshipPrompt" in source
    assert "role、score、description 三个字段" in source
    assert "顶层键必须使用下方允许列表中的角色 ID" in source
    assert "可生成关系的其他角色" in source
    assert "顶层键必须使用下方可生成角色列表中的“原名”" in source
    assert "只允许新增下方列表中的角色，不要虚构其他角色" in source
    assert "原名：${name}；ID：${contact.id}" in source
    assert "请在以下当前完整 JSON 基础上修改" in source
    assert "navigator.clipboard?.writeText" in source
    assert "function showToast(message)" in source
    assert "let copied = false" in source
    assert "if (copied)" in source
