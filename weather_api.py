import json
import os
import time as time_module
import requests
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEATHER_CACHE_FILE = os.path.join(BASE_DIR, "configs", "weather_cache.json")
GEOCODE_CACHE_FILE = os.path.join(BASE_DIR, "configs", "geocode_cache.json")
CACHE_TTL_SECONDS = 900  # 15 minutes
GEOCODE_TTL_SECONDS = 86400 * 30  # 30 days for geocode cache

# WMO weather code -> Chinese description + emoji
WMO_CODE_MAP = {
    0:  ("晴", "☀️"),
    1:  ("晴", "🌤️"),
    2:  ("多云", "⛅"),
    3:  ("阴", "☁️"),
    45: ("雾", "🌫️"),
    48: ("雾凇", "🌫️"),
    51: ("小雨", "🌦️"),
    53: ("中雨", "🌧️"),
    55: ("大雨", "🌧️"),
    56: ("小冻雨", "🌨️"),
    57: ("冻雨", "🌨️"),
    61: ("小雨", "🌦️"),
    63: ("中雨", "🌧️"),
    65: ("大雨", "🌧️"),
    66: ("小冻雨", "🌨️"),
    67: ("冻雨", "🌨️"),
    71: ("小雪", "🌨️"),
    73: ("中雪", "🌨️"),
    75: ("大雪", "❄️"),
    77: ("雪粒", "🌨️"),
    80: ("阵雨", "🌦️"),
    81: ("中阵雨", "🌧️"),
    82: ("大阵雨", "🌧️"),
    85: ("小阵雪", "🌨️"),
    86: ("大阵雪", "❄️"),
    95: ("雷暴", "⛈️"),
    96: ("雷暴伴小冰雹", "⛈️"),
    99: ("雷暴伴大冰雹", "⛈️"),
}


