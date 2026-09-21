#!/usr/bin/env python3
"""Run the 36-question Ego assessment against Kunigami characters.

The runner talks to the authenticated, read-only assessment endpoint. It saves
after every valid answer and produces a self-contained HTML report.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sqlite3
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def reexec_project_python_if_needed() -> None:
    """Use the application's virtualenv when invoked with bare system Python."""
    if os.environ.get("KUNIGAMI_ASSESSMENT_REEXEC") == "1":
        return
    try:
        import dotenv  # noqa: F401
        import requests  # noqa: F401
        return
    except ImportError:
        pass

    candidates = (
        ROOT_DIR / "venv" / "bin" / "python3",
        ROOT_DIR / ".venv" / "bin" / "python3",
    )
    current = Path(sys.executable).resolve()
    for candidate in candidates:
        if not candidate.is_file() or candidate.resolve() == current:
            continue
        environment = os.environ.copy()
        environment["KUNIGAMI_ASSESSMENT_REEXEC"] = "1"
        print(f"检测到项目虚拟环境，切换到：{candidate}", flush=True)
        os.execve(str(candidate), [str(candidate), *sys.argv], environment)


reexec_project_python_if_needed()


from services.character_assessment import (  # noqa: E402
    DEFAULT_OPENING,
    QuestionnaireError,
    load_questionnaire,
    parse_choice_reply,
    score_answers,
)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_characters(user_id: int) -> dict[str, dict[str, Any]]:
    path = ROOT_DIR / "users" / str(user_id) / "configs" / "characters.json"
    try:
        data = read_json(path)
    except FileNotFoundError as exc:
        raise SystemExit(f"找不到用户角色配置：{path}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"角色配置格式错误：{path}")
    return {key: value for key, value in data.items() if isinstance(value, dict)}


def character_name(char_id: str, info: dict[str, Any]) -> str:
    return str(info.get("remark") or info.get("name") or char_id)


def parse_character_ids(raw_value: str | None) -> list[str]:
    return [item.strip() for item in str(raw_value or "").split(",") if item.strip()]


def filter_character_ids(
    selected_ids: list[str],
    excluded_ids: list[str],
    configured: dict[str, dict[str, Any]],
) -> list[str]:
    unknown_selected = [char_id for char_id in selected_ids if char_id not in configured]
    if unknown_selected:
        raise SystemExit(
            f"以下角色不在用户配置中：{', '.join(dict.fromkeys(unknown_selected))}"
        )
    unknown_excluded = [char_id for char_id in excluded_ids if char_id not in configured]
    if unknown_excluded:
        raise SystemExit(
            f"以下排除角色不在用户配置中：{', '.join(dict.fromkeys(unknown_excluded))}"
        )
    if len(selected_ids) != len(set(selected_ids)):
        raise SystemExit("角色列表中存在重复 ID。")

    excluded = set(excluded_ids)
    filtered = [char_id for char_id in selected_ids if char_id not in excluded]
    if not filtered:
        raise SystemExit("排除后没有需要运行的角色。")
    return filtered


def discover_character_progress(
    user_id: int,
    configured: dict[str, dict[str, Any]],
    assessments_root: Path | None = None,
    total_questions: int = 36,
) -> dict[str, dict[str, Any]]:
    """Return the latest saved assessment state for each configured character."""
    root = assessments_root or ROOT_DIR / "users" / str(user_id) / "assessments" / "ego_8d"
    latest: dict[str, dict[str, Any]] = {}
    if root.is_dir():
        for run_dir in root.iterdir():
            if not run_dir.is_dir() or not (run_dir / "manifest.json").is_file():
                continue
            character_dir = run_dir / "characters"
            for char_id in configured:
                result_path = character_dir / f"{char_id}.json"
                if not result_path.is_file():
                    continue
                try:
                    result = read_json(result_path)
                    answers = result.get("answers") or []
                    answered_ids = {
                        str(item.get("question_id") or "").upper()
                        for item in answers
                        if isinstance(item, dict)
                    }
                    answered = len(
                        {
                            question_id
                            for question_id in answered_ids
                            if question_id.startswith("Q") and question_id[1:].isdigit()
                        }
                    )
                    modified_at = result_path.stat().st_mtime
                except (OSError, ValueError, TypeError):
                    continue
                previous = latest.get(char_id)
                if previous is None or modified_at > previous["modified_at"]:
                    latest[char_id] = {
                        "state": "completed" if answered >= total_questions else "incomplete",
                        "answered": min(answered, total_questions),
                        "run_dir": run_dir,
                        "run_id": run_dir.name,
                        "modified_at": modified_at,
                    }

    progress: dict[str, dict[str, Any]] = {}
    for char_id in configured:
        progress[char_id] = latest.get(
            char_id,
            {
                "state": "not_started",
                "answered": 0,
                "run_dir": None,
                "run_id": None,
                "modified_at": 0.0,
            },
        )
    return progress


def pending_character_ids(
    configured: dict[str, dict[str, Any]],
    progress: dict[str, dict[str, Any]],
) -> list[str]:
    return [char_id for char_id in configured if progress[char_id]["state"] != "completed"]


def _saved_answer_count(result: dict[str, Any]) -> int:
    answers = result.get("answers") or []
    return len(
        {
            str(item.get("question_id") or "").upper()
            for item in answers
            if isinstance(item, dict)
            and str(item.get("question_id") or "").upper().startswith("Q")
        }
    )


def ensure_shared_character_run(
    user_id: int,
    configured: dict[str, dict[str, Any]],
    questionnaire: dict[str, Any],
    progress: dict[str, dict[str, Any]],
    run_dir: Path | None = None,
) -> Path:
    """Create one stable run folder and import each character's latest result."""
    run_dir = run_dir or ROOT_DIR / "users" / str(user_id) / "assessments" / "ego_8d" / "all-characters"
    manifest_path = run_dir / "manifest.json"
    if manifest_path.is_file():
        manifest = read_json(manifest_path)
        if manifest.get("questionnaire_version") != questionnaire["questionnaire_version"]:
            raise SystemExit("all-characters 使用的是旧题库版本，请先备份并移走该目录。")
    else:
        manifest = {
            "schema_version": 1,
            "run_id": "all-characters",
            "user_id": user_id,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "questionnaire": "ego_8d",
            "questionnaire_version": questionnaire["questionnaire_version"],
            "opening_message": DEFAULT_OPENING,
            "characters": list(configured),
            "status": "partial",
        }
    manifest["characters"] = list(configured)
    manifest["updated_at"] = now_iso()
    atomic_write_json(manifest_path, manifest)

    target_dir = run_dir / "characters"
    target_dir.mkdir(parents=True, exist_ok=True)
    for char_id, state in progress.items():
        source_run_dir = state.get("run_dir")
        if source_run_dir is None or Path(source_run_dir).resolve() == run_dir.resolve():
            continue
        source_path = Path(source_run_dir) / "characters" / f"{char_id}.json"
        target_path = target_dir / f"{char_id}.json"
        try:
            source_result = read_json(source_path)
        except (OSError, ValueError):
            continue
        if target_path.is_file():
            try:
                target_result = read_json(target_path)
                if _saved_answer_count(target_result) >= _saved_answer_count(source_result):
                    continue
            except (OSError, ValueError):
                pass
        source_result.setdefault("source_run_id", source_result.get("run_id"))
        source_result["run_id"] = "all-characters"
        atomic_write_json(target_path, source_result)
    return run_dir


def all_manifest_characters_completed(run_dir: Path, manifest: dict[str, Any]) -> bool:
    for char_id in manifest["characters"]:
        result_path = run_dir / "characters" / f"{char_id}.json"
        if not result_path.is_file():
            return False
        try:
            result = read_json(result_path)
        except (OSError, ValueError, TypeError):
            return False
        if _saved_answer_count(result) < 36:
            return False
    return True


def choose_pending_character(
    configured: dict[str, dict[str, Any]],
    progress: dict[str, dict[str, Any]],
    input_fn=input,
    output_fn=print,
) -> tuple[str | None, Path | None]:
    pending = pending_character_ids(configured, progress)
    completed_count = len(configured) - len(pending)
    output_fn(f"\n用户 1 角色测评进度：已完成 {completed_count}/{len(configured)}")
    if not pending:
        output_fn("所有已配置角色都完成了 36 题测评。")
        return None, None

    for index, char_id in enumerate(pending, 1):
        info = progress[char_id]
        if info["state"] == "incomplete":
            status = f"未完成 {info['answered']}/36，将继续 {info['run_id']}"
        else:
            status = "未开始，将创建新测评"
        output_fn(f"  {index}. {character_name(char_id, configured[char_id])} ({char_id}) — {status}")
    output_fn("  0. 退出")

    while True:
        raw_choice = input_fn("请输入角色序号：").strip()
        if raw_choice == "0":
            return None, None
        try:
            selected_index = int(raw_choice)
        except ValueError:
            selected_index = -1
        if 1 <= selected_index <= len(pending):
            char_id = pending[selected_index - 1]
            return char_id, progress[char_id]["run_dir"]
        output_fn(f"请输入 0 到 {len(pending)} 之间的序号。")


def require_requests():
    try:
        import requests
    except ImportError as exc:
        raise SystemExit("缺少 requests 依赖，请先执行 pip install -r requirements.txt。") from exc
    return requests


def lookup_user_email(user_id: int, users_db: Path | None = None) -> str:
    users_db = users_db or ROOT_DIR / "configs" / "users.db"
    if not users_db.is_file():
        return ""
    try:
        connection = sqlite3.connect(f"file:{users_db}?mode=ro", uri=True)
        try:
            row = connection.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return ""
    return str(row[0] or "").strip() if row else ""


def allow_loopback_session_cookie(session: Any, base_url: str) -> None:
    """Allow a refreshed Secure session cookie on the private HTTP listener."""
    parsed_url = urlparse(base_url)
    if parsed_url.scheme != "http" or parsed_url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return
    for cookie in session.cookies:
        if cookie.name == "session":
            cookie.secure = False


def make_session(args: argparse.Namespace):
    requests = require_requests()
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "User-Agent": "Kunigami-Ego-Assessment/1.0"})

    raw_cookie = os.getenv("KUNIGAMI_ASSESSMENT_SESSION_COOKIE", "").strip()
    if raw_cookie:
        value = raw_cookie.split("session=", 1)[-1].split(";", 1)[0].strip()
        session.cookies.set("session", value)
        return session

    username = (
        args.username
        or os.getenv("KUNIGAMI_ASSESSMENT_USERNAME", "")
        or lookup_user_email(args.user_id)
    ).strip()
    password = os.getenv(args.password_env, "")
    if username and not password and sys.stdin.isatty():
        password = getpass.getpass(f"请输入用户 {args.user_id}（{username}）的登录密码：")
    if not username or not password:
        raise SystemExit(
            "无法取得登录邮箱或密码。交互终端可直接输入密码；后台运行请设置 "
            f"KUNIGAMI_ASSESSMENT_USERNAME 和 {args.password_env}，"
            "或设置 KUNIGAMI_ASSESSMENT_SESSION_COOKIE。"
        )
    try:
        response = session.post(
            f"{args.base_url}/api/login",
            json={"username": username, "password": password},
            timeout=args.timeout,
        )
    except requests.exceptions.RequestException as exc:
        raise SystemExit(f"连接登录接口失败：{exc}") from exc
    if response.status_code != 200:
        raise SystemExit(f"登录失败（HTTP {response.status_code}）：{response.text[:300]}")
    allow_loopback_session_cookie(session, args.base_url)
    return session


