import json

from scripts.migrate_sleep_times_to_character_local import (
    apply_plan,
    build_plan,
)


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_sleep_migration_preserves_wall_clock_values_and_changes_basis(tmp_path):
    users_root = tmp_path / "users"
    characters_path = users_root / "1" / "configs" / "characters.json"
    _write(
        characters_path,
        {
            "hero": {
                "name": "Hero",
                "timezone": "America/New_York",
                "ds_start": "02:00",
                "ds_end": "09:30",
                "ds_time_basis": "user",
                "ds_timezone_at_set": "Asia/Shanghai",
                "deep_sleep": True,
                "sleep_manual_override": True,
                "sleep_last_event_key": "old",
            }
        },
    )
    original_text = characters_path.read_text(encoding="utf-8")

    plan = build_plan(users_root)

    assert not plan.errors
    assert len(plan.changes) == 1
    assert characters_path.read_text(encoding="utf-8") == original_text

    backup_root = apply_plan(plan, tmp_path / "backups")
    migrated = json.loads(characters_path.read_text(encoding="utf-8"))
    hero = migrated["hero"]
    assert hero["ds_start"] == "02:00"
    assert hero["ds_end"] == "09:30"
    assert hero["ds_time_basis"] == "character"
    assert hero["ds_timezone_at_set"] == "America/New_York"
    assert hero["ds_set_by"] == "migration_character_local"
    assert hero["sleep_last_event_key"] is None
    assert hero["deep_sleep"] is True
    assert hero["sleep_manual_override"] is True
    backup = backup_root / "users" / "1" / "configs" / "characters.json"
    assert backup.read_text(encoding="utf-8") == original_text


def test_sleep_migration_skips_invalid_timezone_and_sleep_time(tmp_path):
    users_root = tmp_path / "users"
    characters_path = users_root / "2" / "configs" / "characters.json"
    _write(
        characters_path,
        {
            "no_timezone": {
                "ds_start": "02:00",
                "ds_end": "09:00",
            },
            "bad_time": {
                "timezone": "Asia/Tokyo",
                "ds_start": "25:00",
                "ds_end": "09:00",
            },
        },
    )

    plan = build_plan(users_root)

    assert not plan.errors
    assert plan.changes == []
    assert len(plan.user_plans[0].skipped) == 2
