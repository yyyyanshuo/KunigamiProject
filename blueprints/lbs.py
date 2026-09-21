from flask import Blueprint, request, jsonify, session, redirect, render_template
import os
import json
import math
from datetime import datetime
import weather_api
from core.config import COS_BASE_URL
from core.context import get_current_user_id
from core.utils import (
    safe_save_json,
    _get_locations_file,
    _get_character_positions_file,
    _get_user_position_file,
    _get_characters_config_file,
    init_map_data,
    load_locations,
    save_locations,
    load_character_positions,
    save_character_positions,
    load_user_position,
    save_user_position,
    calc_distance,
    get_location_by_id,
    get_location_at_exact_coord,
    auto_toggle_chat_mode_on_move,
    normalize_map_state,
    normalize_real_world_timezone,
    sync_character_timezone_to_location,
    move_character_position,
    move_user_position,
)
from core.time_utils import (
    BEIJING_TZ_NAME,
    get_zone,
    is_valid_timezone,
    search_timezones,
)

map_bp = Blueprint('map', __name__)

# ===================== 地图系统 API =====================

@map_bp.route("/map")
def map_page():
    uid = session.get("user_id")
    if not uid:
        return redirect("/login")
    init_map_data()
    return render_template("map.html", user_id=uid,
                           COS_BASE_URL=COS_BASE_URL)

@map_bp.route("/api/map/locations", methods=["GET"])
def api_get_locations():
    init_map_data()
    return jsonify(load_locations())

