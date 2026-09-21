#!/usr/bin/env python3
"""Change existing sleep schedules to character-local wall-clock time.

This migration intentionally preserves ``ds_start`` and ``ds_end``. For
example, ``02:00`` remains ``02:00``, but its meaning changes from the legacy
user/Beijing time basis to 02:00 in the character's current timezone.

The script is dry-run by default. Use ``--apply`` during a maintenance window
to write changes. Every changed characters.json is backed up first.

Recommended order:
    1. Run scripts/sync_character_timezones.py --apply
    2. Run this script without --apply and review the preview
    3. Run this script with --apply
"""

from __future__ import annotations

import argparse
import copy
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.sync_character_timezones import (  # noqa: E402
    atomic_save_json,
    backup_file,
    is_valid_timezone,
    load_json,
)


def valid_hhmm(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.strip().split(":")
    if len(parts) != 2:
        return False
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59


@dataclass
class SleepChange:
    user_id: str
    char_id: str
    char_name: str
    timezone_name: str
    ds_start: str
    ds_end: str
    old_basis: str
    metadata_only: bool


@dataclass
class UserSleepPlan:
    user_id: str
    characters_path: Path
    original_characters: dict[str, Any]
    updated_characters: dict[str, Any]
    changes: list[SleepChange] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.original_characters != self.updated_characters


@dataclass
class SleepMigrationPlan:
    users_root: Path
    user_plans: list[UserSleepPlan] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def changes(self) -> list[SleepChange]:
        return [
            change
            for user_plan in self.user_plans
            for change in user_plan.changes
        ]


def build_plan(
    users_root: Path,
    selected_users: set[str] | None = None,
) -> SleepMigrationPlan:
    plan = SleepMigrationPlan(users_root=users_root)
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

        characters_path = user_dir / "configs" / "characters.json"
        if not characters_path.exists():
            continue
        try:
            original_characters = load_json(characters_path)
        except Exception as exc:
            plan.errors.append(f"用户 {user_id} 读取 characters.json 失败: {exc}")
            continue
        if not isinstance(original_characters, dict):
            plan.errors.append(f"用户 {user_id} 的 characters.json 不是对象")
            continue

        updated_characters = copy.deepcopy(original_characters)
        user_plan = UserSleepPlan(
            user_id=user_id,
            characters_path=characters_path,
            original_characters=original_characters,
            updated_characters=updated_characters,
        )
        for char_id, info in updated_characters.items():
            if not isinstance(info, dict):
                user_plan.skipped.append(f"{char_id}: 角色配置不是对象")
                continue

            timezone_name = info.get("timezone")
            if not is_valid_timezone(timezone_name):
                user_plan.skipped.append(
                    f"{char_id}: 没有有效角色时区，请先运行时区同步脚本"
                )
                continue
            timezone_name = str(timezone_name).strip()

            ds_start = info.get("ds_start")
            ds_end = info.get("ds_end")
            if not valid_hhmm(ds_start) or not valid_hhmm(ds_end):
                user_plan.skipped.append(
                    f"{char_id}: 睡眠时间格式无效 "
                    f"({ds_start!r} - {ds_end!r})"
                )
                continue
            if ds_start == ds_end:
                user_plan.skipped.append(
                    f"{char_id}: 入睡和唤醒时间相同 ({ds_start})"
                )
                continue

            old_basis = str(info.get("ds_time_basis") or "legacy/user")
            expected_metadata = (
                info.get("ds_time_basis") == "character"
                and info.get("ds_timezone_at_set") == timezone_name
                and info.get("ds_set_by") in {
                    "character",
                    "migration_character_local",
                }
            )
            if expected_metadata:
                continue

            metadata_only = info.get("ds_time_basis") == "character"
            info["ds_time_basis"] = "character"
            info["ds_timezone_at_set"] = timezone_name
            info["ds_set_by"] = "migration_character_local"
            info["sleep_last_event_key"] = None

            user_plan.changes.append(
                SleepChange(
                    user_id=user_id,
                    char_id=str(char_id),
                    char_name=str(info.get("name") or char_id),
                    timezone_name=str(timezone_name),
                    ds_start=str(ds_start),
                    ds_end=str(ds_end),
                    old_basis=old_basis,
                    metadata_only=metadata_only,
                )
            )

        plan.user_plans.append(user_plan)

    return plan


def apply_plan(
    plan: SleepMigrationPlan,
    backup_parent: Path,
) -> Path | None:
    changed_plans = [item for item in plan.user_plans if item.changed]
    if not changed_plans:
        return None

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_root = backup_parent / timestamp
    for user_plan in changed_plans:
        backup_file(
            user_plan.characters_path,
            backup_root,
            plan.users_root,
        )

    try:
        for user_plan in changed_plans:
            atomic_save_json(
                user_plan.characters_path,
                user_plan.updated_characters,
            )
    except Exception:
        for user_plan in changed_plans:
            backup = (
                backup_root
                / user_plan.characters_path.relative_to(plan.users_root.parent)
            )
            if backup.exists():
                shutil.copy2(backup, user_plan.characters_path)
        raise
    return backup_root


def print_plan(plan: SleepMigrationPlan) -> None:
    for change in plan.changes:
        change_type = "元数据修复" if change.metadata_only else "基准迁移"
        print(
            f"[{change_type}] user={change.user_id} "
            f"char={change.char_id}({change.char_name}) "
            f"{change.ds_start}-{change.ds_end} 保持不变，"
            f"{change.old_basis} -> character，"
            f"timezone={change.timezone_name}"
        )
    for user_plan in plan.user_plans:
        for reason in user_plan.skipped:
            print(f"[跳过] user={user_plan.user_id} {reason}")
    for error in plan.errors:
        print(f"[错误] {error}", file=sys.stderr)

    skipped_count = sum(len(item.skipped) for item in plan.user_plans)
    print(
        f"\n扫描用户 {len(plan.user_plans)} 个，"
        f"待迁移角色 {len(plan.changes)} 个，"
        f"跳过 {skipped_count} 个，"
        f"错误 {len(plan.errors)} 个。"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="保留睡眠钟面时间，并将其改为角色当地时间基准"
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
        help="备份父目录；默认项目根目录下的 sleep_time_migration_backups",
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
        else users_root.parent / "sleep_time_migration_backups"
    )
    backup_root = apply_plan(plan, backup_parent)
    if backup_root is None:
        print("\n没有需要写入的内容。")
    else:
        print(f"\n迁移完成。原文件备份位于：{backup_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
