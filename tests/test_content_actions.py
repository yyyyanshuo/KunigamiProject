import json
from unittest.mock import patch

import pytest

from agent_utils import process_agent_actions
from core.content_action_rules import get_content_action_rules

from services.content_actions import (
    ContentActionError,
    apply_persona_action,
    apply_plan_action,
    apply_relation_action,
    extract_content_actions,
)


def test_nested_json_action_parser_preserves_order_and_cleans_tags():
    source = (
        '正文\n[EDIT_PLAN: {"from":{"content":"以后去海边"},'
        '"set":{"date":"2026-09-10","content":"秋天去海边"}}]\n'
        '[REWRITE_RELATION: ["用户","恋人",5,"完全信任"]]'
    )
    actions, cleaned = extract_content_actions(source)

    assert [action.name for action in actions] == ["EDIT_PLAN", "REWRITE_RELATION"]
    assert actions[0].payload["set"]["date"] == "2026-09-10"
    assert cleaned.strip() == "正文"


def test_all_localized_prompts_publish_the_complete_action_contract():
    for lang in ("zh", "ja", "en"):
        rules = get_content_action_rules(lang)
        for name in (
            "ADD_PERSONA", "DELETE_PERSONA", "EDIT_PERSONA", "REWRITE_PERSONA",
            "ADD_RELATION", "DELETE_RELATION", "EDIT_RELATION", "REWRITE_RELATION",
            "ADD_PLAN", "DELETE_PLAN", "EDIT_PLAN", "REWRITE_PLAN",
        ):
            assert name in rules


def test_persona_actions_change_first_match_but_never_ai_edit_lock():
    current = "习惯喝茶\n习惯喝茶\n[[LOCK]]\n官方身份\n[[/LOCK]]"
    edited = apply_persona_action(
        current,
        "EDIT_PERSONA",
        {"old": "习惯喝茶", "new": "开始喜欢咖啡"},
    )
    assert edited.startswith("开始喜欢咖啡\n习惯喝茶")

    with pytest.raises(ContentActionError) as locked:
        apply_persona_action(current, "DELETE_PERSONA", {"old": "官方身份"})
    assert locked.value.code == "locked_content"

    rewritten = apply_persona_action(
        current,
        "REWRITE_PERSONA",
        {"content": "新人设\n[[LOCK]]\n官方身份\n[[/LOCK]]"},
    )
    assert rewritten.startswith("新人设")

    with pytest.raises(ContentActionError) as missing:
        apply_persona_action(current, "REWRITE_PERSONA", {"content": "遗漏锁定内容"})
    assert missing.value.code == "rewrite_missing_locked_block"


def test_relation_description_actions_and_two_rewrite_call_styles():
    graph = {
        "用户": {"role": "朋友", "score": 3, "description": "仍然警惕。仍然警惕。"}
    }
    edited = apply_relation_action(
        graph,
        "EDIT_RELATION",
        {"target": "用户", "old": "仍然警惕", "new": "逐渐信任"},
    )
    assert edited["用户"]["description"] == "逐渐信任。仍然警惕。"

    positional = apply_relation_action(
        edited, "REWRITE_RELATION", ["用户", "恋人", 5, "已经完全信任"]
    )
    assert positional["用户"] == {
        "role": "恋人", "score": 5, "description": "已经完全信任"
    }

    named = apply_relation_action(
        positional,
        "REWRITE_RELATION",
        {"target": "用户", "description": "开始主动依赖"},
    )
    assert named["用户"]["role"] == "恋人"
    assert named["用户"]["score"] == 5
    assert named["用户"]["description"] == "开始主动依赖"


def test_plan_date_presence_selects_dated_or_undated_and_first_match():
    schedule = {
        "2026-08-20": ["一起看展", "一起看展"],
        "_undated": ["以后去海边", "以后去海边"],
    }
    dated = apply_plan_action(
        schedule,
        "DELETE_PLAN",
        {"date": "2026-08-20", "content": "一起看展"},
    )
    assert dated["2026-08-20"] == ["一起看展"]
    assert dated["_undated"] == ["以后去海边", "以后去海边"]

    undated = apply_plan_action(
        dated, "DELETE_PLAN", {"content": "以后去海边"}
    )
    assert undated["_undated"] == ["以后去海边"]


