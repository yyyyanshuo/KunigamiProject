import json
from datetime import date, datetime, time, timezone

import core.utils as core_utils
import memory_jobs
import services.prompt_builder as prompt_builder
from core.time_utils import (
    BEIJING_TZ_NAME,
    ensure_character_time_defaults,
    resolve_local_datetime,
    sleep_preview,
)


UTC = timezone.utc


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _prepare_character(tmp_path, monkeypatch, *, timezone_name="Asia/Tokyo"):
    users_root = tmp_path / "users"
    config_dir = users_root / "12" / "configs"
    prompts_dir = users_root / "12" / "characters" / "hero" / "prompts"
    monkeypatch.setattr(core_utils, "USERS_ROOT", str(users_root))
    monkeypatch.setattr(prompt_builder, "USERS_ROOT", str(users_root))
    _write(
        config_dir / "characters.json",
        {
            "hero": {
                "name": "主角",
                "timezone": timezone_name,
                "timezone_source": "manual",
            }
        },
    )
    _write(
        config_dir / "user_settings.json",
        {"timezone": "America/New_York", "ai_language": "zh"},
    )
    return config_dir, prompts_dir


def test_time_defaults_preserve_legacy_sleep_basis_and_japanese_timezone():
    legacy = {"language": "ja"}
    assert ensure_character_time_defaults(legacy, existing_character=True)
    assert legacy["timezone"] == "Asia/Tokyo"
    assert legacy["ds_time_basis"] == "user"
    assert legacy["ds_timezone_at_set"] == BEIJING_TZ_NAME

    new_character = {"language": "ja"}
    assert ensure_character_time_defaults(new_character, existing_character=False)
    assert new_character["timezone"] == "Asia/Tokyo"
    assert new_character["ds_time_basis"] == "character"
    assert new_character["ds_timezone_at_set"] == "Asia/Tokyo"


def test_character_sleep_preview_converts_to_beijing_and_user_time():
    info = {
        "timezone": "Asia/Tokyo",
        "ds_time_basis": "character",
        "ds_start": "23:00",
        "ds_end": "07:00",
    }
    preview = sleep_preview(
        info,
        {"timezone": "America/New_York"},
        now=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
    )

    assert preview["source_timezone"] == "Asia/Tokyo"
    assert preview["sleep"] == {
        "source": "2026-07-31 23:00",
        "beijing": "2026-07-31 22:00",
        "character": "2026-07-31 23:00",
        "user": "2026-07-31 10:00",
    }


def test_dst_nonexistent_and_ambiguous_wall_times_are_deterministic():
    spring = resolve_local_datetime(
        date(2026, 3, 8),
        time(2, 30),
        "America/New_York",
    )
    assert spring.strftime("%Y-%m-%d %H:%M %z") == "2026-03-08 03:00 -0400"

    fall = resolve_local_datetime(
        date(2026, 11, 1),
        time(1, 30),
        "America/New_York",
    )
    assert fall.fold == 0
    assert fall.strftime("%Y-%m-%d %H:%M %z") == "2026-11-01 01:30 -0400"


def test_location_move_updates_character_timezone_and_returns_change(tmp_path, monkeypatch):
    users_root = tmp_path / "users"
    config_dir = users_root / "9" / "configs"
    monkeypatch.setattr(core_utils, "USERS_ROOT", str(users_root))
    _write(
        config_dir / "characters.json",
        {
            "hero": {
                "name": "Hero",
                "timezone": "Asia/Shanghai",
                "timezone_source": "location",
                "timezone_location_id": "home",
            }
        },
    )
    _write(
        config_dir / "locations.json",
        {
            "locations": [
                {
                    "id": "home",
                    "name": "Home",
                    "x": 0,
                    "y": 0,
                    "real_world": {"timezone": "Asia/Shanghai"},
                },
                {
                    "id": "tokyo",
                    "name": "Tokyo",
                    "x": 3,
                    "y": 0,
                    "real_world": {"timezone": "Asia/Tokyo"},
                },
            ]
        },
    )
    _write(
        config_dir / "character_positions.json",
        {
            "hero": {
                "location_id": "home",
                "x": 0,
                "y": 0,
                "known_location_ids": ["home"],
            }
        },
    )
    _write(
        config_dir / "user_position.json",
        {"location_id": "home", "x": 0, "y": 0},
    )

    _, position = core_utils.move_character_position(
        "hero",
        3,
        0,
        location_id="tokyo",
        force=True,
        user_id=9,
    )

    assert position["timezone_change"] == {
        "changed": True,
        "from": "Asia/Shanghai",
        "to": "Asia/Tokyo",
        "old_timezone": "Asia/Shanghai",
        "new_timezone": "Asia/Tokyo",
        "reason": "location",
        "location_id": "tokyo",
    }
    characters = json.loads(
        (config_dir / "characters.json").read_text(encoding="utf-8")
    )
    assert characters["hero"]["timezone"] == "Asia/Tokyo"
    assert characters["hero"]["timezone_source"] == "location"


