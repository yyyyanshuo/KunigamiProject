"""Timezone-aware helpers for character, user, map, and sleep time.

Persistent application timestamps remain Beijing wall-clock strings for
backwards compatibility.  This module is the single conversion boundary
between those legacy values and timezone-aware datetimes.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones


UTC = timezone.utc
BEIJING_TZ_NAME = "Asia/Shanghai"
TOKYO_TZ_NAME = "Asia/Tokyo"
BEIJING_TZ = ZoneInfo(BEIJING_TZ_NAME)
TOKYO_TZ = ZoneInfo(TOKYO_TZ_NAME)


def is_valid_timezone(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        ZoneInfo(value.strip())
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def get_zone(value: Any, fallback: str = BEIJING_TZ_NAME) -> ZoneInfo:
    name = value.strip() if isinstance(value, str) else ""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(fallback)


def utc_now() -> datetime:
    return datetime.now(UTC)


def beijing_now(now: Optional[datetime] = None) -> datetime:
    current = now or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=BEIJING_TZ)
    return current.astimezone(BEIJING_TZ)


def default_character_timezone(language: Any) -> str:
    lang = str(language or "").strip().lower()
    return TOKYO_TZ_NAME if lang in {"ja", "jp", "japanese", "日本語"} else BEIJING_TZ_NAME


def get_user_timezone(settings: Optional[Dict[str, Any]]) -> str:
    value = (settings or {}).get("timezone")
    return value.strip() if is_valid_timezone(value) else BEIJING_TZ_NAME


def get_character_timezone(info: Optional[Dict[str, Any]]) -> str:
    data = info or {}
    value = data.get("timezone")
    if is_valid_timezone(value):
        return value.strip()
    return default_character_timezone(data.get("language"))


def ensure_character_time_defaults(
    info: Dict[str, Any],
    *,
    existing_character: bool = True,
) -> bool:
    """Fill missing time metadata without changing valid existing values.

    Existing characters historically used the server/Beijing clock for their
    sleep settings, so their missing basis migrates to ``user``.  Brand-new
    characters use their own local timezone.
    """

    changed = False
    if not is_valid_timezone(info.get("timezone")):
        info["timezone"] = default_character_timezone(info.get("language"))
        info["timezone_source"] = (
            "language_default"
            if info["timezone"] == TOKYO_TZ_NAME
            else "system_default"
        )
        changed = True
    elif info.get("timezone_source") not in {
        "system_default",
        "language_default",
        "manual",
        "location",
    }:
        info["timezone_source"] = "manual"
        changed = True

    if info.get("ds_time_basis") not in {"character", "user"}:
        info["ds_time_basis"] = "user" if existing_character else "character"
        changed = True
    if not info.get("ds_set_by"):
        info["ds_set_by"] = "legacy" if existing_character else "default"
        changed = True
    if not is_valid_timezone(info.get("ds_timezone_at_set")):
        info["ds_timezone_at_set"] = (
            BEIJING_TZ_NAME
            if existing_character
            else get_character_timezone(info)
        )
        changed = True
    if "deep_sleep_source" not in info:
        info["deep_sleep_source"] = "legacy" if existing_character else "schedule"
        changed = True
    if "sleep_manual_override" not in info:
        info["sleep_manual_override"] = False
        changed = True
    return changed


def parse_hhmm(value: Any) -> Optional[time]:
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour, minute)


def parse_beijing_timestamp(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(value.strip(), fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=BEIJING_TZ)
    return parsed.astimezone(BEIJING_TZ)


def format_beijing_timestamp(value: datetime, *, seconds: bool = True) -> str:
    aware = value
    if aware.tzinfo is None:
        aware = aware.replace(tzinfo=BEIJING_TZ)
    fmt = "%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M"
    return aware.astimezone(BEIJING_TZ).strftime(fmt)


def convert_beijing_to_timezone(value: Any, timezone_name: str) -> Optional[datetime]:
    parsed = parse_beijing_timestamp(value)
    if parsed is None:
        return None
    return parsed.astimezone(get_zone(timezone_name))


def _roundtrip_matches(candidate: datetime, naive_local: datetime) -> bool:
    return (
        candidate.astimezone(UTC)
        .astimezone(candidate.tzinfo)
        .replace(tzinfo=None)
        == naive_local
    )


def resolve_local_datetime(
    local_date: date,
    local_time: time,
    timezone_name: str,
) -> datetime:
    """Resolve a wall-clock value in an IANA zone.

    Ambiguous fall-back times use the first occurrence.  Non-existent
    spring-forward times advance to the first valid local minute.
    """

    zone = get_zone(timezone_name)
    naive = datetime.combine(local_date, local_time)
    first = naive.replace(tzinfo=zone, fold=0)
    if _roundtrip_matches(first, naive):
        return first
    second = naive.replace(tzinfo=zone, fold=1)
    if _roundtrip_matches(second, naive):
        return second

    probe = naive
    for _ in range(180):
        probe += timedelta(minutes=1)
        candidate = probe.replace(tzinfo=zone, fold=0)
        if _roundtrip_matches(candidate, probe):
            return candidate
    return first


def sleep_source_timezone(
    char_info: Dict[str, Any],
    user_settings: Optional[Dict[str, Any]],
) -> str:
    if char_info.get("ds_time_basis") == "character":
        return get_character_timezone(char_info)
    return get_user_timezone(user_settings)


def sleep_event_datetime(
    char_info: Dict[str, Any],
    user_settings: Optional[Dict[str, Any]],
    local_date: date,
    event_type: str,
) -> Optional[datetime]:
    field = "ds_start" if event_type == "sleep" else "ds_end"
    wall_time = parse_hhmm(char_info.get(field))
    if wall_time is None:
        return None
    source_tz = sleep_source_timezone(char_info, user_settings)
    return resolve_local_datetime(local_date, wall_time, source_tz)


def sleep_event_key(
    char_info: Dict[str, Any],
    user_settings: Optional[Dict[str, Any]],
    local_date: date,
    event_type: str,
) -> str:
    field = "ds_start" if event_type == "sleep" else "ds_end"
    return "|".join(
        (
            event_type,
            sleep_source_timezone(char_info, user_settings),
            local_date.isoformat(),
            str(char_info.get(field) or ""),
        )
    )


def sleep_preview(
    char_info: Dict[str, Any],
    user_settings: Optional[Dict[str, Any]],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    current = now or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    source_name = sleep_source_timezone(char_info, user_settings)
    source_now = current.astimezone(get_zone(source_name))
    char_zone = get_zone(get_character_timezone(char_info))
    user_zone = get_zone(get_user_timezone(user_settings))
    result: Dict[str, Any] = {
        "basis": char_info.get("ds_time_basis", "user"),
        "source_timezone": source_name,
        "character_timezone": char_zone.key,
        "user_timezone": user_zone.key,
    }
    for event_type in ("sleep", "wake"):
        candidate = sleep_event_datetime(
            char_info, user_settings, source_now.date(), event_type
        )
        if candidate is None:
            result[event_type] = None
            continue
        if candidate <= current:
            candidate = sleep_event_datetime(
                char_info,
                user_settings,
                source_now.date() + timedelta(days=1),
                event_type,
            )
        result[event_type] = {
            "source": candidate.strftime("%Y-%m-%d %H:%M"),
            "beijing": candidate.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M"),
            "character": candidate.astimezone(char_zone).strftime("%Y-%m-%d %H:%M"),
            "user": candidate.astimezone(user_zone).strftime("%Y-%m-%d %H:%M"),
        }
    return result


@lru_cache(maxsize=4096)
def resolve_timezone_from_coordinates(lat: float, lon: float) -> Optional[str]:
    try:
        from timezonefinder import TimezoneFinder

        finder = TimezoneFinder(in_memory=True)
        value = finder.timezone_at(lat=float(lat), lng=float(lon))
        return value if is_valid_timezone(value) else None
    except Exception as exc:
        print(f"[Timezone] coordinate lookup failed: {exc}")
        return None


def search_timezones(query: str = "", limit: int = 100) -> list[str]:
    needle = str(query or "").strip().lower()
    values = sorted(available_timezones())
    if needle:
        values = [value for value in values if needle in value.lower()]
    return values[: max(1, min(int(limit or 100), 500))]

