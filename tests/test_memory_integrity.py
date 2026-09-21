import json
import importlib.util
import sqlite3
import threading
from pathlib import Path

from services import memory
from services import ai_client
from services.ai_client import AIResponseText
from services.memory_store import (
    append_short_memory_events,
    replace_short_memory_events_by_prefix,
)
from blueprints import group as group_blueprint


def _write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _make_chat_db(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, role TEXT, content TEXT, timestamp TEXT)"
        )
        conn.executemany(
            "INSERT INTO messages (id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            rows,
        )


def _patch_character(monkeypatch, db_path: Path, prompts_dir: Path):
    monkeypatch.setattr(memory, "get_paths", lambda *args, **kwargs: (str(db_path), str(prompts_dir)))
    monkeypatch.setattr(memory, "_get_memory_character_name", lambda *args, **kwargs: "凛")
    monkeypatch.setattr(memory, "_load_user_settings", lambda *args, **kwargs: {"current_user_name": "樱"})


def test_force_short_rebuild_keeps_external_sources_and_beijing_early_hours(tmp_path, monkeypatch):
    db_path = tmp_path / "chat.db"
    prompts_dir = tmp_path / "prompts"
    short_file = prompts_dir / "6_memory_short.json"
    _make_chat_db(db_path, [
        (1, "user", "凌晨发生的事情", "2026-08-07 00:30:00"),
        (2, "assistant", "凌晨回复", "2026-08-07 00:31:00"),
        (3, "assistant", "[THOUGHTS]\n睡前日记\n[/THOUGHTS]", "2026-08-07 04:29:00"),
        (4, "user", "下午发生的事情", "2026-08-07 13:00:00"),
    ])
    _write_json(short_file, {
        "2026-08-07": {
            "last_id": 2,
            "events": [
                {"time": "09:00", "event": "[朋友圈] 发了一条动态"},
                {"time": "10:00", "event": "[群聊:测试群] 讨论行程"},
                {"time": "11:00", "event": "手工输入内容"},
            ],
        }
    })
    _patch_character(monkeypatch, db_path, prompts_dir)
    monkeypatch.setattr(memory, "SHORT_MEMORY_BATCH_MESSAGES", 2)
    calls = []

    def fake_summary(source, *_args, **_kwargs):
        calls.append(source)
        assert "[THOUGHTS]" not in source
        if "00:30" in source:
            return "- [00:30] 凌晨的重要事件"
        return "- [13:00] 下午的重要事件"

    monkeypatch.setattr(memory, "call_ai_to_summarize", fake_summary)
    result = memory.update_short_memory_for_date("rin", "2026-08-07", force_reset=True)

    assert result.status == "success"
    assert len(calls) == 2
    saved = json.loads(short_file.read_text(encoding="utf-8"))["2026-08-07"]
    assert saved["last_id"] == 4
    texts = [event["event"] for event in saved["events"]]
    assert "凌晨的重要事件" in texts
    assert "下午的重要事件" in texts
    assert "[朋友圈] 发了一条动态" in texts
    assert "[群聊:测试群] 讨论行程" in texts
    assert "手工输入内容" not in texts
    assert all("睡前日记" not in text for text in texts)


