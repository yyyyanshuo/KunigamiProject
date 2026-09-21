#!/usr/bin/env python3
"""Merge duplicate-coordinate locations and reset character location knowledge.

The script is dry-run by default. Duplicate groups require an explicit keeper
choice, either interactively or through a JSON choices file. Run ``--apply``
only while the application is stopped or in a maintenance window.

Examples:
    python scripts/cleanup_map_locations.py --user 1
    python scripts/cleanup_map_locations.py --user 1 --interactive --apply
    python scripts/cleanup_map_locations.py --all-users --choices choices.json --apply

Choices JSON format (group keys are printed by the dry run):
    {"1": {"home|loc_123": "home"}}
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_USERS_ROOT = PROJECT_ROOT / "users"
COORDINATE_TOLERANCE = 1e-9


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return copy.deepcopy(default)
    with path.open("r", encoding="utf-8-sig") as file:
        return json.load(file)


def atomic_save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def coordinates_equal(first: dict[str, Any], second: dict[str, Any]) -> bool:
    try:
        return (
            abs(float(first.get("x")) - float(second.get("x")))
            <= COORDINATE_TOLERANCE
            and abs(float(first.get("y")) - float(second.get("y")))
            <= COORDINATE_TOLERANCE
        )
    except (TypeError, ValueError):
        return False


def duplicate_groups(locations: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    consumed: set[int] = set()
    for index, location in enumerate(locations):
        if index in consumed or not isinstance(location, dict):
            continue
        group = [location]
        for other_index in range(index + 1, len(locations)):
            if other_index in consumed:
                continue
            other = locations[other_index]
            if isinstance(other, dict) and coordinates_equal(location, other):
                group.append(other)
                consumed.add(other_index)
        if len(group) > 1:
            groups.append(group)
    return groups


def duplicate_group_key(group: list[dict[str, Any]]) -> str:
    return "|".join(sorted(str(location.get("id")) for location in group))


def deduplicate(values: list[Any]) -> list[Any]:
    return list(dict.fromkeys(values))


def reference_counts(
    group: list[dict[str, Any]],
    positions: dict[str, Any],
    user_position: dict[str, Any],
    characters: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for location in group:
        location_id = str(location.get("id"))
        current_characters = [
            char_id
            for char_id, position in positions.items()
            if isinstance(position, dict) and position.get("location_id") == location_id
        ]
        known_by = [
            char_id
            for char_id, position in positions.items()
            if isinstance(position, dict)
            and location_id in (position.get("known_location_ids") or [])
        ]
        timezone_by = [
            char_id
            for char_id, character in characters.items()
            if isinstance(character, dict)
            and character.get("timezone_location_id") == location_id
        ]
        result[location_id] = {
            "current_characters": current_characters,
            "known_by": known_by,
            "timezone_by": timezone_by,
            "user_here": user_position.get("location_id") == location_id,
        }
    return result


def merge_and_reset(
    locations_data: dict[str, Any],
    positions: dict[str, Any],
    user_position: dict[str, Any],
    characters: dict[str, Any],
    keepers: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    updated_locations = copy.deepcopy(locations_data)
    updated_positions = copy.deepcopy(positions)
    updated_user = copy.deepcopy(user_position)
    updated_characters = copy.deepcopy(characters)
    locations = updated_locations.get("locations")
    if not isinstance(locations, list):
        raise ValueError("locations.json format is invalid")

    groups = duplicate_groups(locations)
    replacements: dict[str, str] = {}
    merged_groups = 0
    removed_locations = 0
    for group in groups:
        key = duplicate_group_key(group)
        keeper_id = keepers.get(key)
        if not keeper_id:
            continue
        ids = {str(location.get("id")) for location in group}
        if keeper_id not in ids:
            raise ValueError(f"keeper {keeper_id!r} is not in duplicate group {key}")
        if "home" in ids and keeper_id != "home":
            raise ValueError(f"duplicate group {key} must keep default location 'home'")
        for location_id in ids:
            if location_id != keeper_id:
                replacements[location_id] = keeper_id
                removed_locations += 1
        merged_groups += 1

    if replacements:
        updated_locations["locations"] = [
            location
            for location in locations
            if str(location.get("id")) not in replacements
        ]

    remaining_by_id = {
        str(location.get("id")): location
        for location in updated_locations.get("locations", [])
        if isinstance(location, dict) and location.get("id") is not None
    }
    reset_characters = 0
    remapped_current_positions = 0
    for position in updated_positions.values():
        if not isinstance(position, dict):
            continue
        old_location_id = position.get("location_id")
        new_location_id = replacements.get(old_location_id, old_location_id)
        if new_location_id not in remaining_by_id:
            new_location_id = None
        if new_location_id != old_location_id:
            remapped_current_positions += 1
        position["location_id"] = new_location_id
        if new_location_id:
            keeper = remaining_by_id[new_location_id]
            position["x"] = float(keeper["x"])
            position["y"] = float(keeper["y"])
            new_known = [new_location_id]
        else:
            new_known = []
        if position.get("known_location_ids") != new_known:
            reset_characters += 1
        position["known_location_ids"] = new_known

    old_user_location = updated_user.get("location_id")
    new_user_location = replacements.get(old_user_location, old_user_location)
    if new_user_location not in remaining_by_id:
        new_user_location = None
    updated_user["location_id"] = new_user_location
    if new_user_location:
        keeper = remaining_by_id[new_user_location]
        updated_user["x"] = float(keeper["x"])
        updated_user["y"] = float(keeper["y"])

    remapped_timezones = 0
    for character in updated_characters.values():
        if not isinstance(character, dict):
            continue
        old_timezone_location = character.get("timezone_location_id")
        if old_timezone_location in replacements:
            character["timezone_location_id"] = replacements[old_timezone_location]
            remapped_timezones += 1

    summary = {
        "duplicate_groups": len(groups),
        "merged_groups": merged_groups,
        "removed_locations": removed_locations,
        "reset_characters": reset_characters,
        "remapped_current_positions": remapped_current_positions,
        "remapped_user_position": old_user_location != new_user_location,
        "remapped_timezones": remapped_timezones,
        "unresolved_groups": [
            duplicate_group_key(group)
            for group in groups
            if duplicate_group_key(group) not in keepers
        ],
    }
    return (
        updated_locations,
        updated_positions,
        updated_user,
        updated_characters,
        summary,
    )


def load_choices(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    value = load_json(path, {})
    if not isinstance(value, dict):
        raise ValueError("choices file must contain a JSON object")
    result: dict[str, dict[str, str]] = {}
    for user_id, choices in value.items():
        if not isinstance(choices, dict):
            raise ValueError(f"choices for user {user_id} must be an object")
        result[str(user_id)] = {
            str(group_key): str(keeper_id)
            for group_key, keeper_id in choices.items()
        }
    return result


def print_duplicate_group(
    user_id: str,
    group: list[dict[str, Any]],
    refs: dict[str, dict[str, Any]],
) -> None:
    first = group[0]
    print(
        f"\n用户 {user_id} 重复组 {duplicate_group_key(group)} "
        f"坐标=({first.get('x')}, {first.get('y')})"
    )
    for index, location in enumerate(group, 1):
        location_id = str(location.get("id"))
        info = refs[location_id]
        real_world = location.get("real_world")
        print(
            f"  [{index}] {location.get('name') or '(未命名)'} [id={location_id}] "
            f"default={bool(location.get('is_default'))}"
        )
        print(f"      描述: {location.get('description') or '(无)'}")
        print(f"      真实地点: {real_world if real_world else '(无)'}")
        print(
            "      引用: 当前角色="
            f"{info['current_characters'] or '无'}，用户在此={info['user_here']}，"
            f"认知角色={info['known_by'] or '无'}，时区角色={info['timezone_by'] or '无'}"
        )


def choose_interactively(
    user_id: str,
    groups: list[list[dict[str, Any]]],
    positions: dict[str, Any],
    user_position: dict[str, Any],
    characters: dict[str, Any],
    existing_choices: dict[str, str],
) -> dict[str, str]:
    choices = dict(existing_choices)
    for group in groups:
        key = duplicate_group_key(group)
        if key in choices:
            continue
        refs = reference_counts(group, positions, user_position, characters)
        print_duplicate_group(user_id, group, refs)
        ids = [str(location.get("id")) for location in group]
        if "home" in ids:
            choices[key] = "home"
            print("  包含默认地点 home，已强制保留 home。")
            continue
        while True:
            answer = input("  请选择保留序号，或输入 s 跳过此组: ").strip().lower()
            if answer == "s":
                break
            try:
                selected = int(answer)
            except ValueError:
                selected = 0
            if 1 <= selected <= len(ids):
                choices[key] = ids[selected - 1]
                break
            print("  输入无效，请重新选择。")
    return choices


def selected_user_dirs(users_root: Path, users: list[str] | None) -> list[Path]:
    if users:
        return [users_root / str(user_id) for user_id in deduplicate(users)]
    return sorted(
        (path for path in users_root.iterdir() if path.is_dir()),
        key=lambda path: path.name,
    )


def backup_files(paths: list[Path], users_root: Path, backup_root: Path) -> None:
    for path in paths:
        if not path.exists():
            continue
        destination = backup_root / path.relative_to(users_root.parent)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--user", action="append", dest="users", help="用户 ID；可重复")
    scope.add_argument("--all-users", action="store_true", help="处理全部用户")
    parser.add_argument("--users-root", type=Path, default=DEFAULT_USERS_ROOT)
    parser.add_argument("--choices", type=Path, help="重复地点保留选择 JSON")
    parser.add_argument("--interactive", action="store_true", help="逐组交互选择保留地点")
    parser.add_argument("--apply", action="store_true", help="实际写入；默认仅预览")
    parser.add_argument("--backup-dir", type=Path, help="备份父目录")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    users_root = args.users_root.resolve()
    if not users_root.is_dir():
        print(f"错误：用户目录不存在：{users_root}")
        return 2
    if args.apply and not (args.users or args.all_users):
        print("错误：实际写入时必须明确指定 --user 或 --all-users。")
        return 2

    try:
        choices_by_user = load_choices(args.choices.resolve() if args.choices else None)
    except Exception as exc:
        print(f"错误：无法读取选择文件：{exc}")
        return 2

    plans: list[dict[str, Any]] = []
    for user_dir in selected_user_dirs(users_root, args.users):
        config_dir = user_dir / "configs"
        paths = {
            "locations": config_dir / "locations.json",
            "positions": config_dir / "character_positions.json",
            "user": config_dir / "user_position.json",
            "characters": config_dir / "characters.json",
        }
        if not paths["locations"].exists() or not paths["positions"].exists():
            print(f"用户 {user_dir.name}：缺少地图数据，跳过。")
            continue
        try:
            original = {
                "locations": load_json(paths["locations"], {"locations": []}),
                "positions": load_json(paths["positions"], {}),
                "user": load_json(paths["user"], {}),
                "characters": load_json(paths["characters"], {}),
            }
            groups = duplicate_groups(original["locations"].get("locations", []))
            user_choices = dict(choices_by_user.get(user_dir.name, {}))
            if args.interactive:
                user_choices = choose_interactively(
                    user_dir.name,
                    groups,
                    original["positions"],
                    original["user"],
                    original["characters"],
                    user_choices,
                )
            else:
                for group in groups:
                    print_duplicate_group(
                        user_dir.name,
                        group,
                        reference_counts(
                            group,
                            original["positions"],
                            original["user"],
                            original["characters"],
                        ),
                    )
            updated = merge_and_reset(
                original["locations"],
                original["positions"],
                original["user"],
                original["characters"],
                user_choices,
            )
        except Exception as exc:
            print(f"错误：用户 {user_dir.name} 数据无效：{exc}")
            return 2

        summary = updated[4]
        print(
            f"\n用户 {user_dir.name} 计划：重复组 {summary['duplicate_groups']}，"
            f"合并 {summary['merged_groups']}，删除地点 {summary['removed_locations']}，"
            f"重置认知角色 {summary['reset_characters']}。"
        )
        if summary["unresolved_groups"]:
            print(f"  未选择、不会合并：{', '.join(summary['unresolved_groups'])}")
        plans.append({
            "user_id": user_dir.name,
            "paths": paths,
            "original": original,
            "updated": {
                "locations": updated[0],
                "positions": updated[1],
                "user": updated[2],
                "characters": updated[3],
            },
            "summary": summary,
        })

    if not args.apply:
        print("\n当前为预览模式，没有修改任何文件。")
        return 0
    unresolved = [
        f"用户 {plan['user_id']}: {', '.join(plan['summary']['unresolved_groups'])}"
        for plan in plans
        if plan["summary"]["unresolved_groups"]
    ]
    if unresolved:
        print("错误：仍有重复地点组未选择。请使用 --interactive 或 --choices：")
        for item in unresolved:
            print(f"  {item}")
        return 2

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_parent = (
        args.backup_dir.resolve()
        if args.backup_dir
        else users_root.parent / "map_cleanup_backups"
    )
    backup_root = backup_parent / timestamp
    affected_paths = [
        path
        for plan in plans
        for key, path in plan["paths"].items()
        if plan["updated"][key] != plan["original"][key]
    ]
    backup_files(affected_paths, users_root, backup_root)

    written: list[tuple[Path, Any]] = []
    try:
        for plan in plans:
            for key, path in plan["paths"].items():
                if plan["updated"][key] == plan["original"][key]:
                    continue
                atomic_save_json(path, plan["updated"][key])
                written.append((path, plan["original"][key]))
    except Exception as exc:
        print(f"写入失败，正在回滚：{exc}")
        for path, original_value in reversed(written):
            atomic_save_json(path, original_value)
        return 1

    print(f"\n清理完成。原文件备份位于：{backup_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
