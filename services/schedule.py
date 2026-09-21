"""Shared helpers for the JSON-backed character schedule.

Canonical shape::

    {"2026-08-20": ["plan one", "plan two"], "_undated": ["someday"]}

Legacy dated string values are migrated as one intact item.  They are never
split on punctuation because semicolons may be part of the user's prose.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from services.memory_store import atomic_write_json, load_json_object, memory_file_lock


UNDATED_SCHEDULE_KEY = "_undated"


class ScheduleValidationError(ValueError):
    """Raised when schedule data cannot be represented safely."""


def normalize_schedule_data(value: Any) -> dict[str, Any]:
    """Return the canonical list-per-date schedule shape."""

    if not isinstance(value, dict):
        raise ScheduleValidationError("日程数据必须是 JSON 对象")

    normalized: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key or "").strip()
        if not key or key == UNDATED_SCHEDULE_KEY:
            continue
        if isinstance(raw_value, str):
            raw_items = [raw_value]
        elif isinstance(raw_value, list):
            raw_items = raw_value
        else:
            raise ScheduleValidationError(f"日程 {key} 必须是文本或文本数组")
        items: list[str] = []
        for item in raw_items:
            if not isinstance(item, str):
                raise ScheduleValidationError(f"日程 {key} 的每一项都必须是文本")
            content = item.strip()
            if content:
                items.append(content)
        if items:
            normalized[key] = items

    raw_undated = value.get(UNDATED_SCHEDULE_KEY, [])
    if raw_undated is None:
        raw_undated = []
    if not isinstance(raw_undated, list):
        raise ScheduleValidationError("_undated 必须是文本数组")

    undated: list[str] = []
    for item in raw_undated:
        if not isinstance(item, str):
            raise ScheduleValidationError("无时间计划的每一项都必须是文本")
        content = item.strip()
        if content:
            undated.append(content)

    normalized[UNDATED_SCHEDULE_KEY] = undated
    return normalized


def append_schedule_item(path: str, date_value: Any, content_value: Any) -> tuple[bool, str]:
    """Append one dated or undated item and return ``(changed, category)``."""

    if not isinstance(content_value, str) or not content_value.strip():
        raise ScheduleValidationError("日程内容不能为空")
    content = content_value.strip()

    date_text = "" if date_value is None else str(date_value).strip()
    if date_text:
        try:
            parsed = datetime.strptime(date_text, "%Y-%m-%d")
        except ValueError as exc:
            raise ScheduleValidationError("日程日期必须使用 YYYY-MM-DD 格式") from exc
        if parsed.strftime("%Y-%m-%d") != date_text:
            raise ScheduleValidationError("日程日期必须使用 YYYY-MM-DD 格式")

    with memory_file_lock(path):
        data = normalize_schedule_data(load_json_object(path))
        if not date_text:
            undated = data[UNDATED_SCHEDULE_KEY]
            if content in undated:
                return False, "undated"
            undated.append(content)
            category = "undated"
        else:
            dated = data.setdefault(date_text, [])
            if content in dated:
                return False, "dated"
            dated.append(content)
            category = "dated"

        atomic_write_json(path, data)
        return True, category
