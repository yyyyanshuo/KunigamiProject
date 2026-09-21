"""Structured AI actions for persona, relationship, and plan documents."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from services.persona_locks import (
    PersonaLockError,
    ensure_model_preserved_locks,
    persona_locked_ranges,
    validate_persona_locks,
)
from services.schedule import UNDATED_SCHEDULE_KEY, normalize_schedule_data


PERSONA_ACTIONS = frozenset({
    "ADD_PERSONA", "DELETE_PERSONA", "EDIT_PERSONA", "REWRITE_PERSONA",
})
RELATION_ACTIONS = frozenset({
    "ADD_RELATION", "DELETE_RELATION", "EDIT_RELATION", "REWRITE_RELATION",
})
PLAN_ACTIONS = frozenset({
    "ADD_PLAN", "DELETE_PLAN", "EDIT_PLAN", "REWRITE_PLAN",
})
CONTENT_ACTION_NAMES = PERSONA_ACTIONS | RELATION_ACTIONS | PLAN_ACTIONS


class ContentActionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ParsedContentAction:
    name: str
    payload: Any
    start: int
    end: int


_ACTION_START_RE = re.compile(
    r"\[(" + "|".join(sorted(CONTENT_ACTION_NAMES, key=len, reverse=True)) + r")\s*:\s*",
    re.IGNORECASE,
)
_JSON_DECODER = json.JSONDecoder()


def extract_content_actions(text: object) -> tuple[list[ParsedContentAction], str]:
    """Extract valid content action tags using a real nested-JSON parser."""

    source = "" if text is None else str(text)
    actions: list[ParsedContentAction] = []
    consumed: list[tuple[int, int]] = []
    cursor = 0
    while True:
        match = _ACTION_START_RE.search(source, cursor)
        if not match:
            break
        payload_start = match.end()
        try:
            payload, payload_end = _JSON_DECODER.raw_decode(source, payload_start)
        except json.JSONDecodeError:
            cursor = match.end()
            continue
        end = payload_end
        while end < len(source) and source[end].isspace():
            end += 1
        if end >= len(source) or source[end] != "]":
            cursor = match.end()
            continue
        end += 1
        actions.append(ParsedContentAction(match.group(1).upper(), payload, match.start(), end))
        consumed.append((match.start(), end))
        cursor = end

    cleaned = source
    for start, end in reversed(consumed):
        cleaned = cleaned[:start] + cleaned[end:]
    return actions, cleaned


def _object(payload: Any, action: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ContentActionError("invalid_payload", f"{action} 参数必须是 JSON 对象")
    return payload


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContentActionError("invalid_payload", f"{field} 必须是非空文本")
    return value


def _append_text(original: str, addition: str) -> str:
    if not original:
        return addition
    if original.endswith("\n\n"):
        separator = ""
    elif original.endswith(("\n", "\r")):
        separator = "\n"
    else:
        separator = "\n\n"
    return original + separator + addition


def _first_unlocked_match(text: str, old: str) -> int:
    index = text.find(old)
    if index < 0:
        raise ContentActionError("old_text_not_found", "没有找到需要修改的原文")
    match_end = index + len(old)
    for lock_start, lock_end in persona_locked_ranges(text):
        if index < lock_end and match_end > lock_start:
            raise ContentActionError("locked_content", "首次匹配内容位于 LOCK 区块内")
    return index


def apply_persona_action(current: str, action: str, payload: Any) -> str:
    data = _object(payload, action)
    if action == "ADD_PERSONA":
        updated = _append_text(current, _text(data.get("content"), "content"))
    elif action == "DELETE_PERSONA":
        old = _text(data.get("old"), "old")
        index = _first_unlocked_match(current, old)
        updated = current[:index] + current[index + len(old):]
    elif action == "EDIT_PERSONA":
        old = _text(data.get("old"), "old")
        new = _text(data.get("new"), "new")
        index = _first_unlocked_match(current, old)
        updated = current[:index] + new + current[index + len(old):]
    elif action == "REWRITE_PERSONA":
        updated = _text(data.get("content"), "content")
        try:
            validate_persona_locks(updated)
            ensure_model_preserved_locks(current, updated)
        except PersonaLockError as exc:
            raise ContentActionError("rewrite_missing_locked_block", str(exc)) from exc
    else:
        raise ContentActionError("unknown_action", action)

    try:
        validate_persona_locks(updated)
    except PersonaLockError as exc:
        raise ContentActionError("invalid_persona_lock_markup", str(exc)) from exc
    return updated


def _relation_key(graph: dict[str, Any], target: str) -> str | None:
    if target in graph:
        return target
    folded = target.casefold()
    for key in graph:
        if str(key).casefold() == folded:
            return key
    return None


def _relation_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        value = {"role": "", "score": 1, "description": str(value or "")}
    return {
        "role": str(value.get("role") or "").strip(),
        "score": value.get("score", 1),
        "description": str(value.get("description") or value.get("desc") or "").strip(),
    }


def _score(value: Any) -> int | float:
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise ContentActionError("invalid_relation_score", "关系分数必须是 0 到 5 的数字") from exc
    if not 0 <= score <= 5:
        raise ContentActionError("invalid_relation_score", "关系分数必须位于 0 到 5")
    return int(score) if score.is_integer() else score


def apply_relation_action(graph: dict[str, Any], action: str, payload: Any) -> dict[str, Any]:
    result = {str(key): _relation_record(value) for key, value in graph.items()}

    if action == "REWRITE_RELATION":
        if isinstance(payload, list):
            if len(payload) != 4:
                raise ContentActionError(
                    "invalid_payload",
                    "REWRITE_RELATION 位置参数必须依次提供 target、role、score、description",
                )
            target = _text(payload[0], "target").strip()
            fields = {"role": payload[1], "score": payload[2], "description": payload[3]}
        else:
            data = _object(payload, action)
            target = _text(data.get("target"), "target").strip()
            fields = {key: data[key] for key in ("role", "score", "description") if key in data}
            if not fields:
                raise ContentActionError("invalid_payload", "至少需要提供 role、score、description 中的一项")

        key = _relation_key(result, target)
        if key is None:
            if set(fields) != {"role", "score", "description"}:
                raise ContentActionError("target_not_found", "新关系必须完整提供 role、score、description")
            key = target
            record = {"role": "", "score": 1, "description": ""}
        else:
            record = result[key]
        if "role" in fields:
            if not isinstance(fields["role"], str):
                raise ContentActionError("invalid_payload", "role 必须是文本")
            record["role"] = fields["role"].strip()
        if "score" in fields:
            record["score"] = _score(fields["score"])
        if "description" in fields:
            if not isinstance(fields["description"], str):
                raise ContentActionError("invalid_payload", "description 必须是文本")
            record["description"] = fields["description"].strip()
        result[key] = record
        return result

    data = _object(payload, action)
    target = _text(data.get("target"), "target").strip()
    key = _relation_key(result, target)
    if key is None:
        raise ContentActionError("target_not_found", f"关系对象“{target}”不存在")
    record = result[key]
    description = record["description"]
    if action == "ADD_RELATION":
        record["description"] = _append_text(description, _text(data.get("content"), "content"))
    elif action == "DELETE_RELATION":
        old = _text(data.get("old"), "old")
        index = description.find(old)
        if index < 0:
            raise ContentActionError("old_text_not_found", "关系描述中没有找到需要删除的原文")
        record["description"] = description[:index] + description[index + len(old):]
    elif action == "EDIT_RELATION":
        old = _text(data.get("old"), "old")
        new = _text(data.get("new"), "new")
        index = description.find(old)
        if index < 0:
            raise ContentActionError("old_text_not_found", "关系描述中没有找到需要修改的原文")
        record["description"] = description[:index] + new + description[index + len(old):]
    else:
        raise ContentActionError("unknown_action", action)
    return result


def _date(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContentActionError("invalid_date", f"{field} 必须是 YYYY-MM-DD 日期")
    text = value.strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise ContentActionError("invalid_date", f"{field} 必须是 YYYY-MM-DD 日期") from exc
    if parsed.strftime("%Y-%m-%d") != text:
        raise ContentActionError("invalid_date", f"{field} 必须是 YYYY-MM-DD 日期")
    return text


def _location(data: dict[str, Any], field: str) -> str | None:
    if "date" not in data or data.get("date") is None:
        return None
    return _date(data.get("date"), field)


def _bucket(schedule: dict[str, Any], date_value: str | None) -> list[str]:
    return schedule.setdefault(UNDATED_SCHEDULE_KEY if date_value is None else date_value, [])


def _find_plan(schedule: dict[str, Any], locator: dict[str, Any], field: str) -> tuple[str | None, list[str], int, str]:
    date_value = _location(locator, f"{field}.date")
    content = _text(locator.get("content"), f"{field}.content").strip()
    items = _bucket(schedule, date_value)
    try:
        index = items.index(content)
    except ValueError as exc:
        label = "无时间计划" if date_value is None else date_value
        raise ContentActionError("old_text_not_found", f"{label} 中没有找到指定计划") from exc
    return date_value, items, index, content


def apply_plan_action(current: dict[str, Any], action: str, payload: Any) -> dict[str, Any]:
    schedule = normalize_schedule_data(current)
    data = _object(payload, action)

    if action == "ADD_PLAN":
        date_value = _location(data, "date")
        content = _text(data.get("content"), "content").strip()
        items = _bucket(schedule, date_value)
        if content not in items:
            items.append(content)
        return schedule

    if action == "DELETE_PLAN":
        date_value, items, index, _ = _find_plan(schedule, data, action)
        items.pop(index)
        if date_value is not None and not items:
            schedule.pop(date_value, None)
        return schedule

    if action == "EDIT_PLAN":
        source = _object(data.get("from"), "EDIT_PLAN.from")
        changes = _object(data.get("set"), "EDIT_PLAN.set")
        if not any(key in changes for key in ("date", "content")):
            raise ContentActionError("invalid_payload", "EDIT_PLAN.set 至少需要 date 或 content")
        old_date, old_items, index, old_content = _find_plan(schedule, source, "from")
        new_date = old_date
        if "date" in changes:
            new_date = None if changes.get("date") is None else _date(changes.get("date"), "set.date")
        new_content = old_content
        if "content" in changes:
            new_content = _text(changes.get("content"), "set.content").strip()

        old_items.pop(index)
        if old_date is not None and not old_items:
            schedule.pop(old_date, None)
        _bucket(schedule, new_date).append(new_content)
        return schedule

    if action == "REWRITE_PLAN":
        locator = _object(data.get("at"), "REWRITE_PLAN.at")
        _, items, index, _ = _find_plan(schedule, locator, "at")
        items[index] = _text(data.get("content"), "content").strip()
        return schedule

    raise ContentActionError("unknown_action", action)
