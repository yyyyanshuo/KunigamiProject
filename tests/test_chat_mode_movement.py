"""Location-driven mode changes happen on movement, never on map/prompt reads."""
import json

import pytest

from core import utils


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _modes(config_dir):
    characters = json.loads((config_dir / "characters.json").read_text())
    return {cid: info.get("chat_mode") for cid, info in characters.items()}


@pytest.fixture
def mode_map(tmp_path, monkeypatch):
    import app as application
    from core import languages, localization, user_resources
    from services import prompt_builder

    users_root = tmp_path / "users"
    config_dir = users_root / "7" / "configs"
    for module in (utils, user_resources, prompt_builder, application):
        monkeypatch.setattr(module, "USERS_ROOT", str(users_root))
    for module in (languages, localization):
        monkeypatch.setattr(module, "DATA_ROOT", str(tmp_path))
    _write(config_dir / "characters.json", {
        "hero": {"name": "Hero", "chat_mode": "online", "language": "zh"},
        "other": {"name": "Other", "chat_mode": "offline", "language": "zh"},
    })
    _write(config_dir / "user_settings.json", {"ai_language": "zh"})
    _write(config_dir / "locations.json", {"locations": [
        {"id": "home", "name": "Home", "x": 0.0, "y": 0.0},
        {"id": "cafe", "name": "Cafe", "x": 2.0, "y": 0.0},
    ]})
    _write(config_dir / "character_positions.json", {
        "hero": {"location_id": "home", "x": 0.0, "y": 0.0,
                 "known_location_ids": ["home", "cafe"]},
        "other": {"location_id": "cafe", "x": 2.0, "y": 0.0,
                  "known_location_ids": ["home", "cafe"]},
    })
    _write(config_dir / "user_position.json", {"location_id": "home", "x": 0.0, "y": 0.0})
    return config_dir


def _move(actor, x, y, **kwargs):
    if actor == "character":
        return utils.move_character_position("hero", x, y, user_id=7, **kwargs)
    return utils.move_user_position(x, y, user_id=7, **kwargs)


@pytest.mark.parametrize("source", ["user", "character", "call"])
@pytest.mark.parametrize("mode", ["online", "offline"])
def test_map_refresh_and_prompt_preserve_selected_mode(mode_map, source, mode):
    from flask import session
    from app import app
    from agent_utils import process_agent_actions
    from blueprints import calls, chat, lbs
    from core.config import get_mode_context
    from services.prompt_builder import build_system_prompt_v2

    # Deliberately disagree with the map so a read cannot silently win.
    catalog = mode_map / "characters.json"
    characters = json.loads(catalog.read_text())
    characters["hero"]["chat_mode"] = "online" if mode == "offline" else "offline"
    _write(catalog, characters)
    if mode == "offline":
        positions = utils.load_character_positions(7)
        positions["hero"].update(location_id="cafe", x=2.0)
        utils.save_character_positions(positions, 7)

    with app.test_request_context(json={"chat_mode": mode}):
        session["user_id"] = 7
        if source == "user":
            assert chat.update_char_meta("hero").status_code == 200
        elif source == "character":
            process_agent_actions("hero", f"[SET_CHAT_MODE:{mode}]", 7)
        else:
            calls._set_call_chat_mode(7, "hero", mode)
        assert _modes(mode_map)["hero"] == mode

        assert lbs.api_get_positions().status_code == 200
        assert _modes(mode_map) == {"hero": mode, "other": "offline"}
        prompt = build_system_prompt_v2(
            "hero", user_id=7, call_mode=source == "call",
            include_long_memory=False, include_recent_messages=False,
        )
        assert _modes(mode_map) == {"hero": mode, "other": "offline"}
        if source != "call":
            assert get_mode_context("zh", mode) in prompt


