import copy
import importlib.util
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "cleanup_map_locations.py"
SPEC = importlib.util.spec_from_file_location("cleanup_map_locations", SCRIPT_PATH)
cleanup = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(cleanup)


def _sample_data():
    locations = {
        "locations": [
            {"id": "home", "name": "Home", "x": 0.0, "y": 0.0, "is_default": True},
            {"id": "home-copy", "name": "Copy", "x": 0, "y": 0},
            {"id": "keep", "name": "Keep", "x": 2.0, "y": 3.0},
            {"id": "remove", "name": "Remove", "x": 2, "y": 3},
            {"id": "near-only", "name": "Near", "x": 2.01, "y": 3.0},
        ]
    }
    positions = {
        "hero": {
            "location_id": "remove",
            "x": 2.0,
            "y": 3.0,
            "known_location_ids": ["home", "remove", "near-only"],
        },
        "friend": {
            "location_id": "home-copy",
            "x": 0.0,
            "y": 0.0,
            "known_location_ids": ["home-copy", "keep"],
        },
    }
    user_position = {"location_id": "remove", "x": 2.0, "y": 3.0}
    characters = {
        "hero": {"timezone_location_id": "remove"},
        "friend": {"timezone_location_id": "home-copy"},
    }
    return locations, positions, user_position, characters


def test_merge_duplicate_locations_remaps_references_and_resets_knowledge():
    original = _sample_data()
    snapshot = copy.deepcopy(original)
    groups = cleanup.duplicate_groups(original[0]["locations"])
    assert [cleanup.duplicate_group_key(group) for group in groups] == [
        "home|home-copy",
        "keep|remove",
    ]

    updated = cleanup.merge_and_reset(
        *original,
        keepers={"home|home-copy": "home", "keep|remove": "keep"},
    )
    locations, positions, user_position, characters, summary = updated

    assert original == snapshot
    assert [item["id"] for item in locations["locations"]] == [
        "home",
        "keep",
        "near-only",
    ]
    assert positions["hero"]["location_id"] == "keep"
    assert positions["hero"]["known_location_ids"] == ["keep"]
    assert positions["friend"]["location_id"] == "home"
    assert positions["friend"]["known_location_ids"] == ["home"]
    assert user_position == {"location_id": "keep", "x": 2.0, "y": 3.0}
    assert characters["hero"]["timezone_location_id"] == "keep"
    assert characters["friend"]["timezone_location_id"] == "home"
    assert summary["removed_locations"] == 2
    assert summary["reset_characters"] == 2


def test_duplicate_group_containing_home_cannot_keep_another_location():
    locations, positions, user_position, characters = _sample_data()
    try:
        cleanup.merge_and_reset(
            locations,
            positions,
            user_position,
            characters,
            keepers={"home|home-copy": "home-copy"},
        )
        assert False, "home duplicate group must keep home"
    except ValueError as exc:
        assert "must keep default location 'home'" in str(exc)


def test_nearby_but_not_equal_coordinates_are_not_duplicates():
    locations, _, _, _ = _sample_data()
    groups = cleanup.duplicate_groups(locations["locations"])
    grouped_ids = [
        {location["id"] for location in group}
        for group in groups
    ]
    assert {"keep", "near-only"} not in grouped_ids