def test_map_normalization_does_not_override_direct_manual_timezone(
    tmp_path, monkeypatch
):
    users_root = tmp_path / "users"
    config_dir = users_root / "10" / "configs"
    monkeypatch.setattr(core_utils, "USERS_ROOT", str(users_root))
    _write(
        config_dir / "characters.json",
        {
            "hero": {
                "name": "Hero",
                "timezone": "Europe/Paris",
                "timezone_source": "manual",
            }
        },
    )
    _write(
        config_dir / "locations.json",
        {
            "locations": [
                {
                    "id": "tokyo",
                    "name": "Tokyo",
                    "x": 0,
                    "y": 0,
                    "real_world": {"timezone": "Asia/Tokyo"},
                }
            ]
        },
    )
    _write(
        config_dir / "character_positions.json",
        {
            "hero": {
                "location_id": "tokyo",
                "x": 0,
                "y": 0,
                "known_location_ids": ["tokyo"],
            }
        },
    )
    _write(
        config_dir / "user_position.json",
        {"location_id": "tokyo", "x": 0, "y": 0},
    )

    core_utils.normalize_map_state(user_id=10)

    characters = json.loads(
        (config_dir / "characters.json").read_text(encoding="utf-8")
    )
    assert characters["hero"]["timezone"] == "Europe/Paris"
    assert characters["hero"]["timezone_source"] == "manual"


def test_short_memory_reads_previous_24_hours_and_uses_character_time(
    tmp_path, monkeypatch
):
    _, prompts_dir = _prepare_character(tmp_path, monkeypatch)
    fixed_now = datetime(2026, 7, 31, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(prompt_builder, "utc_now", lambda: fixed_now)
    _write(
        prompts_dir / "6_memory_short.json",
        {
            "2026-07-31": {
                "events": [
                    {"time": "00:00", "event": "刚好二十四小时前，不读取"},
                    {"time": "00:01", "event": "二十四小时内的旧事件"},
                    {"time": "23:50", "event": "最近事件"},
                ]
            },
            "2026-08-01": {"events": []},
        },
    )

    events = prompt_builder.extract_short_memory_with_timeline_ts(
        "hero", user_id=12
    )

    assert [(content, ts.strftime("%Y-%m-%d %H:%M")) for content, _, ts in events] == [
        ("二十四小时内的旧事件", "2026-07-31 01:01"),
        ("最近事件", "2026-08-01 00:50"),
    ]


def test_medium_and_long_memory_keep_single_anchor_in_character_time(
    tmp_path, monkeypatch
):
    _, prompts_dir = _prepare_character(tmp_path, monkeypatch)
    fixed_beijing = datetime.fromisoformat("2026-08-01T00:00:00+08:00")
    monkeypatch.setattr(prompt_builder, "beijing_now", lambda: fixed_beijing)
    _write(prompts_dir / "5_memory_medium.json", {"2026-07-31": "中期摘要"})
    _write(prompts_dir / "4_memory_long.json", {"2026-07": "长期摘要"})

    medium = prompt_builder.extract_medium_memory_with_timeline_ts(
        "hero", user_id=12
    )
    long = prompt_builder.extract_long_memory_with_timeline_ts(
        "hero", user_id=12
    )
    timeline = prompt_builder.build_timeline_section(
        [
            ("medium_memory", medium[0][0], medium[0][2]),
            ("long_memory", long[0][0], long[0][2]),
        ]
    )

    assert medium[0][2].strftime("%Y-%m-%d %H:%M") == "2026-08-01 00:59"
    assert long[0][2].strftime("%Y-%m-%d %H:%M") == "2026-08-01 00:59"
    assert "[2026-08-01 00:59] 【中期记忆】 中期摘要" in timeline
    assert "[2026-08-01 00:59] 【长期记忆】 长期摘要" in timeline
    assert "～" not in timeline
    assert "至" not in timeline


def test_manual_deep_sleep_cannot_enter_diary_retry_queue():
    now = datetime(2026, 7, 31, 23, 0)
    manual = {
        "deep_sleep": True,
        "deep_sleep_source": "manual_user",
        "bedtime_diary_enabled": True,
        "bedtime_diary_date": "2026-07-31",
        "bedtime_diary_status": "pending",
    }
    scheduled = {
        **manual,
        "deep_sleep_source": "schedule",
    }

    assert not memory_jobs._should_run_bedtime_diary(
        manual, "2026-07-31", now
    )
    assert memory_jobs._should_run_bedtime_diary(
        scheduled, "2026-07-31", now
    )
