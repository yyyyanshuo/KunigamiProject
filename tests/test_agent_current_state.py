import json

import core.utils as core_utils
import services.prompt_builder as prompt_builder


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_current_state_lists_agent_mutable_values(tmp_path, monkeypatch):
    users_root = tmp_path / "users"
    config_dir = users_root / "12" / "configs"
    prompts_dir = users_root / "12" / "characters" / "hero" / "prompts"
    monkeypatch.setattr(core_utils, "USERS_ROOT", str(users_root))
    monkeypatch.setattr(prompt_builder, "USERS_ROOT", str(users_root))

    _write(config_dir / "characters.json", {
        "hero": {
            "name": "主角",
            "emotion": 7,
            "moments_index": 2.5,
            "intimacy": 81,
            "voice_emotion": "温柔",
            "chat_mode": "offline",
            "light_sleep": False,
            "deep_sleep": True,
            "ds_start": "00:30",
            "ds_end": "08:00",
        }
    })
    _write(config_dir / "user_settings.json", {
        "current_user_name": "测试用户",
        "ai_language": "zh",
    })
    _write(config_dir / "locations.json", {
        "locations": [
            {"id": "home", "name": "家", "x": 0.0, "y": 0.0},
        ]
    })
    _write(config_dir / "character_positions.json", {
        "hero": {
            "location_id": "home",
            "x": 0.0,
            "y": 0.0,
            "known_location_ids": ["home"],
        }
    })
    _write(config_dir / "user_position.json", {
        "location_id": "home",
        "x": 0.0,
        "y": 0.0,
    })
    _write(prompts_dir / "2_relationship.json", {
        "测试用户": {
            "role": "恋人",
            "score": 5,
            "description": "彼此信任",
        }
    })
    _write(prompts_dir / "7_schedule.json", {})

    section = prompt_builder.build_agent_current_state_section(
        "hero",
        str(prompts_dir),
        user_id=12,
    )

    assert "社交渴望度 emotion：7" in section
    assert "朋友圈表达欲 moments_index：2.5" in section
    assert "对用户亲密度 intimacy：81" in section
    assert "语音情绪 voice_emotion：温柔" in section
    assert "聊天模式 chat_mode：offline" in section
    assert "深睡眠：开启（时间段 00:30-08:00）" in section
    assert "测试用户: 恋人 / 5 / 彼此信任" not in section
    assert "当前地理位置：家 [id=home] (0.0, 0.0)" not in section
    assert "未来7天日程" not in section
    assert "角色当地时间" not in section