def test_edit_plan_atomically_moves_and_changes_content_then_rewrite_keeps_time():
    schedule = {"2026-08-20": ["一起看展"], "_undated": ["以后去海边"]}
    moved = apply_plan_action(
        schedule,
        "EDIT_PLAN",
        {
            "from": {"date": "2026-08-20", "content": "一起看展"},
            "set": {"date": None, "content": "下午一起看展"},
        },
    )
    assert "2026-08-20" not in moved
    assert moved["_undated"] == ["以后去海边", "下午一起看展"]

    rewritten = apply_plan_action(
        moved,
        "REWRITE_PLAN",
        {"at": {"content": "下午一起看展"}, "content": "下午两点一起看展"},
    )
    assert rewritten["_undated"] == ["以后去海边", "下午两点一起看展"]


def test_plan_lookup_never_falls_back_between_dated_and_undated():
    schedule = {"2026-08-20": ["同名计划"], "_undated": []}
    with pytest.raises(ContentActionError) as exc:
        apply_plan_action(schedule, "DELETE_PLAN", {"content": "同名计划"})
    assert exc.value.code == "old_text_not_found"


def test_process_agent_actions_writes_all_three_documents_and_hides_tags(tmp_path):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "1_base_persona.json").write_text(
        json.dumps({
            "system_prompt": "原人设",
            "visual_descriptions": {"tags": "legacy"},
            "custom_settings": {"reply_style": "legacy"},
        }, ensure_ascii=False), encoding="utf-8"
    )
    (prompts / "2_relationship.json").write_text(
        json.dumps({"用户": {"role": "朋友", "score": 3, "description": "有些警惕"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (prompts / "7_schedule.json").write_text("{}", encoding="utf-8")
    raw = (
        '正文\n[ADD_PERSONA: {"content":"新增习惯"}]\n'
        '[REWRITE_RELATION: {"target":"用户","score":4,"description":"逐渐信任"}]\n'
        '[ADD_PLAN: {"content":"以后一起去海边"}]'
    )

    with patch("app.get_paths", return_value=(str(tmp_path / "chat.db"), str(prompts))):
        cleaned, _, _, events = process_agent_actions(
            "hero", raw, user_id="12", return_events=True
        )

    assert cleaned == "正文"
    assert all(event["status"] == "success" for event in events if event["type"] == "content_action")
    persona = json.loads((prompts / "1_base_persona.json").read_text(encoding="utf-8"))
    relation = json.loads((prompts / "2_relationship.json").read_text(encoding="utf-8"))
    schedule = json.loads((prompts / "7_schedule.json").read_text(encoding="utf-8"))
    assert set(persona) == {"system_prompt"}
    assert persona["system_prompt"] == "原人设\n\n新增习惯"
    assert relation["用户"]["score"] == 4
    assert relation["用户"]["description"] == "逐渐信任"
    assert schedule == {"_undated": ["以后一起去海边"]}


def test_same_document_actions_roll_back_together(tmp_path):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    schedule_path = prompts / "7_schedule.json"
    schedule_path.write_text(
        json.dumps({"_undated": ["原计划"]}, ensure_ascii=False), encoding="utf-8"
    )
    raw = (
        '[ADD_PLAN: {"content":"本来可以增加"}]\n'
        '[DELETE_PLAN: {"content":"不存在的计划"}]'
    )

    with patch("app.get_paths", return_value=(str(tmp_path / "chat.db"), str(prompts))):
        cleaned, _, _, events = process_agent_actions(
            "hero", raw, user_id="12", return_events=True
        )

    assert cleaned == ""
    assert json.loads(schedule_path.read_text(encoding="utf-8")) == {"_undated": ["原计划"]}
    action_events = [event for event in events if event["type"] == "content_action"]
    assert len(action_events) == 2
    assert all(event["status"] == "failed" for event in action_events)