def test_force_short_rebuild_without_private_messages_clears_only_private_entries(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "chat.db"
    prompts_dir = tmp_path / "prompts"
    short_file = prompts_dir / "6_memory_short.json"
    _make_chat_db(db_path, [])
    _write_json(short_file, {
        "2026-08-07": {
            "last_id": 9,
            "events": [
                {"time": "09:00", "event": "旧私聊摘要"},
                {"time": "10:00", "event": "[群聊:测试群] 完整群聊事件"},
                {"time": "11:00", "event": "[朋友圈] 一条朋友圈记忆"},
            ],
        }
    })
    _patch_character(monkeypatch, db_path, prompts_dir)

    result = memory.update_short_memory_for_date(
        "rin", "2026-08-07", force_reset=True
    )

    assert result.status == "success"
    saved = json.loads(short_file.read_text(encoding="utf-8"))["2026-08-07"]
    assert saved["last_id"] == 0
    assert [item["event"] for item in saved["events"]] == [
        "[群聊:测试群] 完整群聊事件",
        "[朋友圈] 一条朋友圈记忆",
    ]


def test_short_partial_failure_never_overwrites_or_advances_cursor(tmp_path, monkeypatch):
    db_path = tmp_path / "chat.db"
    prompts_dir = tmp_path / "prompts"
    short_file = prompts_dir / "6_memory_short.json"
    _make_chat_db(db_path, [
        (1, "user", "第一件事", "2026-08-07 00:10:00"),
        (2, "user", "第二件事", "2026-08-07 13:10:00"),
    ])
    original = {"2026-08-07": {"events": [{"time": "08:00", "event": "旧记忆"}], "last_id": 0}}
    _write_json(short_file, original)
    _patch_character(monkeypatch, db_path, prompts_dir)
    monkeypatch.setattr(memory, "SHORT_MEMORY_BATCH_MESSAGES", 1)
    responses = iter(["- [00:10] 第一件事", None])
    monkeypatch.setattr(memory, "call_ai_to_summarize", lambda *_a, **_k: next(responses))

    result = memory.update_short_memory_for_date("rin", "2026-08-07", force_reset=True)

    assert result.status == "partial_failure"
    assert json.loads(short_file.read_text(encoding="utf-8")) == original


def test_external_event_appended_during_ai_is_merged_at_commit(tmp_path, monkeypatch):
    db_path = tmp_path / "chat.db"
    prompts_dir = tmp_path / "prompts"
    short_file = prompts_dir / "6_memory_short.json"
    _make_chat_db(db_path, [
        (1, "user", "当天私聊", "2026-08-07 12:00:00"),
    ])
    _write_json(short_file, {"2026-08-07": {"events": [], "last_id": 0}})
    _patch_character(monkeypatch, db_path, prompts_dir)

    def fake_summary(*_args, **_kwargs):
        append_short_memory_events(
            str(short_file),
            "2026-08-07",
            [{"time": "12:01", "event": "[朋友圈] 总结期间新增的动态"}],
        )
        return "- [12:00] 当天私聊"

    monkeypatch.setattr(memory, "call_ai_to_summarize", fake_summary)
    result = memory.update_short_memory_for_date("rin", "2026-08-07", force_reset=True)

    assert result.status == "success"
    texts = [
        item["event"]
        for item in json.loads(short_file.read_text(encoding="utf-8"))["2026-08-07"]["events"]
    ]
    assert texts == ["当天私聊", "[朋友圈] 总结期间新增的动态"]


def test_truncated_transport_response_is_rejected():
    response = AIResponseText("- [00:10] 只有半段", complete=False, finish_reason="length")
    assert memory.normalize_memory_summary_output(response, "short") is None
    assert memory.normalize_memory_summary_output(response, "medium") is None


def test_short_parser_accepts_json_and_legacy_time_variants():
    valid, events = memory._parse_short_summary(
        '```json\n{"events":[{"time":"9:05","event":"JSON事件"}]}\n```'
    )
    assert valid is True
    assert events == [{"time": "09:05", "event": "JSON事件"}]

    valid, events = memory._parse_short_summary(
        "### 短期记忆\n- ［2026-08-07 9：06］ 全角日期事件"
    )
    assert valid is True
    assert events == [{"time": "09:06", "event": "全角日期事件"}]


def test_short_retries_truncation_and_format_before_atomic_commit(tmp_path, monkeypatch):
    db_path = tmp_path / "chat.db"
    prompts_dir = tmp_path / "prompts"
    short_file = prompts_dir / "6_memory_short.json"
    _make_chat_db(db_path, [
        (1, "user", "需要总结的完整事件", "2026-08-07 09:00:00"),
    ])
    _write_json(short_file, {"2026-08-07": {"events": [], "last_id": 0}})
    _patch_character(monkeypatch, db_path, prompts_dir)
    calls = []
    responses = iter([
        AIResponseText('{"events":[', complete=False, finish_reason="length"),
        "以下是短期记忆：\n[09:00] 仍带有非法前言",
        '{"events":[{"time":"09:00","event":"用户提出并完成了一件需要记录的事情。"}]}',
    ])

    def fake_summary(*_args, **kwargs):
        calls.append(kwargs)
        return next(responses)

    monkeypatch.setattr(memory, "call_ai_to_summarize", fake_summary)
    result = memory.update_short_memory_for_date(
        "rin", "2026-08-07", force_reset=True
    )

    assert result.status == "success"
    assert len(calls) == 3
    assert [item["max_tokens"] for item in calls] == [4096, 8192, 12288]
    assert calls[2]["format_reminder"]
    saved = json.loads(short_file.read_text(encoding="utf-8"))["2026-08-07"]
    assert saved["last_id"] == 1
    assert saved["events"] == [{
        "time": "09:00",
        "event": "用户提出并完成了一件需要记录的事情。",
    }]


def test_memory_output_retry_does_not_retry_api_errors(monkeypatch):
    calls = []

    def failed_call(*_args, **_kwargs):
        calls.append(1)
        return None

    monkeypatch.setattr(memory, "call_ai_to_summarize", failed_call)
    result = memory._summary_with_output_retries(
        "素材", "medium", "rin"
    )

    assert result.status == "api_error"
    assert len(calls) == 1

    calls.clear()

    def safety_stop(*_args, **_kwargs):
        calls.append(1)
        return AIResponseText("未完成", complete=False, finish_reason="SAFETY")

    monkeypatch.setattr(memory, "call_ai_to_summarize", safety_stop)
    result = memory._summary_with_output_retries(
        "素材", "medium", "rin"
    )
    assert result.status == "api_error"
    assert len(calls) == 1


def test_short_summary_quality_rejects_line_by_line_transcript_copy():
    rows = [
        (1, "2026-08-07 00:00:00", "assistant", "食い終わったか/味はどうだった"),
        (2, "2026-08-07 00:01:00", "user", "明日の行き先を考えてる"),
        (3, "2026-08-07 00:02:00", "assistant", "好きな店を予約しろ"),
        (4, "2026-08-07 00:03:00", "user", "いらない"),
    ]
    copied = [
        {"time": "00:00", "event": "糸師凛: 食い終わったか/味はどうだった"},
        {"time": "00:01", "event": "ユーザー: 明日の行き先を考えてる"},
        {"time": "00:02", "event": "糸師凛: 好きな店を予約しろ"},
        {"time": "00:03", "event": "ユーザー: いらない"},
    ]
    summarized = [
        {"time": "00:00", "event": "食事の感想を尋ねた。"},
        {"time": "00:01", "event": "相手は翌日の行き先と店を検討し、金銭援助は断った。"},
    ]

    assert memory._short_summary_is_compressed(rows, copied) is False
    assert memory._short_summary_is_compressed(rows, summarized) is True


def test_short_rebuild_rejects_transcript_copy_without_overwriting(tmp_path, monkeypatch):
    db_path = tmp_path / "chat.db"
    prompts_dir = tmp_path / "prompts"
    short_file = prompts_dir / "6_memory_short.json"
    rows = [
        (1, "user", "第一句", "2026-08-07 00:01:00"),
        (2, "assistant", "第二句", "2026-08-07 00:02:00"),
        (3, "user", "第三句", "2026-08-07 00:03:00"),
        (4, "assistant", "第四句", "2026-08-07 00:04:00"),
    ]
    _make_chat_db(db_path, rows)
    original = {"2026-08-07": {"events": [{"time": "08:00", "event": "旧记忆"}], "last_id": 0}}
    _write_json(short_file, original)
    _patch_character(monkeypatch, db_path, prompts_dir)
    copied = "\n".join([
        "- [00:01] 樱: 第一句",
        "- [00:02] 凛: 第二句",
        "- [00:03] 樱: 第三句",
        "- [00:04] 凛: 第四句",
    ])
    monkeypatch.setattr(memory, "call_ai_to_summarize", lambda *_a, **_k: copied)

    result = memory.update_short_memory_for_date("rin", "2026-08-07", force_reset=True)

    assert result.status == "partial_failure"
    assert "未真正压缩" in result.message
    assert json.loads(short_file.read_text(encoding="utf-8")) == original


def test_gemini_combines_all_text_parts_and_marks_truncation(monkeypatch):
    class Response:
        status_code = 200
        text = "ok"

        def json(self):
            return {
                "candidates": [{
                    "finishReason": "MAX_TOKENS",
                    "content": {"parts": [{"text": "第一段"}, {"text": "第二段"}]},
                }]
            }

    monkeypatch.setattr(ai_client, "get_effective_gemini_key", lambda **_kwargs: "test-key")
    monkeypatch.setattr(ai_client.requests, "post", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(ai_client, "log_full_prompt", lambda *_args, **_kwargs: None)
    result = ai_client.call_gemini(
        [{"role": "user", "content": "test"}], model_name="test-model"
    )

    assert str(result) == "第一段第二段"
    assert result.complete is False
    assert result.finish_reason == "MAX_TOKENS"


def test_gemini_honors_task_specific_max_output_tokens(monkeypatch):
    captured = {}

    class Response:
        status_code = 200
        text = "ok"

        def json(self):
            return {
                "candidates": [{
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": "完成"}]},
                }]
            }

    def fake_post(*_args, **kwargs):
        captured.update(kwargs["json"])
        return Response()

    monkeypatch.setattr(ai_client, "get_effective_gemini_key", lambda **_kwargs: "test-key")
    monkeypatch.setattr(ai_client.requests, "post", fake_post)
    monkeypatch.setattr(ai_client, "log_full_prompt", lambda *_args, **_kwargs: None)

    result = ai_client.call_gemini(
        [{"role": "user", "content": "test"}],
        model_name="test-model",
        max_tokens=8192,
    )

    assert str(result) == "完成"
    assert captured["generationConfig"]["maxOutputTokens"] == 8192


def test_gemini_timeout_is_structured_and_not_automatically_retried(monkeypatch):
    calls = []

    def fake_post(*_args, **kwargs):
        calls.append(kwargs)
        raise ai_client.requests.exceptions.Timeout("read timed out")

    monkeypatch.setattr(ai_client, "get_effective_gemini_key", lambda **_kwargs: "test-key")
    monkeypatch.setattr(ai_client.requests, "post", fake_post)

    result = ai_client.call_gemini(
        [{"role": "user", "content": "test"}], model_name="test-model"
    )

    assert len(calls) == 1
    assert calls[0]["timeout"] == (
        ai_client.GEMINI_CONNECT_TIMEOUT_SECONDS,
        ai_client.GEMINI_READ_TIMEOUT_SECONDS,
    )
    assert result.error_code == "ai_timeout"
    assert result.retryable is True
    assert result.status_code == 504


def test_only_gemini_503_is_automatically_retried(monkeypatch):
    statuses = iter([503, 503, 200])
    calls = []
    sleeps = []

    class Response:
        text = "temporary"

        def __init__(self, status_code):
            self.status_code = status_code

        def json(self):
            return {
                "candidates": [{
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": "完成"}]},
                }]
            }

    def fake_post(*_args, **_kwargs):
        calls.append(1)
        return Response(next(statuses))

    monkeypatch.setattr(ai_client, "get_effective_gemini_key", lambda **_kwargs: "test-key")
    monkeypatch.setattr(ai_client.requests, "post", fake_post)
    monkeypatch.setattr(ai_client.time, "sleep", sleeps.append)
    monkeypatch.setattr(ai_client, "log_full_prompt", lambda *_args, **_kwargs: None)

    result = ai_client.call_gemini(
        [{"role": "user", "content": "test"}], model_name="test-model"
    )

    assert str(result) == "完成"
    assert len(calls) == 3
    assert sleeps == [1, 2]


def test_gemini_500_is_returned_without_automatic_retry(monkeypatch):
    calls = []

    class Response:
        status_code = 500
        text = "server error"

    def fake_post(*_args, **_kwargs):
        calls.append(1)
        return Response()

    monkeypatch.setattr(ai_client, "get_effective_gemini_key", lambda **_kwargs: "test-key")
    monkeypatch.setattr(ai_client.requests, "post", fake_post)
    monkeypatch.setattr(ai_client, "log_api_error", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ai_client, "mark_api_fatal_error", lambda *_args, **_kwargs: None)

    result = ai_client.call_gemini(
        [{"role": "user", "content": "test"}], model_name="test-model"
    )

    assert len(calls) == 1
    assert result.error_code == "gemini_500"
    assert result.retryable is True


def test_medium_memory_compacts_many_events_to_few_short_paragraphs(tmp_path, monkeypatch):
    prompts_dir = tmp_path / "prompts"
    short_file = prompts_dir / "6_memory_short.json"
    events = [{"time": f"{index:02d}:00", "event": f"重要事件{index}"} for index in range(12)]
    _write_json(short_file, {"2026-08-07": {"events": events, "last_id": 12}})
    monkeypatch.setattr(memory, "get_paths", lambda *args, **kwargs: (str(tmp_path / "chat.db"), str(prompts_dir)))
    output = "上午围绕训练安排完成了讨论并确定方案。\n\n下午处理了另一件重要事项，结论已经明确。"
    calls = []

    def fake_summary(source, *_args, **_kwargs):
        calls.append(source)
        return output

    monkeypatch.setattr(memory, "call_ai_to_summarize", fake_summary)

    result = memory.generate_medium_memory_for_date("rin", "2026-08-07")

    assert result.status == "success"
    assert result.count == 2
    assert len(calls) == 1
    assert "[00:00] 重要事件0" in calls[0]
    assert "[11:00] 重要事件11" in calls[0]
    medium = json.loads((prompts_dir / "5_memory_medium.json").read_text(encoding="utf-8"))
    assert medium["2026-08-07"] == output


def test_medium_retries_truncation_and_format_only(tmp_path, monkeypatch):
    prompts_dir = tmp_path / "prompts"
    _write_json(prompts_dir / "6_memory_short.json", {
        "2026-08-07": {
            "events": [{"time": "09:00", "event": "当天的重要事件"}],
            "last_id": 1,
        }
    })
    monkeypatch.setattr(
        memory,
        "get_paths",
        lambda *args, **kwargs: (str(tmp_path / "chat.db"), str(prompts_dir)),
    )
    responses = iter([
        AIResponseText("半段", complete=False, finish_reason="length"),
        "【中期记忆】",
        "当天的重要事件得到妥善处理，相关结论已经明确。",
    ])
    calls = []

    def fake_summary(*_args, **kwargs):
        calls.append(kwargs)
        return next(responses)

    monkeypatch.setattr(memory, "call_ai_to_summarize", fake_summary)
    result = memory.generate_medium_memory_for_date("rin", "2026-08-07")

    assert result.status == "success"
    assert len(calls) == 3
    assert calls[2]["format_reminder"]
    saved = json.loads(
        (prompts_dir / "5_memory_medium.json").read_text(encoding="utf-8")
    )
    assert saved["2026-08-07"] == "当天的重要事件得到妥善处理，相关结论已经明确。"


def test_long_retries_truncation_and_format_then_writes_atomically(tmp_path, monkeypatch):
    prompts_dir = tmp_path / "prompts"
    _write_json(prompts_dir / "5_memory_medium.json", {
        "2026-07-27": "周一发生的重要事件。",
        "2026-07-28": "周二形成了明确结论。",
    })
    _write_json(prompts_dir / "4_memory_long.json", {
        "2026-07-Week4": "原有长期记忆",
    })
    monkeypatch.setattr(
        memory,
        "get_paths",
        lambda *args, **kwargs: (str(tmp_path / "chat.db"), str(prompts_dir)),
    )
    responses = iter([
        AIResponseText("半段", complete=False, finish_reason="length"),
        "【长期记忆】",
        "这一周的重要事件形成了持续影响，相关决定和后续方向已经明确。",
    ])
    monkeypatch.setattr(
        memory, "call_ai_to_summarize", lambda *_args, **_kwargs: next(responses)
    )

    result = memory.generate_long_memory_for_week(
        "rin", "2026-08-Week1"
    )

    assert result.status == "success"
    saved = json.loads(
        (prompts_dir / "4_memory_long.json").read_text(encoding="utf-8")
    )
    assert saved["2026-07-Week4"] == "原有长期记忆"
    assert saved["2026-08-Week1"] == result.content


def test_long_format_retry_exhaustion_preserves_old_memory(tmp_path, monkeypatch):
    prompts_dir = tmp_path / "prompts"
    _write_json(prompts_dir / "5_memory_medium.json", {
        "2026-07-27": "周一发生的重要事件。",
    })
    original = {"2026-08-Week1": "不得覆盖的旧长期记忆"}
    _write_json(prompts_dir / "4_memory_long.json", original)
    monkeypatch.setattr(
        memory,
        "get_paths",
        lambda *args, **kwargs: (str(tmp_path / "chat.db"), str(prompts_dir)),
    )
    calls = []

    def invalid_summary(*_args, **_kwargs):
        calls.append(1)
        return "【只有标题】"

    monkeypatch.setattr(memory, "call_ai_to_summarize", invalid_summary)
    result = memory.generate_long_memory_for_week("rin", "2026-08-Week1")

    assert result.status == "partial_failure"
    assert len(calls) == 3
    assert json.loads(
        (prompts_dir / "4_memory_long.json").read_text(encoding="utf-8")
    ) == original


def test_incremental_short_summary_reads_all_new_messages_once_and_leaves_no_tail(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "chat.db"
    prompts_dir = tmp_path / "prompts"
    short_file = prompts_dir / "6_memory_short.json"
    _make_chat_db(db_path, [
        (1, "user", "已经总结过的问题", "2026-08-07 09:00:00"),
        (2, "assistant", "已经总结过的回答", "2026-08-07 09:01:00"),
        (3, "user", "继续讨论新的完整事情", "2026-08-07 10:00:00"),
        (4, "assistant", "给出新事情的结果", "2026-08-07 10:03:00"),
        (5, "user", "总结触发前的最后一句", "2026-08-07 10:04:00"),
    ])
    _write_json(short_file, {
        "2026-08-07": {
            "events": [{"time": "09:00", "event": "旧的完整事件"}],
            "last_id": 2,
        }
    })
    _patch_character(monkeypatch, db_path, prompts_dir)
    calls = []

    def fake_summary(source, *_args, **_kwargs):
        calls.append(source)
        assert "已经总结过" not in source
        assert "继续讨论新的完整事情" in source
        assert "总结触发前的最后一句" in source
        return "- [10:00] 双方继续讨论新事项，形成结果并补充了最后一点。"

    monkeypatch.setattr(memory, "call_ai_to_summarize", fake_summary)
    result = memory.update_short_memory_for_date("rin", "2026-08-07")

    assert result.status == "success"
    assert len(calls) == 1
    saved = json.loads(short_file.read_text(encoding="utf-8"))["2026-08-07"]
    assert saved["last_id"] == 5
    assert [item["event"] for item in saved["events"]] == [
        "旧的完整事件",
        "双方继续讨论新事项，形成结果并补充了最后一点。",
    ]


def test_group_short_summary_only_reads_new_messages_and_consumes_latest_tail(
    tmp_path, monkeypatch
):
    group_dir = tmp_path / "groups" / "g1"
    db_path = group_dir / "chat.db"
    _make_chat_db(db_path, [
        (1, "user", "第一件群聊事情", "2026-08-07 09:00:00"),
        (2, "rin", "第一件事情的结果", "2026-08-07 09:02:00"),
    ])
    groups_file = tmp_path / "groups.json"
    chars_file = tmp_path / "characters.json"
    _write_json(groups_file, {"g1": {"name": "测试群", "members": ["rin"]}})
    _write_json(chars_file, {"rin": {"name": "糸師凛"}})

    monkeypatch.setattr(group_blueprint, "get_group_dir", lambda _group_id: str(group_dir))
    monkeypatch.setattr(group_blueprint, "_get_groups_config_file", lambda: str(groups_file))
    monkeypatch.setattr(group_blueprint, "_get_characters_config_file", lambda: str(chars_file))
    distributed = []
    monkeypatch.setattr(
        group_blueprint,
        "distribute_group_memory",
        lambda *args, **kwargs: distributed.append((args, kwargs)),
    )
    calls = []

    def fake_summary(source, *_args, **_kwargs):
        calls.append(source)
        if len(calls) == 1:
            return "- [09:00] 群成员讨论第一件事并得出结果。"
        if len(calls) == 2:
            assert "第一件群聊事情" not in source
            assert "尚未回复的最新消息" in source
            return "- [10:00] 用户提出了第二件群聊事项，等待后续处理。"
        assert "第一件群聊事情" in source
        assert "尚未回复的最新消息" in source
        return "- [09:00] 群成员先完成第一件事，随后用户提出第二件事项。"

    monkeypatch.setattr(memory, "call_ai_to_summarize", fake_summary)

    first_count, _ = group_blueprint.update_group_short_memory("g1", "2026-08-07")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO messages (id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (3, "user", "尚未回复的最新消息", "2026-08-07 10:00:00"),
        )
    second_count, _ = group_blueprint.update_group_short_memory("g1", "2026-08-07")

    assert (first_count, second_count) == (1, 1)
    assert len(calls) == 2
    saved = json.loads((group_dir / "memory_short.json").read_text(encoding="utf-8"))[
        "2026-08-07"
    ]
    assert saved["last_id"] == 3
    assert len(saved["events"]) == 2
    assert len(distributed) == 2

    rebuilt_count, _ = group_blueprint.update_group_short_memory(
        "g1", "2026-08-07", force_reset=True
    )
    rebuilt = json.loads(
        (group_dir / "memory_short.json").read_text(encoding="utf-8")
    )["2026-08-07"]
    assert rebuilt_count == 1
    assert rebuilt["last_id"] == 3
    assert len(rebuilt["events"]) == 1
    assert distributed[-1][1] == {
        "replace": True,
        "target_member_ids": None,
    }


def test_medium_format_failure_preserves_old_day(tmp_path, monkeypatch):
    prompts_dir = tmp_path / "prompts"
    short_file = prompts_dir / "6_memory_short.json"
    medium_file = prompts_dir / "5_memory_medium.json"
    events = [{"time": f"00:{index:02d}", "event": f"事件{index}"} for index in range(3)]
    _write_json(short_file, {"2026-08-07": {"events": events, "last_id": 3}})
    original = {"2026-08-07": "- 原来的完整中期记忆"}
    _write_json(medium_file, original)
    monkeypatch.setattr(memory, "get_paths", lambda *args, **kwargs: (str(tmp_path / "chat.db"), str(prompts_dir)))
    monkeypatch.setattr(
        memory,
        "call_ai_to_summarize",
        lambda *_a, **_k: "【只有标题】",
    )

    result = memory.generate_medium_memory_for_date("rin", "2026-08-07")

    assert result.status == "partial_failure"
    assert json.loads(medium_file.read_text(encoding="utf-8")) == original


def test_force_medium_rebuild_without_short_events_removes_stale_day(
    tmp_path, monkeypatch
):
    prompts_dir = tmp_path / "prompts"
    _write_json(prompts_dir / "6_memory_short.json", {"2026-08-07": {"events": []}})
    _write_json(prompts_dir / "5_memory_medium.json", {
        "2026-08-06": "保留前一天",
        "2026-08-07": "应删除的旧摘要",
    })
    monkeypatch.setattr(
        memory,
        "get_paths",
        lambda *args, **kwargs: (str(tmp_path / "chat.db"), str(prompts_dir)),
    )

    result = memory.generate_medium_memory_for_date(
        "rin", "2026-08-07", force_reset=True
    )

    assert result.status == "success"
    saved = json.loads(
        (prompts_dir / "5_memory_medium.json").read_text(encoding="utf-8")
    )
    assert saved == {"2026-08-06": "保留前一天"}


def test_concurrent_external_appends_do_not_lose_events(tmp_path):
    short_file = tmp_path / "6_memory_short.json"
    date_str = "2026-08-07"

    def worker(index):
        append_short_memory_events(
            str(short_file),
            date_str,
            [{"time": f"00:{index:02d}", "event": f"[朋友圈] 事件{index}"}],
        )

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    saved = json.loads(short_file.read_text(encoding="utf-8"))[date_str]["events"]
    assert len(saved) == 20


def test_group_rebuild_replaces_only_matching_group_prefix(tmp_path):
    short_file = tmp_path / "6_memory_short.json"
    _write_json(short_file, {
        "2026-08-07": {
            "last_id": 8,
            "events": [
                {"time": "08:00", "event": "私聊事件"},
                {"time": "09:00", "event": "[群聊:目标群] 旧碎片一"},
                {"time": "09:01", "event": "[群聊:目标群] 旧碎片二"},
                {"time": "10:00", "event": "[群聊:其他群] 保留事件"},
                {"time": "11:00", "event": "[朋友圈] 保留动态"},
            ],
        }
    })

    count = replace_short_memory_events_by_prefix(
        str(short_file),
        "2026-08-07",
        "[群聊:目标群]",
        [{"time": "09:00", "event": "[群聊:目标群] 新的完整事件"}],
    )

    saved = json.loads(short_file.read_text(encoding="utf-8"))["2026-08-07"]
    assert count == 1
    assert saved["last_id"] == 8
    assert [item["event"] for item in saved["events"]] == [
        "私聊事件",
        "[群聊:目标群] 新的完整事件",
        "[群聊:其他群] 保留事件",
        "[朋友圈] 保留动态",
    ]


def test_memory_page_uses_character_database_and_beijing_dates():
    root = Path(__file__).resolve().parents[1]
    chat_source = (root / "blueprints" / "chat.py").read_text(encoding="utf-8")
    template = (root / "templates" / "memory.html").read_text(encoding="utf-8")
    save_block = chat_source.split("def save_prompt_file", 1)[1].split("def search_messages", 1)[0]
    assert "sqlite3.connect(db_path)" in save_block
    assert "sqlite3.connect(DATABASE_FILE)" not in save_block
    assert "function beijingDateString" in template
    assert "toISOString().split('T')[0]" not in template


def test_rebuild_script_collects_and_summarizes_each_moment_action(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    script_path = root / "scripts" / "rebuild_memory_day.py"
    spec = importlib.util.spec_from_file_location("rebuild_memory_day_test", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    users_root = tmp_path / "users"
    config_dir = users_root / "1" / "configs"
    config_dir.mkdir(parents=True)
    _write_json(config_dir / "characters.json", {
        "rin": {"name": "糸師凛"},
        "sae": {"name": "糸師冴"},
    })
    _write_json(config_dir / "moments_data.json", [{
        "char_id": "rin",
        "timestamp": "2026-08-07 13:30:00",
        "content": "練習終了",
        "comments": [{
            "commenter_id": "rin",
            "reply_to": "user",
            "timestamp": "2026-08-07 15:30:00",
            "content": "うるせぇ",
        }],
    }, {
        "char_id": "sae",
        "timestamp": "2026-08-07 18:00:00",
        "content": "試合",
        "comments": [{
            "commenter_id": "rin",
            "timestamp": "2026-08-07 20:00:00",
            "content": "知るか",
        }],
    }])
    monkeypatch.setattr(module, "USERS_ROOT", str(users_root))
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    actions = module._moment_actions(1, "rin", "2026-08-07")
    assert [action["time"] for action in actions] == ["13:30", "15:30", "20:00"]
    monkeypatch.setattr(
        module,
        "call_ai_to_summarize",
        lambda context, *_args, **_kwargs: f"已总结：{context[:12]}",
    )
    events = module._summarize_moment_actions(actions, "rin", 1)
    assert len(events) == 3
    assert all(event["event"].startswith("[朋友圈]") for event in events)