def _load_cache():
    if os.path.exists(WEATHER_CACHE_FILE):
        try:
            with open(WEATHER_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_cache(data):
    os.makedirs(os.path.dirname(WEATHER_CACHE_FILE), exist_ok=True)
    try:
        with open(WEATHER_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def fetch_weather(lat, lon):
    """Fetch current weather from Open-Meteo API (free, no key required).
    Returns None on failure.
    """
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,"
                    "weather_code,wind_speed_10m",
        "timezone": "auto",
        "forecast_days": 1,
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        current = data.get("current", {})
        if not current:
            return None
        weather_code = current.get("weather_code", 0)
        desc, emoji = WMO_CODE_MAP.get(weather_code, ("未知", "🌡️"))
        return {
            "temperature": current.get("temperature_2m"),
            "humidity": current.get("relative_humidity_2m"),
            "wind_speed": current.get("wind_speed_10m"),
            "weather_code": weather_code,
            "condition": desc,
            "condition_en": _code_to_en(weather_code),
            "emoji": emoji,
            "fetched_at": datetime.now().isoformat(),
        }
    except Exception as e:
        print(f"[Weather] Fetch failed for ({lat},{lon}): {e}")
        return None


def _code_to_en(code):
    mapping = {
        0: "Clear", 1: "Clear", 2: "Partly Cloudy", 3: "Overcast",
        45: "Fog", 48: "Frost", 51: "Drizzle", 53: "Drizzle", 55: "Drizzle",
        61: "Rain", 63: "Rain", 65: "Rain",
        71: "Snow", 73: "Snow", 75: "Snow", 77: "Snow",
        80: "Showers", 81: "Showers", 82: "Showers",
        85: "Snow", 86: "Snow",
        95: "Thunderstorm", 96: "Thunderstorm", 99: "Thunderstorm",
    }
    return mapping.get(code, "Unknown")


def _load_geocode_cache():
    if os.path.exists(GEOCODE_CACHE_FILE):
        try:
            with open(GEOCODE_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_geocode_cache(data):
    os.makedirs(os.path.dirname(GEOCODE_CACHE_FILE), exist_ok=True)
    try:
        with open(GEOCODE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def geocode(city, country=None):
    """Geocode city name to lat/lon using OpenStreetMap Nominatim API (free, no key).
    Returns (lat, lon) or (None, None) on failure.
    """
    query = city if not country else f"{city}, {country}"
    cache_key = query.lower().strip()

    geo_cache = _load_geocode_cache()
    if cache_key in geo_cache:
        cached = geo_cache[cache_key]
        cached_at = cached.get("cached_at", "")
        if cached_at:
            try:
                age = (datetime.now() - datetime.fromisoformat(cached_at)).total_seconds()
                if age < GEOCODE_TTL_SECONDS:
                    return cached.get("lat"), cached.get("lon")
            except Exception:
                pass

    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": query, "format": "json", "limit": 1}
    headers = {"User-Agent": "KunigamiChat/1.0"}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data:
            r = data[0]
            lat = float(r.get("lat", 0))
            lon = float(r.get("lon", 0))
            geo_cache[cache_key] = {
                "lat": lat,
                "lon": lon,
                "name": r.get("display_name", city),
                "country": country or "",
                "cached_at": datetime.now().isoformat(),
            }
            _save_geocode_cache(geo_cache)
            print(f"[Geocode] '{query}' -> ({lat}, {lon})")
            return lat, lon
    except Exception as e:
        print(f"[Geocode] Failed for '{query}': {e}")
    return None, None


def get_weather_for_location(loc, force_refresh=False):
    """Get weather for a single location.
    Uses cache if data is fresh (<15 min). Returns None if no real_world.
    Auto-geocodes city name if lat/lon are missing.
    """
    rw = loc.get("real_world")
    if not rw:
        return None
    lat = rw.get("lat")
    lon = rw.get("lon")
    city = rw.get("city")

    if lat is None or lon is None:
        if not city:
            return None
        lat, lon = geocode(city, rw.get("country"))
        if lat is None or lon is None:
            return None
        rw["lat"] = lat
        rw["lon"] = lon

    cache_key = f"{lat:.4f},{lon:.4f}"
    cache = _load_cache()

    if not force_refresh and cache_key in cache:
        cached = cache[cache_key]
        fetched_at = cached.get("fetched_at")
        if fetched_at:
            try:
                dt = datetime.fromisoformat(fetched_at)
                age = (datetime.now() - dt).total_seconds()
                if age < CACHE_TTL_SECONDS:
                    cached["city"] = rw.get("city", "")
                    cached["country"] = rw.get("country", "")
                    return cached
            except Exception:
                pass

    weather = fetch_weather(lat, lon)
    if weather:
        weather["city"] = rw.get("city", "")
        weather["country"] = rw.get("country", "")
        cache[cache_key] = weather
        _save_cache(cache)

    return weather


def get_all_weather(locations, force_refresh=False):
    """Get weather for all locations that have real_world data."""
    result = {}
    for loc in locations:
        weather = get_weather_for_location(loc, force_refresh=force_refresh)
        if weather:
            result[loc["id"]] = weather
    return result


def weather_to_prompt_text(weather, lang="zh"):
    """Convert weather data to prompt text for AI character awareness."""
    if not weather:
        return ""
    temp = weather.get("temperature", "?")
    condition = weather.get("condition", "?")
    humidity = weather.get("humidity", "?")
    wind = weather.get("wind_speed", "?")
    city = weather.get("city", "")

    if lang == "ja":
        city_prefix = f"{city}の" if city else ""
        return f"{city_prefix}現在の天気：{condition}、気温{temp}°C、湿度{humidity}%、風速{wind}km/h"
    elif lang == "en":
        city_prefix = f" in {city}" if city else ""
        return f"Current weather{city_prefix}: {condition}, {temp}°C, humidity {humidity}%, wind {wind}km/h"
    else:
        city_prefix = f"{city}" if city else "当地"
        return f"{city_prefix}当前天气：{condition}，温度{temp}°C，湿度{humidity}%，风速{wind}km/h"


def refresh_all_weather():
    """Force refresh weather for all cached locations."""
    cache = _load_cache()
    locations = []
    for key in cache:
        parts = key.split(",")
        if len(parts) == 2:
            lat, lon = float(parts[0]), float(parts[1])
            locations.append((key, lat, lon))

    for key, lat, lon in locations:
        weather = fetch_weather(lat, lon)
        if weather:
            cache[key] = weather

    _save_cache(cache)
    print(f"[Weather] Refreshed {len(locations)} cached locations")


def refresh_weather_from_locations(locations):
    """Refresh weather for all locations with real_world data."""
    for loc in locations:
        get_weather_for_location(loc, force_refresh=True)
    print(f"[Weather] Refreshed locations with real_world data")