def get_option(question: dict[str, Any], choice: str) -> dict[str, Any]:
    return next(option for option in question["options"] if option["key"] == choice)


def render_report(run_dir: Path, manifest: dict[str, Any], questionnaire: dict[str, Any]) -> Path:
    characters = []
    for char_id in manifest["characters"]:
        result_path = run_dir / "characters" / f"{char_id}.json"
        if result_path.exists():
            characters.append(read_json(result_path))
    payload = {
        "schema_version": 1,
        "run_id": manifest["run_id"],
        "created_at": manifest["created_at"],
        "opening_message": manifest["opening_message"],
        "questionnaire": questionnaire,
        "characters": characters,
    }
    embedded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    embedded = embedded.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    template_path = ROOT_DIR / "templates" / "character_ego_report.html"
    template = template_path.read_text(encoding="utf-8")
    placeholder = "__ASSESSMENT_DATA_JSON__"
    if template.count(placeholder) != 1:
        raise RuntimeError(f"报告模板中的 {placeholder} 占位符必须恰好出现一次")
    report_path = run_dir / "report.html"
    atomic_write_text(report_path, template.replace(placeholder, embedded))
    if not report_path.is_file():
        raise RuntimeError(f"报告写入后不存在：{report_path}")
    if report_path.stat().st_size <= 0:
        raise RuntimeError(f"报告写入后是空文件：{report_path}")
    return report_path


