#!/usr/bin/env python3
"""统计“实际用户”当前配置的聊天模型。

统计口径：
1. 用户必须存在于 configs/users.db 的 users 表；
2. users/<id>/logs/usage_history.json 中至少有一条 input > 0 且 output > 0；
3. 模型取 users/<id>/configs/api_settings.json 中 active_route 对应的当前 chat
   模型。日志里曾经记录的 model 不参与模型归类。

默认排除项目约定的 1—5 号测试账号。脚本只读取业务数据，且输出不包含邮箱、
昵称和用户 ID。
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence


PROJECT_ROOT = Path(__file__).resolve().parent
UNCONFIGURED = "(未配置)"
UNKNOWN_PROVIDER = "(未知提供商)"


@dataclass(frozen=True)
class ActualUserModel:
    route: str
    route_provider: str
    model: str
    model_provider: str
    successful_calls_retained: int


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按有效 token 调用筛选实际用户，再统计其当前聊天模型配置。"
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=PROJECT_ROOT,
        help="项目根目录，默认是脚本所在目录",
    )
    parser.add_argument(
        "--users-db",
        type=Path,
        help="用户数据库；默认 <project-root>/configs/users.db",
    )
    parser.add_argument(
        "--min-successes",
        type=int,
        default=1,
        help="成为实际用户所需的有效调用条数，默认 1",
    )
    parser.add_argument(
        "--include-test-users",
        action="store_true",
        help="纳入项目约定的 1—5 号测试账号",
    )
    parser.add_argument(
        "--include-orphan-user-dirs",
        action="store_true",
        help="纳入 users 目录存在、但 users.db 中不存在的旧账号目录",
    )
    parser.add_argument(
        "--exclude-user",
        action="append",
        default=[],
        metavar="USER_ID",
        help="额外排除一个用户 ID，可重复使用",
    )
    parser.add_argument("--json", dest="json_path", type=Path, help="另存脱敏 JSON 报告")
    parser.add_argument("--csv", dest="csv_path", type=Path, help="另存按模型汇总的 CSV")
    args = parser.parse_args(argv)
    if args.min_successes < 1:
        parser.error("--min-successes 必须大于等于 1")
    return args


def _positive_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def count_successful_calls(path: Path) -> tuple[int, int]:
    """返回 (有效调用数, 无效/异常记录数)。"""
    with path.open("r", encoding="utf-8-sig") as handle:
        records = json.load(handle)
    if not isinstance(records, list):
        raise ValueError("usage_history.json 顶层不是数组")

    successful = 0
    invalid = 0
    for record in records:
        if not isinstance(record, dict):
            invalid += 1
            continue
        if _positive_number(record.get("input")) and _positive_number(record.get("output")):
            successful += 1
        else:
            invalid += 1
    return successful, invalid


def infer_model_provider(model: str) -> str:
    normalized = model.strip().lower()
    rules = (
        ("Google", ("gemini", "gemma")),
        ("OpenAI", ("gpt", "chatgpt", "o1", "o3", "o4")),
        ("DeepSeek", ("deepseek",)),
        ("Anthropic", ("claude",)),
        ("阿里云", ("qwen", "tongyi", "通义")),
        ("Moonshot", ("kimi", "moonshot")),
        ("智谱", ("chatglm", "glm", "智谱")),
        ("字节跳动", ("doubao", "豆包", "seed")),
        ("MiniMax", ("minimax",)),
        ("百度", ("ernie", "文心")),
        ("xAI", ("grok",)),
        ("Mistral", ("mistral", "mixtral")),
    )
    for provider, markers in rules:
        if any(marker in normalized for marker in markers):
            return provider
    return UNKNOWN_PROVIDER


def read_current_chat_model(config_path: Path) -> tuple[str, str, str, str]:
    """返回 route、route_provider、chat model、model_provider。"""
    if not config_path.is_file():
        return UNCONFIGURED, UNCONFIGURED, UNCONFIGURED, UNKNOWN_PROVIDER

    with config_path.open("r", encoding="utf-8-sig") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ValueError("api_settings.json 顶层不是对象")

    route = str(config.get("active_route") or "").strip() or UNCONFIGURED
    routes = config.get("routes")
    route_config = routes.get(route) if isinstance(routes, dict) else None
    if not isinstance(route_config, dict):
        return route, UNCONFIGURED, UNCONFIGURED, UNKNOWN_PROVIDER

    models = route_config.get("models")
    model = str(models.get("chat") or "").strip() if isinstance(models, dict) else ""
    model = model or UNCONFIGURED
    route_provider = str(route_config.get("relay_provider") or "").strip()
    if not route_provider:
        if route == "gemini":
            route_provider = "direct"
        elif route == "relay":
            route_provider = "old"
        else:
            route_provider = UNCONFIGURED
    return route, route_provider, model, infer_model_provider(model)


def read_registered_user_ids(db_path: Path) -> set[str]:
    if not db_path.is_file():
        raise FileNotFoundError(f"找不到用户数据库：{db_path}")
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT id FROM users").fetchall()
    finally:
        connection.close()
    return {str(row[0]) for row in rows}


def is_default_test_user(user_id: str) -> bool:
    try:
        return 1 <= int(user_id) <= 5
    except ValueError:
        return False


def percentage(numerator: int, denominator: int) -> float:
    return round(numerator * 100 / denominator, 2) if denominator else 0.0


def aggregate_rows(
    actual_users: Iterable[ActualUserModel],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    users = list(actual_users)
    total_users = len(users)

    model_counts = Counter((u.route, u.model_provider, u.model) for u in users)
    model_rows = [
        {
            "route": route,
            "model_provider": provider,
            "model": model,
            "actual_users": count,
            "actual_user_share_pct": percentage(count, total_users),
        }
        for (route, provider, model), count in model_counts.most_common()
    ]

    provider_counts = Counter(u.model_provider for u in users)
    provider_rows = [
        {
            "model_provider": provider,
            "actual_users": count,
            "actual_user_share_pct": percentage(count, total_users),
        }
        for provider, count in provider_counts.most_common()
    ]

    route_counts = Counter((u.route, u.route_provider) for u in users)
    route_rows = [
        {
            "route": route,
            "route_provider": route_provider,
            "actual_users": count,
            "actual_user_share_pct": percentage(count, total_users),
        }
        for (route, route_provider), count in route_counts.most_common()
    ]
    return model_rows, provider_rows, route_rows


def build_report(
    project_root: Path,
    users_db: Path,
    *,
    min_successes: int = 1,
    include_test_users: bool = False,
    include_orphans: bool = False,
    excluded_user_ids: set[str] | None = None,
) -> dict[str, Any]:
    users_root = project_root / "users"
    registered_ids = read_registered_user_ids(users_db)
    excluded_ids = set(excluded_user_ids or ())
    if not include_test_users:
        excluded_ids.update(user_id for user_id in registered_ids if is_default_test_user(user_id))

    eligible_registered_ids = registered_ids - excluded_ids
    directory_ids = {path.name for path in users_root.iterdir() if path.is_dir()} if users_root.is_dir() else set()
    candidate_ids = directory_ids if include_orphans else directory_ids & registered_ids
    candidate_ids -= excluded_ids
    eligible_population_ids = set(eligible_registered_ids)
    if include_orphans:
        eligible_population_ids.update(directory_ids - registered_ids - excluded_ids)

    actual_users: list[ActualUserModel] = []
    unreadable_usage_logs = 0
    invalid_usage_records = 0
    unreadable_model_configs = 0
    users_below_threshold = 0
    users_without_usage_log = 0

    for user_id in sorted(candidate_ids):
        usage_path = users_root / user_id / "logs" / "usage_history.json"
        if not usage_path.is_file():
            users_without_usage_log += 1
            continue
        try:
            successful_calls, invalid_records = count_successful_calls(usage_path)
            invalid_usage_records += invalid_records
        except (OSError, ValueError, json.JSONDecodeError):
            unreadable_usage_logs += 1
            continue
        if successful_calls < min_successes:
            users_below_threshold += 1
            continue

        config_path = users_root / user_id / "configs" / "api_settings.json"
        try:
            route, route_provider, model, model_provider = read_current_chat_model(config_path)
        except (OSError, ValueError, json.JSONDecodeError):
            unreadable_model_configs += 1
            route, route_provider, model, model_provider = (
                UNCONFIGURED,
                UNCONFIGURED,
                UNCONFIGURED,
                UNKNOWN_PROVIDER,
            )
        actual_users.append(
            ActualUserModel(
                route=route,
                route_provider=route_provider,
                model=model,
                model_provider=model_provider,
                successful_calls_retained=successful_calls,
            )
        )

    model_rows, provider_rows, route_rows = aggregate_rows(actual_users)
    unconfigured_users = sum(user.model == UNCONFIGURED for user in actual_users)
    retained_successful_calls = sum(user.successful_calls_retained for user in actual_users)
    return {
        "definition": {
            "actual_user": f"usage_history.json 中至少 {min_successes} 条 input > 0 且 output > 0 的记录",
            "reported_model": "实际用户 active_route 下当前配置的 chat 模型",
            "usage_log_model_used_for_grouping": False,
            "privacy": "仅输出聚合数据，不输出用户 ID、邮箱或昵称",
            "history_limit": "每名用户的 usage_history.json 仅保留最近 50 条，且 time 没有年份",
        },
        "summary": {
            "eligible_users_after_exclusions": len(eligible_population_ids),
            "registered_users_after_exclusions": len(eligible_registered_ids),
            "actual_users": len(actual_users),
            "actual_user_rate_pct": percentage(len(actual_users), len(eligible_population_ids)),
            "retained_successful_calls_of_actual_users": retained_successful_calls,
            "unconfigured_actual_users": unconfigured_users,
            "registered_users_without_directory": len(eligible_registered_ids - directory_ids),
            "candidate_users_without_usage_log": users_without_usage_log,
            "users_below_success_threshold": users_below_threshold,
            "unreadable_usage_logs": unreadable_usage_logs,
            "invalid_usage_records": invalid_usage_records,
            "unreadable_model_configs": unreadable_model_configs,
            "excluded_users": len(excluded_ids & (registered_ids | directory_ids)),
            "orphan_user_directories_seen": len(directory_ids - registered_ids),
        },
        "by_model": model_rows,
        "by_model_provider": provider_rows,
        "by_route": route_rows,
    }


def print_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    rendered = [[str(cell) for cell in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in rendered:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    print("  ".join(header.ljust(width) for header, width in zip(headers, widths)))
    print("  ".join("-" * width for width in widths))
    for row in rendered:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)))


def print_report(report: dict[str, Any]) -> None:
    summary = report["summary"]
    print("实际用户当前聊天模型分布")
    print(
        f"统计范围内用户（排除项后）: {summary['eligible_users_after_exclusions']}  |  "
        f"实际用户: {summary['actual_users']}  |  "
        f"转化率: {summary['actual_user_rate_pct']}%"
    )
    print(
        f"实际用户日志中保留的有效调用: {summary['retained_successful_calls_of_actual_users']}  |  "
        f"模型未配置/无法读取: {summary['unconfigured_actual_users']}"
    )

    print("\n按当前 chat 模型")
    print_table(
        ("Route", "Provider", "Model", "Users", "Share"),
        [
            (
                row["route"],
                row["model_provider"],
                row["model"],
                row["actual_users"],
                f"{row['actual_user_share_pct']}%",
            )
            for row in report["by_model"]
        ],
    )

    print("\n按模型提供商")
    print_table(
        ("Provider", "Users", "Share"),
        [
            (row["model_provider"], row["actual_users"], f"{row['actual_user_share_pct']}%")
            for row in report["by_model_provider"]
        ],
    )

    print("\n说明：日志中的 model 字段未用于归类；表中是实际用户当前保存的 chat 模型。")
    print("注意：usage_history.json 每名用户最多保留 50 条，time 字段没有年份。")


def write_outputs(report: dict[str, Any], args: argparse.Namespace) -> None:
    if args.json_path:
        args.json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if args.csv_path:
        with args.csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            fields = ("route", "model_provider", "model", "actual_users", "actual_user_share_pct")
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(report["by_model"])


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    project_root = args.project_root.resolve()
    users_db = args.users_db.resolve() if args.users_db else project_root / "configs" / "users.db"
    try:
        report = build_report(
            project_root,
            users_db,
            min_successes=args.min_successes,
            include_test_users=args.include_test_users,
            include_orphans=args.include_orphan_user_dirs,
            excluded_user_ids=set(args.exclude_user),
        )
        print_report(report)
        write_outputs(report, args)
    except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
