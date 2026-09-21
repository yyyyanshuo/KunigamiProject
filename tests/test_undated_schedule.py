import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

import services.prompt_builder as prompt_builder
from agent_utils import process_agent_actions
from services.schedule import (
    ScheduleValidationError,
    append_schedule_item,
    normalize_schedule_data,
)


def _write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_normalize_schedule_adds_undated_to_legacy_data():
    assert normalize_schedule_data({"2026-08-12": " 水族馆 "}) == {
        "2026-08-12": ["水族馆"],
        "_undated": [],
    }


def test_normalize_schedule_preserves_first_match_order_and_rejects_invalid_undated():
    assert normalize_schedule_data({"_undated": [" 去海边 ", "去海边", ""]}) == {
        "_undated": ["去海边", "去海边"]
    }
    with pytest.raises(ScheduleValidationError):
        normalize_schedule_data({"_undated": "去海边"})


def test_append_schedule_item_supports_dated_and_undated(tmp_path):
    path = tmp_path / "7_schedule.json"
    _write_json(path, {})

    assert append_schedule_item(str(path), None, "以后一起去海边") == (True, "undated")
    assert append_schedule_item(str(path), "", "以后一起去海边") == (False, "undated")
    assert append_schedule_item(str(path), "2026-08-12", "去水族馆") == (True, "dated")

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "2026-08-12": ["去水族馆"],
        "_undated": ["以后一起去海边"],
    }


def test_append_schedule_item_does_not_turn_invalid_date_into_undated(tmp_path):
    path = tmp_path / "7_schedule.json"
    _write_json(path, {"_undated": []})

    with pytest.raises(ScheduleValidationError):
        append_schedule_item(str(path), "2026-99-99", "错误日期")

    assert json.loads(path.read_text(encoding="utf-8")) == {"_undated": []}


def test_add_schedule_action_without_date_writes_undated_plan(tmp_path):
    prompts_dir = tmp_path / "prompts"
    _write_json(prompts_dir / "7_schedule.json", {})

    with patch("app.get_paths", return_value=(str(tmp_path / "chat.db"), str(prompts_dir))):
        cleaned, affinity, directive = process_agent_actions(
            "hero",
            '记住了\n[ADD_SCHEDULE: {"content": "以后一起去海边"}]',
            user_id="12",
        )

    assert cleaned == "记住了"
    assert affinity is None
    assert directive is None
    assert json.loads((prompts_dir / "7_schedule.json").read_text(encoding="utf-8")) == {
        "_undated": ["以后一起去海边"]
    }


def _patch_prompt_environment(monkeypatch, prompts_dir):
    monkeypatch.setattr(
        prompt_builder,
        "get_paths",
        lambda *args, **kwargs: (str(prompts_dir.parent / "chat.db"), str(prompts_dir)),
    )
    monkeypatch.setattr(prompt_builder, "normalize_map_state", lambda **kwargs: None)
    monkeypatch.setattr(
        prompt_builder,
        "_get_character_time_info",
        lambda *args, **kwargs: (None, "Asia/Shanghai", datetime(2026, 8, 10, 12, 0)),
    )
    monkeypatch.setattr(prompt_builder, "get_char_name", lambda *args, **kwargs: "")
    monkeypatch.setattr(prompt_builder, "get_char_age", lambda *args, **kwargs: None)
    monkeypatch.setattr(prompt_builder, "get_user_age", lambda *args, **kwargs: None)
    monkeypatch.setattr(prompt_builder, "_get_username_for_user", lambda *args, **kwargs: "")
    monkeypatch.setattr(prompt_builder, "get_ai_language", lambda *args, **kwargs: "zh")
    monkeypatch.setattr(prompt_builder, "_get_char_chat_mode", lambda *args, **kwargs: "online")
    monkeypatch.setattr(prompt_builder, "build_agent_current_state_section", lambda *args, **kwargs: "")


def test_both_prompt_builders_include_only_next_seven_days_and_undated_plans(
    tmp_path, monkeypatch
):
    prompts_dir = tmp_path / "prompts"
    _write_json(
        prompts_dir / "7_schedule.json",
        {
            "2025-01-01": ["过去的计划"],
            "2026-08-10": ["今天的计划"],
            "2026-08-12": "去水族馆",
            "2026-08-17": ["第七天边界计划"],
            "2026-08-18": ["超过七天的计划"],
            "_undated": ["以后一起去海边"],
        },
    )
    _patch_prompt_environment(monkeypatch, prompts_dir)
    monkeypatch.setattr(prompt_builder, "get_current_username", lambda: "")

    v2_prompt = prompt_builder.build_system_prompt_v2(
        "hero",
        include_global_format=False,
        include_long_memory=False,
        include_recent_messages=False,
        user_id=12,
    )
    legacy_prompt = prompt_builder.build_system_prompt(
        "hero",
        include_global_format=False,
        include_long_memory=False,
        user_id=12,
    )

    for prompt in (v2_prompt, legacy_prompt):
        assert "以后一起去海边" in prompt
        assert "Undated Plans" in prompt
        assert "Plans for the Next 7 Days" in prompt
        assert "2026-08-10: 今天的计划" in prompt
        assert "2026-08-12: 去水族馆" in prompt
        assert "2026-08-17: 第七天边界计划" in prompt
        assert "过去的计划" not in prompt
        assert "超过七天的计划" not in prompt