@pytest.mark.parametrize("actor", ["character", "user"])
@pytest.mark.parametrize("named", [True, False])
def test_movement_immediately_switches_on_departure_and_arrival(mode_map, actor, named):
    origin = {"location_id": "home", "x": 0.0, "y": 0.0} if named else {
        "location_id": None, "x": 3.0, "y": 3.0,
    }
    positions = utils.load_character_positions(7)
    positions["hero"].update(origin)
    utils.save_character_positions(positions, 7)
    utils.save_user_position(origin, 7)
    catalog = mode_map / "characters.json"
    characters = json.loads(catalog.read_text())
    characters["hero"]["chat_mode"] = "offline"
    _write(catalog, characters)

    _move(actor, origin["x"] + 0.5, origin["y"])
    assert _modes(mode_map)["hero"] == "online"
    _move(actor, origin["x"], origin["y"], location_id=origin["location_id"])
    assert _modes(mode_map)["hero"] == "offline"
    # Only the moving character changes; a user move re-evaluates every pair.
    assert _modes(mode_map)["other"] == ("offline" if actor == "character" else "online")


@pytest.mark.parametrize("actor", ["character", "user"])
@pytest.mark.parametrize("distance,expected", [(0.099, "offline"), (0.1, "online")])
def test_unnamed_arrival_uses_strict_proximity_boundary(mode_map, actor, distance, expected):
    positions = utils.load_character_positions(7)
    positions["hero"].update(location_id=None, x=1.0 if actor == "character" else 0.0, y=3.0)
    utils.save_character_positions(positions, 7)
    utils.save_user_position({"location_id": None, "x": 1.0 if actor == "user" else 0.0, "y": 3.0}, 7)

    _move(actor, distance, 3.0)
    assert _modes(mode_map)["hero"] == expected


@pytest.mark.parametrize("actor", ["character", "user"])
@pytest.mark.parametrize("named", [True, False])
def test_saving_same_position_preserves_modes(mode_map, actor, named):
    if not named:
        positions = utils.load_character_positions(7)
        positions["hero"].update(location_id=None, x=3.0, y=3.0)
        utils.save_character_positions(positions, 7)
        utils.save_user_position({"location_id": None, "x": 3.0, "y": 3.0}, 7)
    origin = utils.load_character_positions(7)["hero"]
    before = _modes(mode_map)

    _move(actor, origin["x"], origin["y"], location_id=origin["location_id"])
    assert _modes(mode_map) == before


def test_initializing_map_does_not_set_or_override_modes(mode_map):
    catalog = mode_map / "characters.json"
    characters = json.loads(catalog.read_text())
    characters["new"] = {"name": "New"}
    _write(catalog, characters)
    for name in ("character_positions.json", "user_position.json", "locations.json"):
        (mode_map / name).unlink()

    utils.normalize_map_state(7)
    assert _modes(mode_map) == {"hero": "online", "other": "offline", "new": None}


def test_failed_move_preserves_modes(mode_map):
    before = _modes(mode_map)
    with pytest.raises(ValueError, match="destination is unknown"):
        utils.move_character_position("hero", 10, 10, user_id=7)
    assert _modes(mode_map) == before


def test_character_action_can_override_mode_until_next_move(mode_map):
    from agent_utils import process_agent_actions

    process_agent_actions("hero", "[SET_CHAT_MODE:offline]", 7)
    assert _modes(mode_map)["hero"] == "offline"
    process_agent_actions("hero", "[MOVE_TO:cafe]", 7)
    assert _modes(mode_map) == {"hero": "online", "other": "offline"}
    process_agent_actions("hero", "[MOVE_TO:home]", 7)
    assert _modes(mode_map)["hero"] == "offline"


def test_user_move_leaves_one_character_and_meets_another(mode_map):
    utils.move_user_position(2, 0, location_id="cafe", user_id=7)
    assert _modes(mode_map) == {"hero": "online", "other": "offline"}
    utils.move_user_position(0, 0, location_id="home", user_id=7)
    assert _modes(mode_map) == {"hero": "offline", "other": "online"}