def update_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = now_iso()
    atomic_write_json(run_dir / "manifest.json", manifest)


def initial_character_result(
    manifest: dict[str, Any],
    char_id: str,
    info: dict[str, Any],
    questionnaire: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": manifest["run_id"],
        "user_id": manifest["user_id"],
        "char_id": char_id,
        "char_name": character_name(char_id, info),
        "status": "running",
        "opening_message": manifest["opening_message"],
        "questionnaire_version": questionnaire["questionnaire_version"],
        "started_at": now_iso(),
        "completed_at": None,
        "route": None,
        "model": None,
        "answers": [],
        "failures": [],
        "dimensions": {name: 0 for name in questionnaire["dimensions"]},
        "none_count": 0,
        "hidden_ending": None,
    }


def request_answer(
    session: Any,
    args: argparse.Namespace,
    char_id: str,
    question: dict[str, Any],
    opening_message: str,
    previous_answers: list[dict[str, Any]],
) -> dict[str, Any]:
    requests = require_requests()
    retry_instruction = ""
    last_error = "未知错误"
    for attempt in range(1, args.max_retries + 1):
        started = time.monotonic()
        try:
            # Flask refreshes permanent sessions after a response and restores
            # Secure=True, so localhost HTTP needs this before every request.
            allow_loopback_session_cookie(session, args.base_url)
            response = session.post(
                f"{args.base_url}/api/{char_id}/assessment_chat",
                json={
                    "question_id": question["id"],
                    "opening_message": opening_message,
                    "retry_instruction": retry_instruction,
                    "previous_answers": [
                        {"question_id": item["question_id"], "choice": item["choice"]}
                        for item in previous_answers
                    ],
                },
                timeout=args.timeout,
            )
            latency_ms = round((time.monotonic() - started) * 1000)
            if response.status_code == 401:
                raise SystemExit("测评 Session 已失效，请重新登录后再运行。")
            if response.status_code == 404:
                raise SystemExit(f"服务器中不存在角色 {char_id}。")
            if response.status_code >= 400:
                last_error = f"HTTP {response.status_code}: {response.text[:300]}"
            else:
                data = response.json()
                if int(data.get("user_id", -1)) != args.user_id:
                    raise SystemExit(
                        f"登录账号是用户 {data.get('user_id')}，但命令要求用户 {args.user_id}；已停止以防串号。"
                    )
                parsed = parse_choice_reply(data.get("raw_reply", ""), question["id"])
                return {
                    **parsed,
                    "raw_reply": data.get("raw_reply", ""),
                    "route": data.get("route"),
                    "model": data.get("model"),
                    "attempts": attempt,
                    "latency_ms": latency_ms,
                }
        except QuestionnaireError as exc:
            last_error = str(exc)
        except requests.exceptions.RequestException as exc:
            last_error = str(exc)
        except ValueError as exc:
            last_error = f"服务器返回的不是有效 JSON：{exc}"

        retry_instruction = f"{last_error}。请严格按两行格式重新回答 {question['id']}。"
        if attempt < args.max_retries:
            time.sleep(min(args.retry_backoff * attempt, 10))
    raise QuestionnaireError(f"连续 {args.max_retries} 次未得到有效回答：{last_error}")


