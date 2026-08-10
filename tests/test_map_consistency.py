import json

import core.utils as core_utils


def test_normalize_map_state_repairs_coordinates_and_drops_orphans(tmp_path, monkeypatch):
    users_root = tmp_path / "users"
    config_dir = users_root / "7" / "configs"
    config_dir.mkdir(parents=True)
    monkeypatch.setattr(core_utils, "USERS_ROOT", str(users_root))

    (config_dir / "characters.json").write_text(
        json.dumps({"hero": {"name": "Hero"}}),
        encoding="utf-8",
    )
    (config_dir / "locations.json").write_text(
        json.dumps({
            "locations": [
                {"id": "home", "name": "Home", "x": 0.0, "y": 0.0},
                {"id": "cafe", "name": "Cafe", "x": 2.0, "y": 3.0},
            ]
        }),
        encoding="utf-8",
    )
    (config_dir / "character_positions.json").write_text(
        json.dumps({
            "hero": {
                "location_id": "cafe",
                "x": 999,
                "y": 999,
                "known_location_ids": ["missing"],
            },
            "deleted": {
                "location_id": "home",
                "x": 0,
                "y": 0,
                "known_location_ids": ["home"],
            },
        }),
        encoding="utf-8",
    )
    (config_dir / "user_position.json").write_text(
        json.dumps({"location_id": "home", "x": 8, "y": 8}),
        encoding="utf-8",
    )

    positions, user_position, _ = core_utils.normalize_map_state(user_id=7)

    assert set(positions) == {"hero"}
    assert positions["hero"]["location_id"] == "cafe"
    assert positions["hero"]["x"] == 2.0
    assert positions["hero"]["y"] == 3.0
    assert positions["hero"]["known_location_ids"] == ["cafe"]
    assert user_position == {"location_id": "home", "x": 0.0, "y": 0.0}
    characters = json.loads((config_dir / "characters.json").read_text(encoding="utf-8"))
    assert characters["hero"]["chat_mode"] == "online"


def test_move_character_position_rejects_unknown_character_and_location(tmp_path, monkeypatch):
    users_root = tmp_path / "users"
    config_dir = users_root / "9" / "configs"
    config_dir.mkdir(parents=True)
    monkeypatch.setattr(core_utils, "USERS_ROOT", str(users_root))

    (config_dir / "characters.json").write_text(
        json.dumps({"hero": {"name": "Hero"}}),
        encoding="utf-8",
    )
    core_utils.init_map_data(user_id=9)

    try:
        core_utils.move_character_position("ghost", 0, 0, user_id=9)
        assert False, "unknown character should fail"
    except ValueError as exc:
        assert str(exc) == "character not found"

    try:
        core_utils.move_character_position(
            "hero",
            0,
            0,
            location_id="missing",
            user_id=9,
        )
        assert False, "unknown location should fail"
    except ValueError as exc:
        assert str(exc) == "location not found"
