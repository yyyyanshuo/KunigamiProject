#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出指定用户在指定日期范围内的指定模型调用记录，并按角色(char_id)统计调用量。

数据来源：users/<user_id>/logs/usage_history.json
- time 字段格式为 "MM-DD HH:MM:SS"（无年份）
- char_id 为角色/系统任务标识
- model 为实际调用模型名

用法示例：
  python3 export_model_calls.py
  python3 export_model_calls.py --user-id 1 --model gemini-2.5-pro --start 09-04 --end 09-05
  python3 export_model_calls.py --csv users/1/logs/gemini_pro_0904_0905.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按日期范围导出用户模型调用记录并按角色统计调用量。"
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=PROJECT_ROOT,
        help="项目根目录，默认是脚本所在目录",
    )
    parser.add_argument("--user-id", default="1", help="用户 ID，默认 1")
    parser.add_argument("--model", default="gemini-2.5-pro", help="要筛选的模型名")
    parser.add_argument("--start", default="09-04", help="开始日期 MM-DD（含），默认 09-04")
    parser.add_argument("--end", default="09-05", help="结束日期 MM-DD（含），默认 09-05")
    parser.add_argument("--csv", type=Path, help="另存原始调用记录为 CSV")
    parser.add_argument("--json", dest="json_path", type=Path, help="另存完整报告为 JSON")
    return parser.parse_args()


def load_usage_records(usage_path: Path) -> list[dict[str, Any]]:
    with usage_path.open("r", encoding="utf-8-sig") as handle:
        records = json.load(handle)
    if not isinstance(records, list):
        raise ValueError("usage_history.json 顶层不是数组")
    return [r for r in records if isinstance(r, dict)]


def load_char_names(project_root: Path, user_id: str) -> dict[str, str]:
    names: dict[str, str] = {}
    config_path = project_root / "users" / str(user_id) / "configs" / "characters.json"
    if not config_path.is_file():
        return names
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


def filter_records(
    records: list[dict[str, Any]],
    model: str,
    start: str,
    end: str,
) -> list[dict[str, Any]]:
    model_lower = model.strip().lower()
    start_key = f"{start.strip()} 00:00:00"
    end_key = f"{end.strip()} 23:59:59"

    matched = []
    for record in records:
        rec_model = str(record.get("model") or "").strip().lower()
        if rec_model != model_lower:
            continue
        time_str = str(record.get("time") or "").strip()
        if not time_str:
            continue
        if not (start_key <= time_str <= end_key):
            continue
        matched.append(record)
    return matched


def build_report(
    project_root: Path,
    user_id: str,
    model: str,
    start: str,
    end: str,
) -> dict[str, Any]:
    usage_path = project_root / "users" / str(user_id) / "logs" / "usage_history.json"
    if not usage_path.is_file():
        raise FileNotFoundError(f"找不到调用记录文件：{usage_path}")

    records = load_usage_records(usage_path)
    matched = filter_records(records, model, start, end)
    char_names = load_char_names(project_root, user_id)

    counts: Counter[str] = Counter()
    input_sum: Counter[str] = Counter()
    output_sum: Counter[str] = Counter()
    total_sum: Counter[str] = Counter()

    for record in matched:
        char_id = str(record.get("char_id") or "(未知)").strip()
        counts[char_id] += 1
        input_sum[char_id] += int(record.get("input") or 0)
        output_sum[char_id] += int(record.get("output") or 0)
        total_sum[char_id] += int(record.get("total") or 0)

    by_char = []
    for char_id, count in counts.most_common():
        by_char.append(
            {
                "char_id": char_id,
                "name": char_names.get(char_id, ""),
                "calls": count,
                "input_tokens": input_sum[char_id],
                "output_tokens": output_sum[char_id],
                "total_tokens": total_sum[char_id],
            }
        )

    return {
        "user_id": str(user_id),
        "model": model,
        "date_range": {"start": start, "end": end},
        "matched_records": len(matched),
        "by_char": by_char,
        "records": matched,
    }


def print_report(report: dict[str, Any]) -> None:
    print(
        f"用户 {report['user_id']} | 模型 {report['model']} | "
        f"日期 {report['date_range']['start']} ~ {report['date_range']['end']}"
    )
    print(f"匹配调用记录数: {report['matched_records']}\n")

    rows = report["by_char"]
    if not rows:
        print("（无匹配记录）")
        return

    headers = ("角色", "角色名", "调用次数", "输入tokens", "输出tokens", "总tokens")
    rendered = [
        (
            row["char_id"],
            row["name"] or "-",
            str(row["calls"]),
            str(row["input_tokens"]),
            str(row["output_tokens"]),
            str(row["total_tokens"]),
        )
        for row in rows
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
        char_names = {row["char_id"]: row["name"] for row in report["by_char"]}
        with args.csv.open("w", encoding="utf-8-sig", newline="") as handle:
            fields = ("time", "char_id", "char_name", "model", "input", "output", "total")
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for record in report["records"]:
                char_id = str(record.get("char_id") or "")
                writer.writerow(
                    {
                        "time": record.get("time", ""),
                        "char_id": char_id,
                        "char_name": char_names.get(char_id, ""),
                        "model": record.get("model", ""),
                        "input": record.get("input", 0),
                        "output": record.get("output", 0),
                        "total": record.get("total", 0),
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
        report = build_report(
            project_root,
            args.user_id,
            args.model,
            args.start,
            args.end,
        )
        print_report(report)
        write_outputs(report, args)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