def run_character(
    session: Any,
    args: argparse.Namespace,
    run_dir: Path,
    manifest: dict[str, Any],
    questionnaire: dict[str, Any],
    questions: list[dict[str, Any]],
    char_id: str,
    info: dict[str, Any],
) -> dict[str, Any]:
    result_path = run_dir / "characters" / f"{char_id}.json"
    result = read_json(result_path) if result_path.exists() else initial_character_result(
        manifest, char_id, info, questionnaire
    )
    completed_ids = {answer["question_id"] for answer in result["answers"]}
    result["status"] = "running"
    atomic_write_json(result_path, result)

    print(f"\n[{result['char_name']}] 开始或继续测评，已完成 {len(completed_ids)} 题。", flush=True)
    for question in questions:
        if question["id"] in completed_ids:
            continue
        print(f"  {question['id']}/Q36 {question['short_title']} ... ", end="", flush=True)
        try:
            generated = request_answer(
                session,
                args,
                char_id,
                question,
                manifest["opening_message"],
                result["answers"],
            )
        except QuestionnaireError as exc:
            print("失败", flush=True)
            result["status"] = "incomplete"
            result["failures"].append(
                {"question_id": question["id"], "error": str(exc), "timestamp": now_iso()}
            )
            atomic_write_json(result_path, result)
            return result

        option = get_option(question, generated["choice"])
        answer = {
            "question_id": question["id"],
            "module": question["module"],
            "choice": generated["choice"],
            "choice_tag": generated["choice_tag"],
            "monologue": generated["monologue"],
            "raw_reply": generated["raw_reply"],
            "score_delta": option["scores"],
            "attempts": generated["attempts"],
            "latency_ms": generated["latency_ms"],
            "answered_at": now_iso(),
        }
        result["answers"].append(answer)
        scored = score_answers(questionnaire, result["answers"])
        result.update(scored)
        result["route"] = generated["route"]
        result["model"] = generated["model"]
        atomic_write_json(result_path, result)
        render_report(run_dir, manifest, questionnaire)
        print(f"{generated['choice']}（{generated['attempts']} 次）", flush=True)
        if args.delay:
            time.sleep(args.delay)

    target_count = len(questions)
    completed_target = sum(1 for answer in result["answers"] if answer["question_id"] in {q["id"] for q in questions})
    result["status"] = "completed" if target_count == 36 and completed_target == 36 else "partial"
    result["completed_at"] = now_iso()
    atomic_write_json(result_path, result)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="让用户角色完成 Ego 八维题目并生成 HTML 报告")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--characters", help="逗号分隔的角色 ID，例如 kunigami,isagi")
    selection.add_argument("--all-configured", action="store_true", help="运行该用户配置中的全部角色")
    selection.add_argument(
        "--all-pending",
        action="store_true",
        help="扫描并运行全部未完成角色，统一保存到 all-characters",
    )
    parser.add_argument(
        "--exclude",
        help="逗号分隔的排除角色 ID，可与 --all-configured 或 --all-pending 一起使用",
    )
    parser.add_argument("--user-id", type=int, default=1, help="预期登录用户 ID，默认 1")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Kunigami 服务地址")
    parser.add_argument("--username", help="登录邮箱；默认读取 KUNIGAMI_ASSESSMENT_USERNAME")
    parser.add_argument("--password-env", default="KUNIGAMI_ASSESSMENT_PASSWORD", help="保存登录密码的环境变量名")
    parser.add_argument("--opening", help="自定义开场白")
    parser.add_argument("--opening-file", type=Path, help="从 UTF-8 文件读取开场白")
    parser.add_argument("--run-id", help="自定义运行 ID")
    parser.add_argument("--output-dir", type=Path, help="本次运行的输出目录")
    parser.add_argument("--resume", type=Path, help="继续一个已有运行目录")
    parser.add_argument("--limit-questions", type=int, choices=range(1, 37), metavar="1-36", help="仅运行前 N 题，适合本地试跑")
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--retry-backoff", type=float, default=1.5)
    parser.add_argument("--delay", type=float, default=1.0, help="每道题成功后的等待秒数")
    parser.add_argument("--timeout", type=float, default=330.0, help="单次 HTTP 请求超时秒数")
    parser.add_argument("--dry-run", action="store_true", help="只检查题库、角色与参数，不调用模型")
    parser.add_argument("--report-only", action="store_true", help="只从已有 JSON 重建并验证 HTML，不登录或调用模型")
    args = parser.parse_args()
    args.base_url = args.base_url.rstrip("/")
    if args.max_retries < 1:
        parser.error("--max-retries 必须至少为 1")
    if args.delay < 0 or args.retry_backoff < 0:
        parser.error("等待时间不能为负数")
    if args.opening and args.opening_file:
        parser.error("--opening 与 --opening-file 不能同时使用")
    if args.report_only and not args.resume:
        parser.error("--report-only 必须和 --resume 一起使用")
    if args.all_pending and args.resume:
        parser.error("--all-pending 会自动选择续跑目录，不能再指定 --resume")
    return args


