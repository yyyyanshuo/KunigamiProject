#!/usr/bin/env python3
"""Translate saved Ego-assessment monologues to Simplified Chinese."""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.run_character_ego_test import (  # noqa: E402
    allow_loopback_session_cookie,
    atomic_write_json,
    load_characters,
    make_session,
    now_iso,
    read_json,
    render_report,
)
from services.character_assessment import load_questionnaire  # noqa: E402
from services.assessment_translation import translation_quality_issues  # noqa: E402


TRANSLATION_ENDPOINT = "/api/assessments/ego_8d/translate_monologues"
TRANSLATION_MODEL = "gemini-2.5-flash-lite"


def source_digest(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def parse_character_ids(raw_value: str | None) -> set[str] | None:
    values = {item.strip() for item in str(raw_value or "").split(",") if item.strip()}
    return values or None


def build_question_context(
    questionnaire: dict[str, Any],
    answer: dict[str, Any],
) -> str:
    question = next(
        (item for item in questionnaire["questions"] if item["id"] == answer.get("question_id")),
        None,
    )
    if not question:
        return ""
    option = next(
        (item for item in question["options"] if item["key"] == answer.get("choice")),
        None,
    )
    parts = [f"题目：{question['text']}"]
    if option:
        parts.append(f"角色选择：{option['text']}")
    return "\n".join(parts)


def collect_translation_jobs(
    run_dir: Path,
    questionnaire: dict[str, Any],
    selected_characters: set[str] | None = None,
    force: bool = False,
    suspect_only: bool = False,
) -> tuple[list[dict[str, Any]], dict[Path, dict[str, Any]]]:
    jobs: list[dict[str, Any]] = []
    documents: dict[Path, dict[str, Any]] = {}
    character_dir = run_dir / "characters"
    for result_path in sorted(character_dir.glob("*.json")):
        result = read_json(result_path)
        char_id = str(result.get("char_id") or result_path.stem)
        if selected_characters is not None and char_id not in selected_characters:
            continue
        documents[result_path] = result
        char_name = str(result.get("char_name") or char_id)
        for answer_index, answer in enumerate(result.get("answers") or []):
            source = str(answer.get("monologue") or "").strip()
            if not source:
                continue
            digest = source_digest(source)
            translated = str(answer.get("monologue_zh") or "").strip()
            saved_digest = str(answer.get("monologue_zh_source_sha256") or "").strip()
            up_to_date = bool(translated) and saved_digest == digest
            issues = translation_quality_issues(source, translated)
            if suspect_only:
                if up_to_date and not issues:
                    continue
            elif not force and up_to_date:
                continue
            question_id = str(answer.get("question_id") or f"answer-{answer_index + 1}")
            jobs.append(
                {
                    "id": f"{char_id}:{question_id}",
                    "source": source,
                    "character": char_name,
                    "context": build_question_context(questionnaire, answer),
                    "result_path": result_path,
                    "answer_index": answer_index,
                    "source_sha256": digest,
                }
            )
    return jobs, documents


def audit_translation_quality(
    documents: dict[Path, dict[str, Any]],
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    counts: dict[str, int] = {}
    suspects: list[dict[str, Any]] = []
    for result in documents.values():
        char_id = str(result.get("char_id") or "unknown")
        char_name = str(result.get("char_name") or char_id)
        for answer in result.get("answers") or []:
            source = str(answer.get("monologue") or "").strip()
            translated = str(answer.get("monologue_zh") or "").strip()
            issues = translation_quality_issues(source, translated)
            if not issues:
                continue
            for issue in issues:
                issue_name = issue.split(":", 1)[0]
                counts[issue_name] = counts.get(issue_name, 0) + 1
            suspects.append(
                {
                    "char_id": char_id,
                    "char_name": char_name,
                    "question_id": answer.get("question_id"),
                    "issues": issues,
                }
            )
    return counts, suspects


def request_translation_batch(
    session: Any,
    args: argparse.Namespace,
    jobs: list[dict[str, Any]],
) -> tuple[dict[str, str], str]:
    requests = __import__("requests")
    payload_items = [
        {
            "id": job["id"],
            "source": job["source"],
            "character": job["character"],
            "context": job["context"],
        }
        for job in jobs
    ]
    last_error = "未知错误"
    for attempt in range(1, args.max_retries + 1):
        try:
            allow_loopback_session_cookie(session, args.base_url)
            response = session.post(
                f"{args.base_url}{TRANSLATION_ENDPOINT}",
                json={"items": payload_items},
                timeout=args.timeout,
            )
            if response.status_code == 401:
                raise SystemExit("翻译 Session 已失效，请重新运行脚本登录。")
            if response.status_code == 200:
                data = response.json()
                if int(data.get("user_id", -1)) != args.user_id:
                    raise SystemExit("登录用户与 --user-id 不一致，已停止以防串号。")
                translations = data.get("translations")
                if not isinstance(translations, dict):
                    raise ValueError("服务器没有返回 translations 对象")
                expected = [job["id"] for job in jobs]
                if set(translations) != set(expected):
                    raise ValueError("服务器返回的翻译项目不完整")
                if any(not str(translations[item_id] or "").strip() for item_id in expected):
                    raise ValueError("服务器返回了空译文")
                return {key: str(value).strip() for key, value in translations.items()}, str(
                    data.get("model") or TRANSLATION_MODEL
                )
            last_error = f"HTTP {response.status_code}: {response.text[:500]}"
        except requests.exceptions.RequestException as exc:
            last_error = str(exc)
        except ValueError as exc:
            last_error = str(exc)

        if attempt < args.max_retries:
            time.sleep(min(args.retry_backoff * attempt, 10))
    raise RuntimeError(last_error)


def translate_with_fallback(
    session: Any,
    args: argparse.Namespace,
    jobs: list[dict[str, Any]],
) -> tuple[list[tuple[dict[str, Any], str, str]], list[tuple[dict[str, Any], str]]]:
    try:
        translations, model = request_translation_batch(session, args, jobs)
        return [(job, translations[job["id"]], model) for job in jobs], []
    except RuntimeError as exc:
        if len(jobs) == 1:
            return [], [(jobs[0], str(exc))]
        middle = len(jobs) // 2
        left_ok, left_failed = translate_with_fallback(session, args, jobs[:middle])
        right_ok, right_failed = translate_with_fallback(session, args, jobs[middle:])
        return left_ok + right_ok, left_failed + right_failed


def save_translations(
    translated: list[tuple[dict[str, Any], str, str]],
    documents: dict[Path, dict[str, Any]],
) -> set[Path]:
    changed_paths: set[Path] = set()
    translated_at = now_iso()
    for job, chinese, model in translated:
        result = documents[job["result_path"]]
        answer = result["answers"][job["answer_index"]]
        answer["monologue_zh"] = chinese
        answer["monologue_zh_model"] = model
        answer["monologue_zh_source_sha256"] = job["source_sha256"]
        answer["monologue_zh_translated_at"] = translated_at
        changed_paths.add(job["result_path"])
    for result_path in changed_paths:
        atomic_write_json(result_path, documents[result_path])
    return changed_paths


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="使用 Gemini Flash Lite 为测评碎碎念补充中文翻译")
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=ROOT_DIR / "users" / "1" / "assessments" / "ego_8d" / "all-characters",
        help="测评运行目录，默认用户 1 的 all-characters",
    )
    parser.add_argument("--characters", help="只翻译这些角色 ID，使用逗号分隔")
    parser.add_argument("--user-id", type=int, default=1)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--username", help="登录邮箱；默认从用户数据库读取")
    parser.add_argument("--password-env", default="KUNIGAMI_ASSESSMENT_PASSWORD")
    parser.add_argument("--batch-size", type=int, default=8, choices=range(1, 21), metavar="1-20")
    parser.add_argument("--limit", type=int, help="最多翻译 N 条，适合先试跑")
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--retry-backoff", type=float, default=1.5)
    parser.add_argument("--delay", type=float, default=0.5, help="每批完成后的等待秒数")
    parser.add_argument("--timeout", type=float, default=330.0)
    translation_mode = parser.add_mutually_exclusive_group()
    translation_mode.add_argument("--force", action="store_true", help="忽略原文哈希，重新翻译全部已有译文")
    translation_mode.add_argument(
        "--retranslate-suspect",
        action="store_true",
        help="只重翻空白、照抄原文、残留日语假名或错误提示的译文",
    )
    parser.add_argument("--audit", action="store_true", help="审计现有中文翻译质量，不登录或修改文件")
    parser.add_argument("--dry-run", action="store_true", help="只统计待翻译数量")
    args = parser.parse_args()
    args.base_url = args.base_url.rstrip("/")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit 必须至少为 1")
    if args.max_retries < 1:
        parser.error("--max-retries 必须至少为 1")
    if args.delay < 0 or args.retry_backoff < 0:
        parser.error("等待时间不能为负数")
    return args


