#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 chat.db 统计用户在指定日期范围内每个角色(角色目录/群聊角色)的 AI 调用量。

与 usage_history.json 不同，chat.db 的 messages 表保存完整聊天历史（不限条数），
因此即使调用量超过 50 条也不会丢失。

口径：
- 单聊：users/<id>/characters/<char_id>/chat.db，role != 'user' 的每条消息 = 该角色一次 AI 调用
- 群聊：users/<id>/groups/<group_id>/chat.db，role != 'user' 的每条消息 = 该 role（角色 id）一次 AI 调用

注意：chat.db 不记录模型名与 token 数，本脚本只统计“调用次数”。
如需模型/token 信息，只能从 usage_history.json（仅保留最近 50 条）交叉补充。

用法示例：
  python3 export_char_calls.py
  python3 export_char_calls.py --user-id 1 --start 2026-09-04 --end 2026-09-05
  python3 export_char_calls.py --csv /tmp/char_calls_0904_0905.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从 chat.db 按日期范围统计每个角色的 AI 调用量。"
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=PROJECT_ROOT,
        help="项目根目录，默认是脚本所在目录",
    )
    parser.add_argument("--user-id", default="1", help="用户 ID，默认 1")
    parser.add_argument("--start", default="2026-09-04", help="开始日期 YYYY-MM-DD（含）")
    parser.add_argument("--end", default="2026-09-05", help="结束日期 YYYY-MM-DD（含）")
    parser.add_argument("--csv", type=Path, help="另存统计结果为 CSV")
    parser.add_argument("--json", dest="json_path", type=Path, help="另存统计结果为 JSON")
    return parser.parse_args()


def load_char_names(project_root: Path, user_id: str) -> dict[str, str]:
    names: dict[str, str] = {}
    config_path = project_root / "users" / str(user_id) / "configs" / "characters.json"
    if config_path.is_file():
        try:
            with config_path.open("r", encoding="utf-8-sig") as handle:
                config = json.load(handle)
            if isinstance(config, dict):
                for char_id, info in config.items():
                    if isinstance(info, dict) and info.get("name"):
                        names[str(char_id)] = str(info["name"])
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return names


def _count_ai_calls(db_path: Path, start: str, end: str) -> Counter[str]:
    """统计单个 chat.db 中 role != 'user' 的消息数，按 role 分组。"""
    counts: Counter[str] = Counter()
    if not db_path.is_file():
        return counts
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT role FROM messages WHERE role != 'user' AND timestamp >= ? AND timestamp <= ?",
                (start, end),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return counts
    for (role,) in rows:
        counts[str(role)] += 1
    return counts


def build_report(
    project_root: Path,
    user_id: str,
    start: str,
    end: str,
) -> dict[str, Any]:
    user_dir = project_root / "users" / str(user_id)
    if not user_dir.is_dir():
        raise FileNotFoundError(f"找不到用户目录：{user_dir}")

    start_ts = f"{start} 00:00:00"
    end_ts = f"{end} 23:59:59"
    char_names = load_char_names(project_root, user_id)

    counts: Counter[str] = Counter()

    # 单聊
    chars_dir = user_dir / "characters"
    if chars_dir.is_dir():
        for char_dir in sorted(chars_dir.iterdir()):
            if not char_dir.is_dir():
                continue
            db_path = char_dir / "chat.db"
            sub = _count_ai_calls(db_path, start_ts, end_ts)
            for role, count in sub.items():
                counts[char_dir.name] += count

    # 群聊
    groups_dir = user_dir / "groups"
    if groups_dir.is_dir():
        for group_dir in sorted(groups_dir.iterdir()):
            if not group_dir.is_dir():
                continue
            db_path = group_dir / "chat.db"
            sub = _count_ai_calls(db_path, start_ts, end_ts)
            for role, count in sub.items():
                counts[role] += count

    by_char = [
        {
            "char_id": char_id,
            "name": char_names.get(char_id, ""),
            "calls": count,
        }
        for char_id, count in counts.most_common()
    ]

    return {
        "user_id": str(user_id),
        "date_range": {"start": start, "end": end},
        "total_calls": sum(counts.values()),
        "by_char": by_char,
    }


def print_report(report: dict[str, Any]) -> None:
    print(
        f"用户 {report['user_id']} | 日期 {report['date_range']['start']} ~ {report['date_range']['end']}"
    )
    print(f"AI 调用总数: {report['total_calls']}\n")

    rows = report["by_char"]
    if not rows:
        print("（无匹配记录）")
        return

    headers = ("角色", "角色名", "调用次数")
    rendered = [
        (row["char_id"], row["name"] or "-", str(row["calls"])) for row in rows
    ]
    widths = [len(h) for h in headers]
    for row in rendered:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  ".join("-" * w for w in widths))
    for row in rendered:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)))


def write_outputs(report: dict[str, Any], args: argparse.Namespace) -> None:
    if args.csv:
        with args.csv.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=("char_id", "char_name", "calls"))
            writer.writeheader()
            for row in report["by_char"]:
                writer.writerow(
                    {
                        "char_id": row["char_id"],
                        "char_name": row["name"],
                        "calls": row["calls"],
                    }
                )
    if args.json_path:
        args.json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def main() -> int:
    args = parse_args()
    project_root = args.project_root.resolve()
    try:
        report = build_report(project_root, args.user_id, args.start, args.end)
        print_report(report)
        write_outputs(report, args)
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
