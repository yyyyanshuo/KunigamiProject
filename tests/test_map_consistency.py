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


def test_normalize_map_state_adds_new_character_to_existing_positions_file(
    tmp_path, monkeypatch
):
    users_root = tmp_path / "users"
    config_dir = users_root / "8" / "configs"
    config_dir.mkdir(parents=True)
    monkeypatch.setattr(core_utils, "USERS_ROOT", str(users_root))

    (config_dir / "characters.json").write_text(
        json.dumps({
            "existing": {"name": "Existing"},
            "new_character": {"name": "New Character"},
        }),
        encoding="utf-8",
    )
    (config_dir / "locations.json").write_text(
        json.dumps({
            "locations": [
                {"id": "home", "name": "Home", "x": 3.0, "y": 4.0},
            ]
        }),
        encoding="utf-8",
    )
    (config_dir / "character_positions.json").write_text(
        json.dumps({
            "existing": {
                "location_id": "home",
                "x": 3.0,
                "y": 4.0,
                "known_location_ids": ["home"],
            },
        }),
        encoding="utf-8",
    )

    positions, _, _ = core_utils.normalize_map_state(user_id=8)

    assert positions["new_character"] == {
        "location_id": "home",
        "x": 3.0,
        "y": 4.0,
        "known_location_ids": ["home"],
    }
    persisted = json.loads(
        (config_dir / "character_positions.json").read_text(encoding="utf-8")
    )
    assert persisted["new_character"] == positions["new_character"]


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


def test_character_can_reach_known_far_or_unknown_near_but_not_unknown_far(
    tmp_path, monkeypatch
):
    users_root = tmp_path / "users"
    config_dir = users_root / "10" / "configs"
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
                {"id": "near", "name": "Near", "x": 0.5, "y": 0.0},
                {"id": "boundary", "name": "Boundary", "x": 1.0, "y": 0.0},
                {"id": "known-far", "name": "Known Far", "x": 5.0, "y": 0.0},
                {"id": "unknown-far", "name": "Unknown Far", "x": 10.0, "y": 0.0},
            ]
        }),
        encoding="utf-8",
    )
    (config_dir / "character_positions.json").write_text(
        json.dumps({
            "hero": {
                "location_id": "home",
                "x": 0.0,
                "y": 0.0,
                "known_location_ids": ["home", "known-far"],
            }
        }),
        encoding="utf-8",
    )
    (config_dir / "user_position.json").write_text(
        json.dumps({"location_id": "home", "x": 0.0, "y": 0.0}),
        encoding="utf-8",
    )

    normalized, _, _ = core_utils.normalize_map_state(user_id=10)
    assert normalized["hero"]["known_location_ids"] == ["home", "known-far"]

    _, far_position = core_utils.move_character_position(
        "hero", 5.0, 0.0, location_id="known-far", user_id=10
    )
    assert far_position["location_id"] == "known-far"

    core_utils.move_character_position(
        "hero", 0.0, 0.0, location_id="home", user_id=10
    )
    _, near_position = core_utils.move_character_position(
        "hero", 0.5, 0.0, location_id="near", user_id=10
    )
    assert near_position["known_location_ids"] == ["home", "known-far", "near"]

    core_utils.move_character_position(
        "hero", 0.0, 0.0, location_id="home", user_id=10
    )
    for location_id, x in (("boundary", 1.0), ("unknown-far", 10.0)):
        try:
            core_utils.move_character_position(
                "hero", x, 0.0, location_id=location_id, user_id=10
            )
            assert False, f"unknown destination {location_id} should fail"
        except ValueError as exc:
            assert "destination is unknown" in str(exc)


def test_exact_coordinate_lookup_does_not_use_nearby_tolerance(tmp_path, monkeypatch):
    users_root = tmp_path / "users"
    config_dir = users_root / "11" / "configs"
    config_dir.mkdir(parents=True)
    monkeypatch.setattr(core_utils, "USERS_ROOT", str(users_root))
    (config_dir / "locations.json").write_text(
        json.dumps({
            "locations": [
                {"id": "exact", "name": "Exact", "x": 1.0, "y": 2.0},
                {"id": "near", "name": "Near", "x": 1.01, "y": 2.0},
            ]
        }),
        encoding="utf-8",
    )

    assert core_utils.get_location_at_exact_coord(1, 2, user_id=11)["id"] == "exact"
    assert core_utils.get_location_at_exact_coord(1.001, 2, user_id=11) is None
