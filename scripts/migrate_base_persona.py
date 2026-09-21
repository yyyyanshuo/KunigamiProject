#!/usr/bin/env python3
"""Migrate production character personas to {"system_prompt": ...}.

The command is dry-run by default.  It never deletes legacy Markdown or other
prompt files; cleanup is a separate, later operation after production has been
verified.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class MigrationResult:
    user_id: str
    character_id: str
    status: str
    source: str = ""
    removed_fields: tuple[str, ...] = ()
    nonempty_visual_descriptions: bool = False
    legacy_markdown_present: bool = False
    message: str = ""


def _validate_persona_locks(value: str) -> None:
    """Validate the standalone LOCK format without importing the web app."""

    inside_lock = False
    open_line = None
    has_content = False
    for line_number, line in enumerate(value.splitlines(), start=1):
        marker = line.strip()
        if marker == "[[LOCK]]":
            if inside_lock:
                raise ValueError(f"第 {line_number} 行不允许嵌套 [[LOCK]]")
            inside_lock = True
            open_line = line_number
            has_content = False
        elif marker == "[[/LOCK]]":
            if not inside_lock:
                raise ValueError(f"第 {line_number} 行出现了没有开始标签的 [[/LOCK]]")
            if not has_content:
                raise ValueError(f"第 {open_line} 行的锁定区块不能为空")
            inside_lock = False
            open_line = None
        elif inside_lock and marker:
            has_content = True
    if inside_lock:
        raise ValueError(f"第 {open_line} 行的 [[LOCK]] 缺少 [[/LOCK]]")


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("JSON 顶层必须是对象")
    if not isinstance(value.get("system_prompt"), str):
        raise ValueError("JSON 缺少文本字段 system_prompt")
    _validate_persona_locks(value["system_prompt"])
    return value


def _read_markdown(path: Path) -> str:
    value = path.read_text(encoding="utf-8-sig")
    _validate_persona_locks(value)
    return value


def _visual_is_nonempty(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(item or "").strip() for item in value.values())
    return bool(str(value or "").strip())


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _backup_file(path: Path, users_root: Path, backup_root: Path) -> Path:
    relative = path.relative_to(users_root)
    destination = backup_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)
    return destination


def migrate_prompt_dir(
    prompts_dir: Path,
    users_root: Path,
    *,
    apply: bool,
    backup_root: Path | None,
) -> MigrationResult:
    relative = prompts_dir.relative_to(users_root)
    user_id = relative.parts[0] if len(relative.parts) > 0 else "?"
    character_id = relative.parts[2] if len(relative.parts) > 2 else "?"
    json_path = prompts_dir / "1_base_persona.json"
    md_path = prompts_dir / "1_base_persona.md"

    if not json_path.exists() and not md_path.exists():
        return MigrationResult(user_id, character_id, "skipped", message="核心人设文件不存在")

    try:
        if json_path.exists():
            old_data = _read_json(json_path)
            persona_text = old_data["system_prompt"]
            source = "json"
        else:
            old_data = None
            persona_text = _read_markdown(md_path)
            source = "markdown"

        target = {"system_prompt": persona_text}
        removed_fields = tuple(
            sorted(key for key in (old_data or {}) if key != "system_prompt")
        )
        nonempty_visual = _visual_is_nonempty(
            (old_data or {}).get("visual_descriptions")
        )
        legacy_md_present = md_path.exists()
        changed = old_data != target if old_data is not None else True
        if not changed:
            return MigrationResult(
                user_id,
                character_id,
                "unchanged",
                source=source,
                legacy_markdown_present=legacy_md_present,
            )

        result = MigrationResult(
            user_id,
            character_id,
            "would_migrate" if not apply else "migrated",
            source=source,
            removed_fields=removed_fields,
            nonempty_visual_descriptions=nonempty_visual,
            legacy_markdown_present=legacy_md_present,
        )
        if not apply:
            return result
        if backup_root is None:
            raise RuntimeError("apply 模式缺少备份目录")

        original_json_existed = json_path.exists()
        json_backup = _backup_file(json_path, users_root, backup_root) if original_json_existed else None
        if md_path.exists():
            _backup_file(md_path, users_root, backup_root)

        try:
            _atomic_write_json(json_path, target)
            written = _read_json(json_path)
            if written != target:
                raise RuntimeError("写入后校验不一致")
        except Exception:
            if original_json_existed and json_backup is not None:
                shutil.copy2(json_backup, json_path)
            else:
                try:
                    json_path.unlink()
                except FileNotFoundError:
                    pass
            raise
        return result
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return MigrationResult(
            user_id,
            character_id,
            "failed",
            legacy_markdown_present=md_path.exists(),
            message=str(exc),
        )
    except Exception as exc:
        return MigrationResult(
            user_id,
            character_id,
            "failed",
            legacy_markdown_present=md_path.exists(),
            message=f"{type(exc).__name__}: {exc}",
        )


def run_migration(
    users_root: Path,
    *,
    apply: bool = False,
    backup_root: Path | None = None,
) -> dict[str, Any]:
    users_root = users_root.resolve()
    if not users_root.is_dir():
        raise ValueError(f"users 目录不存在: {users_root}")

    if apply:
        if backup_root is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup_root = users_root.parent / "persona_migration_backups" / stamp
        backup_root = backup_root.resolve()
        if backup_root == users_root or users_root in backup_root.parents:
            raise ValueError("备份目录不能位于 users 目录内")
        if backup_root.exists():
            if not backup_root.is_dir() or any(backup_root.iterdir()):
                raise ValueError(f"备份目录必须为空: {backup_root}")

    prompt_dirs = sorted(users_root.glob("*/characters/*/prompts"))
    results = [
        migrate_prompt_dir(
            prompt_dir,
            users_root,
            apply=apply,
            backup_root=backup_root,
        )
        for prompt_dir in prompt_dirs
    ]
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1

    report = {
        "mode": "apply" if apply else "dry-run",
        "users_root": str(users_root),
        "backup_root": str(backup_root) if backup_root else None,
        "scanned": len(prompt_dirs),
        "counts": counts,
        "nonempty_visual_descriptions": sum(
            result.nonempty_visual_descriptions for result in results
        ),
        "legacy_markdown_present": sum(
            result.legacy_markdown_present for result in results
        ),
        "results": [asdict(result) for result in results],
    }
    if apply and backup_root is not None:
        _atomic_write_json(backup_root / "_migration_report.json", report)
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="将服务器角色核心人设收敛为仅含 system_prompt 的 JSON（默认只预检）"
    )
    parser.add_argument("--users-root", required=True, type=Path, help="服务器 users 数据目录")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只预检（默认）")
    mode.add_argument("--apply", action="store_true", help="实际备份并写入")
    parser.add_argument("--backup-root", type=Path, help="备份目录，只在 --apply 时使用")
    parser.add_argument("--report", type=Path, help="额外保存 JSON 报告")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        report = run_migration(
            args.users_root,
            apply=args.apply,
            backup_root=args.backup_root,
        )
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        return 2

    output = json.dumps(report, ensure_ascii=False, indent=2)
    print(output)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output + "\n", encoding="utf-8")
    return 1 if report["counts"].get("failed", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
