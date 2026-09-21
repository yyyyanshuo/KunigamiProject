import json

from scripts.sync_character_timezones import apply_plan, build_plan


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_timezone_migration_is_dry_until_plan_is_applied(tmp_path):
    users_root = tmp_path / "users"
    config_dir = users_root / "1" / "configs"
    characters_path = config_dir / "characters.json"
    locations_path = config_dir / "locations.json"
    _write(
        characters_path,
        {
            "hero": {
                "name": "Hero",
                "timezone": "Asia/Shanghai",
                "timezone_source": "manual",
                "ds_time_basis": "character",
                "ds_timezone_at_set": "Asia/Shanghai",
                "sleep_last_event_key": "old-event",
            }
        },
    )
    _write(
        config_dir / "character_positions.json",
        {
            "hero": {
                "location_id": "tokyo",
                "x": 1,
                "y": 2,
            }
        },
    )
    _write(
        locations_path,
        {
            "locations": [
                {
                    "id": "tokyo",
                    "name": "Tokyo",
                    "x": 1,
                    "y": 2,
                    "real_world": {"timezone": "Asia/Tokyo"},
                }
            ]
        },
    )
    original_text = characters_path.read_text(encoding="utf-8")

    plan = build_plan(users_root)

    assert not plan.errors
    assert len(plan.changes) == 1
    assert characters_path.read_text(encoding="utf-8") == original_text

    backup_root = apply_plan(plan, tmp_path / "backups")

    migrated = json.loads(characters_path.read_text(encoding="utf-8"))
    assert migrated["hero"]["timezone"] == "Asia/Tokyo"
    assert migrated["hero"]["timezone_source"] == "location"
    assert migrated["hero"]["timezone_location_id"] == "tokyo"
    assert migrated["hero"]["ds_timezone_at_set"] == "Asia/Tokyo"
    assert migrated["hero"]["sleep_last_event_key"] is None
    backup = backup_root / "users" / "1" / "configs" / "characters.json"
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == original_text


def test_timezone_migration_skips_locations_without_real_world_timezone(tmp_path):
    users_root = tmp_path / "users"
    config_dir = users_root / "2" / "configs"
    _write(
        config_dir / "characters.json",
        {"hero": {"name": "Hero", "timezone": "Europe/Paris"}},
    )
    _write(
        config_dir / "character_positions.json",
        {"hero": {"location_id": "home", "x": 0, "y": 0}},
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
                    "real_world": None,
                }
            ]
        },
    )

    plan = build_plan(users_root)

    assert not plan.errors
    assert plan.changes == []
    assert plan.user_plans[0].skipped == [
        "hero: 地点 Home 没有有效时区或经纬度"
    ]