def test_both_prompt_builders_can_include_complete_relationship_graph(tmp_path, monkeypatch):
    prompts_dir = tmp_path / "prompts"
    _write_json(
        prompts_dir / "2_relationship.json",
        {
            "用户": {"role": "恋人", "score": 5, "description": "最亲近的人"},
            "朋友甲": {"role": "朋友", "score": 4, "description": "经常一起训练"},
            "对手乙": {"role": "对手", "score": 2, "description": "彼此竞争"},
        },
    )
    _patch_prompt_environment(monkeypatch, prompts_dir)
    monkeypatch.setattr(prompt_builder, "_get_username_for_user", lambda *args, **kwargs: "用户")
    monkeypatch.setattr(prompt_builder, "get_current_username", lambda: "用户")
    monkeypatch.setattr(prompt_builder, "get_current_user_id", lambda: "12")
    monkeypatch.setattr(
        prompt_builder,
        "_get_characters_config_file",
        lambda *args, **kwargs: str(tmp_path / "missing_characters.json"),
    )

    v2_prompt = prompt_builder.build_system_prompt_v2(
        "hero",
        include_global_format=False,
        include_long_memory=False,
        include_recent_messages=False,
        user_id="12",
        include_all_relationships=True,
    )
    legacy_prompt = prompt_builder.build_system_prompt(
        "hero",
        include_global_format=False,
        include_long_memory=False,
        user_id="12",
        include_all_relationships=True,
    )

    for prompt in (v2_prompt, legacy_prompt):
        assert "Complete Relationship Graph" in prompt
        assert "用户: 恋人 (关系度:5) 最亲近的人" in prompt
        assert "朋友甲: 朋友 (关系度:4) 经常一起训练" in prompt
        assert "对手乙: 对手 (关系度:2) 彼此竞争" in prompt


def test_bedtime_prompt_keeps_content_actions_but_omits_general_agent_rules(tmp_path, monkeypatch):
    prompts_dir = tmp_path / "prompts"
    _write_json(
        prompts_dir / "2_relationship.json",
        {"用户": {"role": "朋友", "score": 3, "description": "熟悉彼此"}},
    )
    _patch_prompt_environment(monkeypatch, prompts_dir)
    monkeypatch.setattr(prompt_builder, "_get_username_for_user", lambda *args, **kwargs: "用户")
    monkeypatch.setattr(
        prompt_builder,
        "_get_characters_config_file",
        lambda *args, **kwargs: str(tmp_path / "missing_characters.json"),
    )

    prompt = prompt_builder.build_system_prompt_v2(
        "hero",
        user_id="12",
        include_all_relationships=True,
        include_general_agent_rules=False,
        include_long_memory=False,
        include_recent_messages=False,
    )

    assert "Complete Relationship Graph" in prompt
    assert "ADD_PERSONA" in prompt
    assert "ADD_RELATION" in prompt
    assert "ADD_PLAN" in prompt
    assert "GENERATE_IMAGE" not in prompt
    assert "DIRECT_TO_GROUP" not in prompt
    assert "MOVE_TO" not in prompt
    assert "每轮回复末尾" not in prompt


def test_role_rules_explicitly_allow_content_only_plan_tag():
    from core.config import get_global_system_rules

    for lang in ("zh", "ja", "en"):
        rules = get_global_system_rules(lang, "online")
        assert "[ADD_PLAN:" in rules


def test_group_prompt_omits_private_voice_call_action(tmp_path, monkeypatch):
    prompts_dir = tmp_path / "prompts"
    _patch_prompt_environment(monkeypatch, prompts_dir)

    private_prompt = prompt_builder.build_system_prompt_v2(
        "hero",
        include_long_memory=False,
        include_recent_messages=False,
        user_id="12",
    )
    group_prompt = prompt_builder.build_system_prompt_v2(
        "hero",
        group_id="group-1",
        include_long_memory=False,
        include_recent_messages=False,
        user_id="12",
    )

    assert "[CALL_USER]" in private_prompt
    assert "[CALL_USER]" not in group_prompt


def test_memory_template_has_separate_undated_editor_and_collector():
    template = Path("templates/memory.html").read_text(encoding="utf-8")

    assert "undated-plan-list" in template
    assert "undated-plan-input" in template
    assert "addUndatedPlanRow" in template
    assert "normalizeScheduleEditorData" in template
    assert "newData._undated" in template
    assert "newData[k].push(v)" in template
