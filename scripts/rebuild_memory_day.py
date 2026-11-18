#!/usr/bin/env python3
"""Safely rebuild one character's Beijing-date short and medium memory."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from core.context import clear_background_user, set_background_user  # noqa: E402
from core.config import USERS_ROOT  # noqa: E402
from core.utils import get_paths  # noqa: E402
from services.memory import (  # noqa: E402
    generate_medium_memory_for_date,
    update_short_memory_for_date,
)
from services.memory_store import (  # noqa: E402
    atomic_write_json,
    load_json_object,
    memory_file_lock,
)
from services.moment_memory import format_moment_memory, moment_reply_source  # noqa: E402


def _load_character_names(user_id: int):
    path = Path(USERS_ROOT) / str(user_id) / "configs" / "characters.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return {
        char_id: (info.get("remark") or info.get("name") or char_id)
        for char_id, info in data.items()
        if isinstance(info, dict)
    }


def _moment_actions(user_id: int, char_id: str, target_date: str):
    """Build one source context for every canonical Moments action."""
    path = Path(USERS_ROOT) / str(user_id) / "configs" / "moments_data.json"
    if not path.exists():
        raise SystemExit(f"Moments data does not exist: {path}")
    try:
        posts = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise SystemExit(f"Unable to read Moments data: {exc}")
    names = _load_character_names(user_id)
    actions = []
    for post in posts if isinstance(posts, list) else []:
        author_id = str(post.get("char_id", ""))
        author_name = "用户" if author_id == "user" else names.get(author_id, author_id)
        post_ts = str(post.get("timestamp", ""))
        content = str(post.get("content", ""))
        if author_id == char_id and post_ts.startswith(target_date):
            actions.append({
                "time": post_ts[11:16],
                "context": format_moment_memory(content),
            })
        comments = post.get("comments", []) or []
        for comment_index, comment in enumerate(comments):
            if str(comment.get("commenter_id", "")) != char_id:
                continue
            comment_ts = str(comment.get("timestamp", ""))
            if not comment_ts.startswith(target_date):
                continue
            comment_text = str(comment.get("content", ""))
            reply_to = comment.get("reply_to")
            source = moment_reply_source(comments, comment_index)
            target = ("用户" if reply_to == "user" else names.get(reply_to, reply_to)) if reply_to else None
            detail = format_moment_memory(
                content, author_name=author_name, comment_content=comment_text,
                reply_to_name=target,
                upstream_content=source.get("content", "") if source is not None else None,
            )
            actions.append({"time": comment_ts[11:16], "context": detail})
    unique = []
    seen = set()
    for action in sorted(actions, key=lambda item: (item["time"], item["context"])):
        key = (action["time"], action["context"])
        if key not in seen:
            seen.add(key)
            unique.append(action)
    return unique


def _moment_events(actions):
    """Restore full source records without a summary model or API delays."""
    return [{"time": action["time"], "event": action["context"]} for action in actions]


def _replace_moment_events(short_file: Path, target_date: str, restored_events):
    with memory_file_lock(str(short_file)):
        data = load_json_object(str(short_file))
        day_data = data.get(target_date, {})
        if isinstance(day_data, list):
            events = list(day_data)
            last_id = 0
        elif isinstance(day_data, dict):
            events = list(day_data.get("events", []))
            last_id = int(day_data.get("last_id", 0) or 0)
        else:
            events = []
            last_id = 0
        events = [
            event for event in events
            if not str((event or {}).get("event", "")).lstrip().startswith("[朋友圈]")
        ]
        events.extend(restored_events)
        events.sort(key=lambda item: (str(item.get("time", "")), str(item.get("event", ""))))
        data[target_date] = {"events": events, "last_id": last_id}
        atomic_write_json(str(short_file), data)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True, type=int)
    parser.add_argument("--char-id", required=True)
    parser.add_argument("--date", required=True, help="Beijing date, YYYY-MM-DD")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--backup-root",
        default=str(ROOT / "memory-rebuild-backups"),
    )
    args = parser.parse_args()
    datetime.strptime(args.date, "%Y-%m-%d")

    set_background_user(args.user_id)
    try:
        db_path, prompts_dir = get_paths(args.char_id, user_id=args.user_id)
        db_path = Path(db_path)
        prompts_dir = Path(prompts_dir)
        short_file = prompts_dir / "6_memory_short.json"
        medium_file = prompts_dir / "5_memory_medium.json"
        if not db_path.exists():
            raise SystemExit(f"Chat database does not exist: {db_path}")

        start = f"{args.date} 00:00:00"
        end = f"{args.date} 23:59:59"
        with sqlite3.connect(db_path) as conn:
            total, thoughts = conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN content LIKE '%[THOUGHTS]%' THEN 1 ELSE 0 END) "
                "FROM messages WHERE timestamp >= ? AND timestamp <= ?",
                (start, end),
            ).fetchone()
        print(f"Database messages: {total}; included [THOUGHTS] rows: {thoughts or 0}")
        print(f"Short file: {short_file}")
        print(f"Medium file: {medium_file}")
        moment_actions = _moment_actions(args.user_id, args.char_id, args.date)
        print(f"Moments actions to rebuild: {len(moment_actions)}")
        for action in moment_actions:
            print(f"MOMENT SOURCE {action['time']} | {action['context']}")
        if not args.apply:
            print("No files changed. Re-run with --apply to back up and rebuild this date.")
            return

        restored_moments = _moment_events(moment_actions)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_dir = Path(args.backup_root).resolve() / stamp / str(args.user_id) / args.char_id
        backup_dir.mkdir(parents=True, exist_ok=True)
        for path in (short_file, medium_file):
            if path.exists():
                shutil.copy2(path, backup_dir / path.name)
        print(f"Backup created: {backup_dir}")

        short_result = update_short_memory_for_date(
            args.char_id,
            args.date,
            force_reset=True,
            user_id=args.user_id,
            batch_delay_seconds=10,
            batch_retry_delays=(20, 45, 90),
        )
        print(f"Short: {short_result.status}; events={short_result.count}; {short_result.message}")
        if not short_result.ok or short_result.status != "success":
            raise SystemExit("Short-memory rebuild failed; medium memory was not changed.")

        _replace_moment_events(short_file, args.date, restored_moments)
        print(f"Moments restored: {len(restored_moments)}")

        print("Waiting 15 seconds before rebuilding medium memory...")
        time.sleep(15)

        medium_result = generate_medium_memory_for_date(
            args.char_id,
            args.date,
            user_id=args.user_id,
            batch_delay_seconds=10,
            batch_retry_delays=(20, 45, 90),
        )
        print(f"Medium: {medium_result.status}; events={medium_result.count}; {medium_result.message}")
        if medium_result.status != "success":
            raise SystemExit("Medium-memory rebuild failed; restore from the backup if needed.")
    finally:
        clear_background_user()


if __name__ == "__main__":
    main()
