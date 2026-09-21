from datetime import datetime

from core.config import get_global_system_rules
from services import prompt_builder


def _build_location_prompt(tmp_path, monkeypatch):
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()

    locations = [
        {"id": "home", "name": "家", "description": "当前住所", "x": 0.0, "y": 0.0},
        *[
            {
                "id": f"near-{index}",
                "name": f"附近地点{index}",
                "x": index / 20,
                "y": 0.0,
            }
            for index in range(1, 10)
        ],
        {"id": "boundary", "name": "边界地点", "x": 1.0, "y": 0.0},
        {"id": "far", "name": "远方地点", "x": 3.0, "y": 4.0},
    ]
    positions = {
        "hero": {
            "location_id": "home",
            "x": 0.0,
            "y": 0.0,
            "known_location_ids": ["home", "near-1", "far"],
        },
        "friend": {
            "location_id": "home",
            "x": 0.0,
            "y": 0.0,
            "known_location_ids": ["home"],
        },
    }
    map_state = (
        positions,
        {"location_id": "home", "x": 0.0, "y": 0.0},
        {"locations": locations},
    )

    monkeypatch.setattr(
        prompt_builder,
        "get_paths",
        lambda *args, **kwargs: (str(tmp_path / "chat.db"), str(prompts_dir)),
    )
    monkeypatch.setattr(prompt_builder, "normalize_map_state", lambda **kwargs: map_state)
    monkeypatch.setattr(
        prompt_builder,
        "_get_character_time_info",
        lambda *args, **kwargs: (None, "Asia/Shanghai", datetime(2026, 9, 1, 13, 39)),
    )
    monkeypatch.setattr(
        prompt_builder,
        "get_char_name",
        lambda char_id, **kwargs: {"hero": "主角", "friend": "朋友"}.get(char_id, char_id),
    )
    monkeypatch.setattr(prompt_builder, "get_char_age", lambda *args, **kwargs: None)
    monkeypatch.setattr(prompt_builder, "get_user_age", lambda *args, **kwargs: None)
    monkeypatch.setattr(prompt_builder, "_get_username_for_user", lambda *args, **kwargs: "")
    monkeypatch.setattr(prompt_builder, "get_current_user_id", lambda: None)
    monkeypatch.setattr(prompt_builder, "get_ai_language", lambda *args, **kwargs: "zh")
    monkeypatch.setattr(prompt_builder, "_get_char_chat_mode", lambda *args, **kwargs: "online")
    monkeypatch.setattr(
        prompt_builder, "build_agent_current_state_section", lambda *args, **kwargs: ""
    )
    monkeypatch.setattr(prompt_builder, "load_character_positions", lambda **kwargs: {})

    return prompt_builder.build_system_prompt_v2(
        "hero",
        include_long_memory=False,
        include_recent_messages=False,
        user_id="12",
    )


def test_agent_output_contract_and_safety_format_are_not_duplicated(
    tmp_path, monkeypatch
):
    prompt = _build_location_prompt(tmp_path, monkeypatch)

    assert prompt.count("每轮回复末尾必须另起行输出1~3条") == 1
    assert "Agent Output Requirement" not in prompt
    assert prompt.count("[SAFETY_ALERT]") == 1
    assert "[SAFETY_ALERT:" not in prompt

    for lang in ("zh", "ja", "en"):
        rules = get_global_system_rules(lang, "online")
        assert rules.count("[SAFETY_ALERT]") == 1
        assert "[SAFETY_ALERT:" not in rules


def test_location_prompt_lists_all_known_and_nearby_destinations_compactly(
    tmp_path, monkeypatch
):
    prompt = _build_location_prompt(tmp_path, monkeypatch)
    location_section = prompt.split("【現在の場所 / 当前环境与位置】\n", 1)[1]
    location_section = location_section.split(
        "【位置移动指令 / Location Movement Commands】", 1
    )[0]

    assert "【家】（当前住所）坐标（0.0, 0.0）" in location_section
    assert "与你同在的人：朋友、用户" in location_section
    assert "可前往地点（已认知地点，以及当前距离<1的地点）" in location_section
    assert location_section.count("坐标") == 1
    for index in range(1, 10):
        assert f"附近地点{index} [id=near-{index}]" in location_section
    assert "边界地点" not in location_section
    assert "远方地点 [id=far]" in location_section
    assert "距离 " not in location_section
    assert "你去过的认知地点" not in location_section
    assert "附近可感知的地点" not in location_section
