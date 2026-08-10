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
    get_location_at_coord,
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


def get_location_at_coord(x, y):
    locs = load_locations()
    best = None
    best_dist = float("inf")
    for loc in locs.get("locations", []):
        d = calc_distance(x, y, loc["x"], loc["y"])
        if d < best_dist:
            best_dist = d
            best = loc
    if best and best_dist < 0.1:
        return best
    return None

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
    x = float(data.get("x", 0))
    y = float(data.get("y", 0))
    if not loc_id or not name:
        return jsonify({"error": "id and name are required"}), 400
    locs = load_locations()
    for loc in locs.get("locations", []):
        if loc["id"] == loc_id:
            return jsonify({"error": f"location id '{loc_id}' already exists"}), 400
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

    positions = load_character_positions()
    for cid in positions:
        cx, cy = positions[cid]["x"], positions[cid]["y"]
        d = calc_distance(cx, cy, x, y)
        if d < 1.0:
            known = positions[cid].get("known_location_ids", [])
            if loc_id not in known:
                known.append(loc_id)
                positions[cid]["known_location_ids"] = known
    save_character_positions(positions)

    return jsonify(new_loc), 201

@map_bp.route("/api/map/locations/<loc_id>", methods=["PUT"])
def api_update_location(loc_id):
    data = request.json or {}
    locs = load_locations()
    for loc in locs.get("locations", []):
        if loc["id"] == loc_id:
            if "name" in data:
                loc["name"] = data["name"].strip()
            if "description" in data:
                loc["description"] = data["description"].strip()
            if "real_world" in data:
                rw = data["real_world"]
                if rw is not None:
                    rw["lat"] = float(rw.get("lat", 0)) if rw.get("lat") is not None else None
                    rw["lon"] = float(rw.get("lon", 0)) if rw.get("lon") is not None else None
                    try:
                        rw = normalize_real_world_timezone(rw)
                    except ValueError as e:
                        return jsonify({"error": str(e)}), 400
                loc["real_world"] = rw
            if "x" in data or "y" in data:
                old_x, old_y = loc["x"], loc["y"]
                loc["x"] = float(data.get("x", loc["x"]))
                loc["y"] = float(data.get("y", loc["y"]))
                loc["r"] = round(math.sqrt(loc["x"]**2 + loc["y"]**2), 4)
                loc["theta"] = round(math.atan2(loc["y"], loc["x"]), 4)
                # 更新在该地点上的所有角色的坐标
                positions = load_character_positions()
                for cid, pos in positions.items():
                    if pos.get("location_id") == loc_id:
                        pos["x"] = loc["x"]
                        pos["y"] = loc["y"]
                # 也移动在该地点上的用户
                user_pos = load_user_position()
                if user_pos.get("location_id") == loc_id:
                    user_pos["x"] = loc["x"]
                    user_pos["y"] = loc["y"]
                    save_user_position(user_pos)
                # 更新所有角色的认知范围
                for cid, pos in positions.items():
                    known = pos.get("known_location_ids", [])
                    d = calc_distance(pos["x"], pos["y"], loc["x"], loc["y"])
                    if d < 1.0 and loc_id not in known:
                        known.append(loc_id)
                        pos["known_location_ids"] = known
                    elif d >= 1.0 and loc_id in known:
                        known.remove(loc_id)
                        pos["known_location_ids"] = known
                save_character_positions(positions)
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
    return jsonify({"error": "location not found"}), 404

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
    known_ids = [
        loc_id for loc_id in (data.get("known_location_ids") or [])
        if loc_id in valid_location_ids
    ]
    positions = load_character_positions()
    if char_id not in positions:
        positions[char_id] = {"location_id": "home", "x": 0.0, "y": 0.0, "known_location_ids": ["home"]}
    current_location_id = positions[char_id].get("location_id")
    if current_location_id and current_location_id not in known_ids:
        known_ids.append(current_location_id)
    positions[char_id]["known_location_ids"] = list(dict.fromkeys(known_ids))
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
    locs = load_locations()
    positions = load_character_positions()
    if char_id not in positions:
        positions[char_id] = {"location_id": "home", "x": 0.0, "y": 0.0, "known_location_ids": ["home"]}
    cx, cy = positions[char_id]["x"], positions[char_id]["y"]
    new_known = []
    for loc in locs.get("locations", []):
        d = calc_distance(cx, cy, loc["x"], loc["y"])
        if d < 1.0:
            new_known.append(loc["id"])
    old_known = set(positions[char_id].get("known_location_ids", []))
    newly_known = set(new_known) - old_known
    positions[char_id]["known_location_ids"] = new_known
    save_character_positions(positions)
    return list(newly_known)

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