def main() -> int:
    args = parse_args()
    run_dir = args.run_dir.resolve()
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(f"找不到测评 manifest：{manifest_path}")
    manifest = read_json(manifest_path)
    if int(manifest.get("user_id", -1)) != args.user_id:
        raise SystemExit("测评目录的 user_id 与命令参数不一致。")

    configured = load_characters(args.user_id)
    selected = parse_character_ids(args.characters)
    if selected:
        unknown = sorted(selected - set(configured))
        if unknown:
            raise SystemExit(f"角色配置中不存在：{', '.join(unknown)}")

    questionnaire = load_questionnaire()
    jobs, documents = collect_translation_jobs(
        run_dir,
        questionnaire,
        selected_characters=selected,
        force=args.force,
        suspect_only=args.retranslate_suspect,
    )
    quality_counts, quality_suspects = audit_translation_quality(documents)
    if args.audit:
        print(f"翻译质量审计：共发现 {len(quality_suspects)} 条可疑译文。")
        if quality_counts:
            print(
                "问题分类："
                + "；".join(f"{name}={count}" for name, count in sorted(quality_counts.items()))
            )
        for item in quality_suspects[:50]:
            print(
                f"  - {item['char_name']}({item['char_id']}) {item['question_id']}："
                + ", ".join(item["issues"])
            )
        if len(quality_suspects) > 50:
            print(f"  ……另有 {len(quality_suspects) - 50} 条未在终端展开。")
        return 1 if quality_suspects else 0
    if args.limit is not None:
        jobs = jobs[: args.limit]

    print(f"翻译模型：{TRANSLATION_MODEL}")
    print(f"测评目录：{run_dir}")
    print(f"待翻译碎碎念：{len(jobs)} 条；批大小：{args.batch_size}")
    if args.dry_run:
        print("dry-run 完成：未登录、未调用模型、未修改 JSON。")
        return 0
    if not jobs:
        report_path = render_report(run_dir, manifest, questionnaire)
        print(f"没有待翻译内容；报告已重建：{report_path}")
        return 0

    session = make_session(args)
    translated_count = 0
    failures: list[tuple[dict[str, Any], str]] = []
    for start in range(0, len(jobs), args.batch_size):
        batch = jobs[start : start + args.batch_size]
        print(
            f"[{start + 1}-{start + len(batch)}/{len(jobs)}] "
            f"{batch[0]['id']} … {batch[-1]['id']} ",
            end="",
            flush=True,
        )
        translated, failed = translate_with_fallback(session, args, batch)
        save_translations(translated, documents)
        translated_count += len(translated)
        failures.extend(failed)
        render_report(run_dir, manifest, questionnaire)
        print(f"完成 {len(translated)}，失败 {len(failed)}", flush=True)
        if args.delay and start + args.batch_size < len(jobs):
            time.sleep(args.delay)

    report_path = render_report(run_dir, manifest, questionnaire)
    print(f"\n翻译完成：新增/更新 {translated_count} 条；失败 {len(failures)} 条。")
    if failures:
        for job, error in failures:
            print(f"  - {job['id']}：{error}")
        print("重新运行相同命令会只重试未成功的条目。")
    print(f"HTML 报告：{report_path}（{report_path.stat().st_size} bytes）")
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