def main() -> int:
    args = parse_args()
    questionnaire = load_questionnaire()
    configured = load_characters(args.user_id)
    interactive_char_id = None
    batch_pending_ids = None
    excluded_ids = parse_character_ids(args.exclude)

    if args.all_pending:
        progress = discover_character_progress(args.user_id, configured)
        shared_run_dir = ensure_shared_character_run(
            args.user_id,
            configured,
            questionnaire,
            progress,
        )
        progress = discover_character_progress(args.user_id, configured)
        batch_pending_ids = pending_character_ids(configured, progress)
        args.resume = shared_run_dir

    if not args.resume and not args.characters and not args.all_configured:
        if not sys.stdin.isatty():
            raise SystemExit(
                "当前不是交互终端，无法显示角色菜单；请使用 --characters、--resume 或 --all-configured。"
            )
        progress = discover_character_progress(args.user_id, configured)
        shared_run_dir = ensure_shared_character_run(
            args.user_id,
            configured,
            questionnaire,
            progress,
        )
        progress = discover_character_progress(args.user_id, configured)
        interactive_char_id, _ = choose_pending_character(configured, progress)
        if interactive_char_id is None:
            manifest = read_json(shared_run_dir / "manifest.json")
            if any((shared_run_dir / "characters" / f"{char_id}.json").is_file() for char_id in configured):
                render_report(shared_run_dir, manifest, questionnaire)
            return 0
        args.resume = shared_run_dir

    if args.resume:
        run_dir = args.resume.resolve()
        manifest = read_json(run_dir / "manifest.json")
        if int(manifest["user_id"]) != args.user_id:
            raise SystemExit("恢复目录的 user_id 与命令参数不一致。")
        if manifest["questionnaire_version"] != questionnaire["questionnaire_version"]:
            raise SystemExit("题库已变化，不能直接恢复旧运行；请新建一次运行。")
        if interactive_char_id:
            selected_ids = [interactive_char_id]
        elif batch_pending_ids is not None:
            selected_ids = batch_pending_ids
        elif args.characters:
            selected_ids = parse_character_ids(args.characters)
        elif args.all_configured:
            selected_ids = list(configured)
        else:
            selected_ids = list(manifest["characters"])
    else:
        selected_ids = list(configured) if args.all_configured else parse_character_ids(args.characters)
        if args.opening_file:
            opening = args.opening_file.read_text(encoding="utf-8-sig").strip()
        else:
            opening = (args.opening or DEFAULT_OPENING).strip()
        run_id = args.run_id or f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        run_dir = (args.output_dir or ROOT_DIR / "users" / str(args.user_id) / "assessments" / "ego_8d" / run_id).resolve()
        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "user_id": args.user_id,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "questionnaire": "ego_8d",
            "questionnaire_version": questionnaire["questionnaire_version"],
            "opening_message": opening,
            "characters": selected_ids,
            "status": "running",
        }

    selected_ids = filter_character_ids(selected_ids, excluded_ids, configured)
    questions = questionnaire["questions"][: args.limit_questions] if args.limit_questions else questionnaire["questions"]

    print(f"题库校验通过：{len(questionnaire['questions'])} 题，版本 {questionnaire['questionnaire_version']}")
    print("准备运行角色：" + "、".join(f"{character_name(cid, configured[cid])}({cid})" for cid in selected_ids))
    print(f"本次目标：每个角色 {len(questions)} 题；输出目录：{run_dir}")
    print(f"开场白：{manifest['opening_message']}")
    if args.dry_run:
        print("dry-run 完成：未登录、未调用模型。")
        return 0
    if args.report_only:
        report_path = render_report(run_dir, manifest, questionnaire)
        print(f"报告已从 JSON 重建并验证：{report_path}（{report_path.stat().st_size} bytes）")
        return 0

    update_manifest(run_dir, manifest)
    session = make_session(args)
    any_incomplete = False
    for char_id in selected_ids:
        result = run_character(
            session,
            args,
            run_dir,
            manifest,
            questionnaire,
            questions,
            char_id,
            configured[char_id],
        )
        if result["status"] == "incomplete":
            any_incomplete = True
        render_report(run_dir, manifest, questionnaire)

    if any_incomplete:
        manifest["status"] = "incomplete"
    elif len(questions) == 36 and all_manifest_characters_completed(run_dir, manifest):
        manifest["status"] = "completed"
    else:
        manifest["status"] = "partial"
    update_manifest(run_dir, manifest)
    report_path = render_report(run_dir, manifest, questionnaire)
    print(f"\n运行结束。HTML 报告：{report_path}（{report_path.stat().st_size} bytes）")
    return 2 if any_incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
