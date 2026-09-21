#!/usr/bin/env python3
"""Synchronize every character timezone with their current mapped location.

The script is dry-run by default. Use ``--apply`` to write changes. Before any
write, the original JSON files are copied to a timestamped backup directory.

Examples:
    python scripts/sync_character_timezones.py
    python scripts/sync_character_timezones.py --apply
    python scripts/sync_character_timezones.py --user 1 --apply

Run ``--apply`` while the application is stopped or in a maintenance window,
so the server cannot overwrite characters.json concurrently.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def is_valid_timezone(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        ZoneInfo(value.strip())
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def infer_timezone(lat: Any, lon: Any) -> str | None:
    try:
        latitude = float(lat)
        longitude = float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None

    try:
        from timezonefinder import TimezoneFinder
    except ImportError as exc:
        raise RuntimeError(
            "地点存在经纬度但没有 timezone，服务器尚未安装 timezonefinder。"
            "请先执行 pip install -r requirements.txt"
        ) from exc

    value = TimezoneFinder(in_memory=True).timezone_at(
        lat=latitude,
        lng=longitude,
    )
    return value if is_valid_timezone(value) else None


def load_json(path: Path) -> Any:
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


@dataclass
class CharacterChange:
    user_id: str
    char_id: str
    char_name: str
    location_id: str
    location_name: str
    old_timezone: str | None
    new_timezone: str
    timezone_origin: str
    metadata_only: bool


@dataclass
class UserPlan:
    user_id: str
    config_dir: Path
    characters_path: Path
    locations_path: Path
    original_characters: dict[str, Any]
    updated_characters: dict[str, Any]
    original_locations: dict[str, Any]
    updated_locations: dict[str, Any]
    changes: list[CharacterChange] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def characters_changed(self) -> bool:
        return self.updated_characters != self.original_characters

    @property
    def locations_changed(self) -> bool:
        return self.updated_locations != self.original_locations


@dataclass
class MigrationPlan:
    users_root: Path
    user_plans: list[UserPlan] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def changes(self) -> list[CharacterChange]:
        return [
            change
            for user_plan in self.user_plans
            for change in user_plan.changes
        ]


def location_timezone(location: dict[str, Any]) -> tuple[str | None, str]:
    real_world = location.get("real_world")
    if not isinstance(real_world, dict):
        return None, "none"

    explicit = real_world.get("timezone")
    if is_valid_timezone(explicit):
        return explicit.strip(), "location"

    inferred = infer_timezone(real_world.get("lat"), real_world.get("lon"))
    if inferred:
        return inferred, "coordinates"
    return None, "none"


def build_plan(
    users_root: Path,
    selected_users: set[str] | None = None,
) -> MigrationPlan:
    plan = MigrationPlan(users_root=users_root)
    if not users_root.is_dir():
        plan.errors.append(f"用户目录不存在: {users_root}")
        return plan

    user_dirs = sorted(
        (path for path in users_root.iterdir() if path.is_dir()),
        key=lambda path: path.name,
    )
    for user_dir in user_dirs:
        user_id = user_dir.name
        if selected_users and user_id not in selected_users:
            continue

        config_dir = user_dir / "configs"
        characters_path = config_dir / "characters.json"
        positions_path = config_dir / "character_positions.json"
        locations_path = config_dir / "locations.json"
        if not characters_path.exists():
            continue
        if not positions_path.exists() or not locations_path.exists():
            missing = [
                path.name
                for path in (positions_path, locations_path)
                if not path.exists()
            ]
            plan.warnings.append(
                f"用户 {user_id} 缺少 {', '.join(missing)}，已跳过"
            )
            continue

        try:
            original_characters = load_json(characters_path)
            positions = load_json(positions_path)
            original_locations = load_json(locations_path)
        except Exception as exc:
            plan.errors.append(f"用户 {user_id} 读取 JSON 失败: {exc}")
            continue

        if not isinstance(original_characters, dict):
            plan.errors.append(f"用户 {user_id} 的 characters.json 不是对象")
            continue
        if not isinstance(positions, dict):
            plan.errors.append(
                f"用户 {user_id} 的 character_positions.json 不是对象"
            )
            continue
        if not isinstance(original_locations, dict) or not isinstance(
            original_locations.get("locations"), list
        ):
            plan.errors.append(f"用户 {user_id} 的 locations.json 格式错误")
            continue

        updated_characters = copy.deepcopy(original_characters)
        updated_locations = copy.deepcopy(original_locations)
        locations_by_id = {
            str(location.get("id")): location
            for location in updated_locations["locations"]
            if isinstance(location, dict) and location.get("id") is not None
        }
        user_plan = UserPlan(
            user_id=user_id,
            config_dir=config_dir,
            characters_path=characters_path,
            locations_path=locations_path,
            original_characters=original_characters,
            updated_characters=updated_characters,
            original_locations=original_locations,
            updated_locations=updated_locations,
        )

        timezone_cache: dict[str, tuple[str | None, str]] = {}
        for char_id, info in updated_characters.items():
            if not isinstance(info, dict):
                user_plan.skipped.append(f"{char_id}: 角色配置不是对象")
                continue
            position = positions.get(char_id)
            if not isinstance(position, dict):
                user_plan.skipped.append(f"{char_id}: 没有地图位置")
                continue
            raw_location_id = position.get("location_id")
            location_id = (
                str(raw_location_id) if raw_location_id is not None else ""
            )
            location = locations_by_id.get(location_id)
            if not location:
                user_plan.skipped.append(
                    f"{char_id}: 当前地点 {location_id or '(空)'} 不存在"
                )
                continue

            try:
                if location_id not in timezone_cache:
                    timezone_cache[location_id] = location_timezone(location)
                timezone_name, origin = timezone_cache[location_id]
            except RuntimeError as exc:
                plan.errors.append(
                    f"用户 {user_id} 地点 {location_id}: {exc}"
                )
                continue
            if not timezone_name:
                user_plan.skipped.append(
                    f"{char_id}: 地点 {location.get('name', location_id)} "
                    "没有有效时区或经纬度"
                )
                continue

            if origin == "coordinates":
                real_world = location.setdefault("real_world", {})
                real_world["timezone"] = timezone_name
                real_world["timezone_source"] = "coordinates"

            old_timezone = (
                str(info.get("timezone")).strip()
                if info.get("timezone") is not None
                else None
            )
            expected_metadata = (
                info.get("timezone_source") == "location"
                and str(info.get("timezone_location_id") or "") == location_id
            )
            metadata_only = old_timezone == timezone_name and not expected_metadata
            if old_timezone == timezone_name and expected_metadata:
                continue

            info["timezone"] = timezone_name
            info["timezone_source"] = "location"
            info["timezone_location_id"] = location_id
            info["sleep_last_event_key"] = None
            if info.get("ds_time_basis") == "character":
                info["ds_timezone_at_set"] = timezone_name

            user_plan.changes.append(
                CharacterChange(
                    user_id=user_id,
                    char_id=str(char_id),
                    char_name=str(info.get("name") or char_id),
                    location_id=location_id,
                    location_name=str(location.get("name") or location_id),
                    old_timezone=old_timezone,
                    new_timezone=timezone_name,
                    timezone_origin=origin,
                    metadata_only=metadata_only,
                )
            )

        plan.user_plans.append(user_plan)

    return plan


def backup_file(
    source: Path,
    backup_root: Path,
    users_root: Path,
) -> Path:
    relative = source.relative_to(users_root.parent)
    destination = backup_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return destination


def apply_plan(plan: MigrationPlan, backup_parent: Path) -> Path | None:
    changed_plans = [
        user_plan
        for user_plan in plan.user_plans
        if user_plan.characters_changed or user_plan.locations_changed
    ]
    if not changed_plans:
        return None

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_root = backup_parent / timestamp
    for user_plan in changed_plans:
        if user_plan.characters_changed:
            backup_file(
                user_plan.characters_path,
                backup_root,
                plan.users_root,
            )
        if user_plan.locations_changed:
            backup_file(
                user_plan.locations_path,
                backup_root,
                plan.users_root,
            )

    try:
        for user_plan in changed_plans:
            if user_plan.locations_changed:
                atomic_save_json(
                    user_plan.locations_path,
                    user_plan.updated_locations,
                )
            if user_plan.characters_changed:
                atomic_save_json(
                    user_plan.characters_path,
                    user_plan.updated_characters,
                )
    except Exception:
        for user_plan in changed_plans:
            for source in (
                user_plan.characters_path,
                user_plan.locations_path,
            ):
                backup = backup_root / source.relative_to(plan.users_root.parent)
                if backup.exists():
                    shutil.copy2(backup, source)
        raise
    return backup_root


def print_plan(plan: MigrationPlan) -> None:
    for change in plan.changes:
        kind = "元数据修复" if change.metadata_only else "时区修改"
        old_timezone = change.old_timezone or "(未设置)"
        print(
            f"[{kind}] user={change.user_id} "
            f"char={change.char_id}({change.char_name}) "
            f"location={change.location_id}({change.location_name}) "
            f"{old_timezone} -> {change.new_timezone} "
            f"来源={change.timezone_origin}"
        )
    for user_plan in plan.user_plans:
        for reason in user_plan.skipped:
            print(f"[跳过] user={user_plan.user_id} {reason}")
    for warning in plan.warnings:
        print(f"[警告] {warning}")
    for error in plan.errors:
        print(f"[错误] {error}", file=sys.stderr)

    skipped_count = sum(len(item.skipped) for item in plan.user_plans)
    print(
        f"\n扫描用户 {len(plan.user_plans)} 个，"
        f"待修改角色 {len(plan.changes)} 个，"
        f"跳过 {skipped_count} 个，"
        f"警告 {len(plan.warnings)} 个，"
        f"错误 {len(plan.errors)} 个。"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="让角色时区与当前地图地点的现实时区保持一致"
    )
    parser.add_argument(
        "--users-root",
        type=Path,
        default=PROJECT_ROOT / "users",
        help="users 目录，默认使用项目根目录下的 users",
    )
    parser.add_argument(
        "--user",
        action="append",
        dest="users",
        help="只处理指定用户 ID；可重复使用",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际写入；不加此参数时只预览",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        help="备份父目录；默认 users 同级的 timezone_sync_backups",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    users_root = args.users_root.resolve()
    plan = build_plan(users_root, set(args.users) if args.users else None)
    print_plan(plan)
    if plan.errors:
        print("\n存在错误，未写入任何文件。", file=sys.stderr)
        return 2
    if not args.apply:
        print("\n当前为预览模式；确认后添加 --apply 执行。")
        return 0

    backup_parent = (
        args.backup_dir.resolve()
        if args.backup_dir
        else users_root.parent / "timezone_sync_backups"
    )
    backup_root = apply_plan(plan, backup_parent)
    if backup_root is None:
        print("\n没有需要写入的内容。")
    else:
        print(f"\n迁移完成。原文件备份位于：{backup_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