@map_bp.route("/api/map/locations", methods=["POST"])
def api_add_location():
    init_map_data()
    data = request.json or {}
    loc_id = data.get("id", "").strip()
    name = data.get("name", "").strip()
    description = data.get("description", "").strip()
    try:
        x = float(data.get("x", 0))
        y = float(data.get("y", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid coordinates"}), 400
    if not math.isfinite(x) or not math.isfinite(y):
        return jsonify({"error": "invalid coordinates"}), 400
    if not loc_id or not name:
        return jsonify({"error": "id and name are required"}), 400
    locs = load_locations()
    for loc in locs.get("locations", []):
        if loc["id"] == loc_id:
            return jsonify({"error": f"location id '{loc_id}' already exists"}), 400
    existing = get_location_at_exact_coord(x, y)
    if existing:
        return jsonify({
            "existing": True,
            "location": existing,
            "message": "same coordinates already belong to an existing location",
        })
    r = math.sqrt(x*x + y*y)
    theta = math.atan2(y, x)
    rw = data.get("real_world")
    if rw is not None:
        rw["lat"] = float(rw.get("lat", 0)) if rw.get("lat") is not None else None
        rw["lon"] = float(rw.get("lon", 0)) if rw.get("lon") is not None else None
        try:
            rw = normalize_real_world_timezone(rw)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    new_loc = {
        "id": loc_id,
        "name": name,
        "description": description,
        "x": x,
        "y": y,
        "r": round(r, 4),
        "theta": round(theta, 4),
        "is_default": False,
        "real_world": rw
    }
    locs["locations"].append(new_loc)
    save_locations(locs)

    return jsonify(new_loc), 201

@map_bp.route("/api/map/locations/<loc_id>", methods=["PUT"])
def api_update_location(loc_id):
    data = request.json or {}
    locs = load_locations()
    loc = next(
        (item for item in locs.get("locations", []) if item.get("id") == loc_id),
        None,
    )
    if not loc:
        return jsonify({"error": "location not found"}), 404

    try:
        new_x = float(data.get("x", loc["x"]))
        new_y = float(data.get("y", loc["y"]))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid coordinates"}), 400
    if not math.isfinite(new_x) or not math.isfinite(new_y):
        return jsonify({"error": "invalid coordinates"}), 400
    duplicate = get_location_at_exact_coord(new_x, new_y, exclude_id=loc_id)
    if duplicate:
        return jsonify({
            "error": "coordinates already belong to an existing location",
            "existing_location": duplicate,
        }), 409

    rw = loc.get("real_world")
    if "real_world" in data:
        rw = data["real_world"]
        if rw is not None:
            rw = dict(rw)
            try:
                rw["lat"] = float(rw.get("lat", 0)) if rw.get("lat") is not None else None
                rw["lon"] = float(rw.get("lon", 0)) if rw.get("lon") is not None else None
                rw = normalize_real_world_timezone(rw)
            except (TypeError, ValueError) as e:
                return jsonify({"error": str(e)}), 400

    if "name" in data:
        loc["name"] = data["name"].strip()
    if "description" in data:
        loc["description"] = data["description"].strip()
    if "real_world" in data:
        loc["real_world"] = rw
    coordinates_changed = new_x != float(loc["x"]) or new_y != float(loc["y"])
    if coordinates_changed:
        loc["x"] = new_x
        loc["y"] = new_y
        loc["r"] = round(math.sqrt(new_x**2 + new_y**2), 4)
        loc["theta"] = round(math.atan2(new_y, new_x), 4)
        positions = load_character_positions()
        for pos in positions.values():
            if pos.get("location_id") == loc_id:
                pos["x"] = new_x
                pos["y"] = new_y
        save_character_positions(positions)
        user_pos = load_user_position()
        if user_pos.get("location_id") == loc_id:
            user_pos["x"] = new_x
            user_pos["y"] = new_y
            save_user_position(user_pos)

    save_locations(locs)
    if "real_world" in data:
        positions = load_character_positions()
        for cid, pos in positions.items():
            if pos.get("location_id") == loc_id:
                sync_character_timezone_to_location(
                    cid,
                    loc,
                    user_id=get_current_user_id(),
                )
    return jsonify(loc)

@map_bp.route("/api/map/locations/<loc_id>", methods=["DELETE"])
def api_delete_location(loc_id):
    locs = load_locations()
    for i, loc in enumerate(locs.get("locations", [])):
        if loc["id"] == loc_id:
            if loc.get("is_default"):
                return jsonify({"error": "cannot delete default location '家'"}), 400
            locs["locations"].pop(i)
            save_locations(locs)
            positions = load_character_positions()
            for cid in positions:
                known = positions[cid].get("known_location_ids", [])
                if loc_id in known:
                    known.remove(loc_id)
                    positions[cid]["known_location_ids"] = known
                if positions[cid].get("location_id") == loc_id:
                    positions[cid]["location_id"] = None
            save_character_positions(positions)
            return jsonify({"ok": True})
    return jsonify({"error": "location not found"}), 404

@map_bp.route("/api/map/positions", methods=["GET"])
def api_get_positions():
    init_map_data()
    char_positions, user_position, _ = normalize_map_state(
        user_id=get_current_user_id(),
        drop_orphan_positions=True,
    )

    chars_cfg = _get_characters_config_file()
    char_names = {}
    if os.path.exists(chars_cfg):
        try:
            with open(chars_cfg, "r", encoding="utf-8") as f:
                chars_data = json.load(f)
            for cid, cinfo in chars_data.items():
                char_names[cid] = cinfo.get("name", cid)
        except:
            pass
    result = {"characters": {}, "user": user_position}
    for cid, pos in char_positions.items():
        result["characters"][cid] = {
            **pos,
            "name": char_names.get(cid, cid)
        }
    return jsonify(result)

@map_bp.route("/api/map/character/<char_id>/position", methods=["PUT"])
def api_move_character(char_id):
    from app import get_char_name, append_short_memory_event
    data = request.json or {}
    try:
        x = float(data.get("x", 0))
        y = float(data.get("y", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid coordinates"}), 400
    location_id = data.get("location_id")
    force = data.get("force", False)
    try:
        _, new_position = move_character_position(
            char_id,
            x,
            y,
            location_id=location_id,
            force=bool(force),
            user_id=get_current_user_id(),
        )
    except ValueError as e:
        status = 404 if str(e) in {"character not found", "location not found"} else 400
        return jsonify({"error": str(e)}), status

    loc_name = new_position["location_id"] or f"({round(new_position['x'],2)}, {round(new_position['y'],2)})"
    char_name = get_char_name(char_id)
    encounters = check_co_encounters(
        char_id,
        new_position["x"],
        new_position["y"],
        new_position.get("location_id"),
    )
    encounter_msgs = []
    if encounters:
        for eid in encounters:
            if eid == "user":
                encounter_msgs.append(f"与用户在{loc_name}相遇")
            else:
                ename = get_char_name(eid)
                encounter_msgs.append(f"与{ename}在{loc_name}相遇")

    timezone_change = new_position.pop("timezone_change", {"changed": False})
    return jsonify({
        "position": new_position,
        "encounters": encounters,
        "timezone_change": timezone_change,
    })

@map_bp.route("/api/map/user/position", methods=["PUT"])
def api_move_user():
    data = request.json or {}
    try:
        x = float(data.get("x", 0))
        y = float(data.get("y", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid coordinates"}), 400
    location_id = data.get("location_id")
    try:
        _, user_pos = move_user_position(
            x,
            y,
            location_id=location_id,
            user_id=get_current_user_id(),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify(user_pos)

@map_bp.route("/api/map/character/<char_id>/locations", methods=["GET"])
def api_get_character_known_locations(char_id):
    cfg_file = _get_characters_config_file()
    try:
        with open(cfg_file, "r", encoding="utf-8") as f:
            chars = json.load(f) or {}
    except Exception:
        chars = {}
    if char_id not in chars:
        return jsonify({"error": "character not found"}), 404
    positions = load_character_positions()
    if char_id not in positions:
        positions[char_id] = {"location_id": "home", "x": 0.0, "y": 0.0, "known_location_ids": ["home"]}
        save_character_positions(positions)
    known_ids = positions[char_id].get("known_location_ids", [])
    all_locs = load_locations()
    known_locs = []
    for loc in all_locs.get("locations", []):
        if loc["id"] in known_ids:
            known_locs.append(loc)
    return jsonify({"locations": known_locs, "position": positions[char_id]})

@map_bp.route("/api/map/character/<char_id>/known_locations", methods=["PUT"])
def api_update_character_known_locations(char_id):
    data = request.json or {}
    cfg_file = _get_characters_config_file()
    try:
        with open(cfg_file, "r", encoding="utf-8") as f:
            chars = json.load(f) or {}
    except Exception:
        chars = {}
    if char_id not in chars:
        return jsonify({"error": "character not found"}), 404
    valid_location_ids = {
        loc["id"] for loc in load_locations().get("locations", [])
    }
    requested_ids = [
        loc_id for loc_id in (data.get("known_location_ids") or [])
        if loc_id in valid_location_ids
    ]
    positions = load_character_positions()
    if char_id not in positions:
        positions[char_id] = {"location_id": "home", "x": 0.0, "y": 0.0, "known_location_ids": ["home"]}
    # This endpoint is an explicit administrator override. Automatic discovery
    # is still arrival-only, but the map editor may repair/add/remove knowledge.
    known_ids = requested_ids
    current_location_id = positions[char_id].get("location_id")
    if (
        current_location_id in valid_location_ids
        and current_location_id not in known_ids
    ):
        known_ids.append(current_location_id)
    known_ids = list(dict.fromkeys(known_ids))
    positions[char_id]["known_location_ids"] = known_ids
    save_character_positions(positions)
    return jsonify({"ok": True, "known_location_ids": known_ids})

# ===================== 天气 API =====================

@map_bp.route("/api/map/weather", methods=["GET"])
def api_get_weather():
    locs = load_locations()
    all_locs = locs.get("locations", [])
    return jsonify(weather_api.get_all_weather(all_locs))

@map_bp.route("/api/map/weather/<loc_id>", methods=["GET"])
def api_get_location_weather(loc_id):
    loc = get_location_by_id(loc_id)
    if not loc:
        return jsonify({"error": "location not found"}), 404
    weather = weather_api.get_weather_for_location(loc)
    if weather is None:
        return jsonify({"error": "no real_world data for this location"}), 404
    return jsonify(weather)

@map_bp.route("/api/map/geocode", methods=["GET"])
def api_geocode():
    city = request.args.get("city", "").strip()
    country = request.args.get("country", "").strip() or None
    if not city:
        return jsonify({"error": "city parameter required"}), 400
    lat, lon = weather_api.geocode(city, country=country)
    if lat is None:
        return jsonify({"error": "geocode failed"}), 404
    real_world = normalize_real_world_timezone({
        "city": city,
        "country": country or "",
        "lat": lat,
        "lon": lon,
    })
    return jsonify(real_world)


@map_bp.route("/api/timezones", methods=["GET"])
def api_timezones():
    query = request.args.get("query", "")
    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    now = datetime.now(get_zone(BEIJING_TZ_NAME))
    result = []
    for name in search_timezones(query, limit):
        local_now = now.astimezone(get_zone(name))
        offset = local_now.strftime("%z")
        offset_display = f"{offset[:3]}:{offset[3:]}" if offset else "+00:00"
        result.append({
            "name": name,
            "offset": f"UTC{offset_display}",
            "local_time": local_now.strftime("%Y-%m-%d %H:%M"),
        })
    return jsonify({"timezones": result})

def sync_known_locations(char_id):
    positions = load_character_positions()
    if char_id not in positions:
        positions[char_id] = {"location_id": "home", "x": 0.0, "y": 0.0, "known_location_ids": ["home"]}
    valid_ids = {loc["id"] for loc in load_locations().get("locations", [])}
    old_known = [
        loc_id for loc_id in (positions[char_id].get("known_location_ids") or [])
        if loc_id in valid_ids
    ]
    current_location_id = positions[char_id].get("location_id")
    newly_known = []
    if current_location_id in valid_ids and current_location_id not in old_known:
        old_known.append(current_location_id)
        newly_known.append(current_location_id)
    positions[char_id]["known_location_ids"] = list(dict.fromkeys(old_known))
    save_character_positions(positions)
    return newly_known

def check_co_encounters(char_id, x, y, location_id):
    from app import get_char_name
    positions = load_character_positions()
    user_pos = load_user_position()
    encounters = []

    loc_name = location_id
    if location_id:
        loc = get_location_by_id(location_id)
        if loc:
            loc_name = loc.get("name", location_id)

    for cid, pos in positions.items():
        if cid == char_id:
            continue
        d = calc_distance(x, y, pos["x"], pos["y"])
        if d < 0.1:
            cname = get_char_name(cid)
            charname = get_char_name(char_id)
            encounters.append(cid)

    ud = calc_distance(x, y, user_pos["x"], user_pos["y"])
    if ud < 0.1:
        encounters.append("user")

    return encounters
