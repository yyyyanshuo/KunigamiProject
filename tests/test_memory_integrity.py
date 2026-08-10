import json
import importlib.util
import sqlite3
import threading
from pathlib import Path

from services import memory
from services import ai_client
from services.ai_client import AIResponseText
from services.memory_store import append_short_memory_events


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


def test_medium_memory_keeps_more_than_eight_items(tmp_path, monkeypatch):
    prompts_dir = tmp_path / "prompts"
    short_file = prompts_dir / "6_memory_short.json"
    events = [{"time": f"{index:02d}:00", "event": f"重要事件{index}"} for index in range(12)]
    _write_json(short_file, {"2026-08-07": {"events": events, "last_id": 12}})
    monkeypatch.setattr(memory, "get_paths", lambda *args, **kwargs: (str(tmp_path / "chat.db"), str(prompts_dir)))
    output = "\n".join(f"- 重要事件{index}" for index in range(12))
    monkeypatch.setattr(memory, "call_ai_to_summarize", lambda *_a, **_k: output)

    result = memory.generate_medium_memory_for_date("rin", "2026-08-07")

    assert result.status == "success"
    assert result.count == 12
    medium = json.loads((prompts_dir / "5_memory_medium.json").read_text(encoding="utf-8"))
    assert len(medium["2026-08-07"].splitlines()) == 12
    assert "重要事件11" in medium["2026-08-07"]


def test_medium_partial_failure_preserves_old_day(tmp_path, monkeypatch):
    prompts_dir = tmp_path / "prompts"
    short_file = prompts_dir / "6_memory_short.json"
    medium_file = prompts_dir / "5_memory_medium.json"
    events = [{"time": f"00:{index:02d}", "event": f"事件{index}"} for index in range(3)]
    _write_json(short_file, {"2026-08-07": {"events": events, "last_id": 3}})
    original = {"2026-08-07": "- 原来的完整中期记忆"}
    _write_json(medium_file, original)
    monkeypatch.setattr(memory, "get_paths", lambda *args, **kwargs: (str(tmp_path / "chat.db"), str(prompts_dir)))
    monkeypatch.setattr(memory, "MEDIUM_MEMORY_BATCH_EVENTS", 1)
    responses = iter(["- 事件0", None])
    monkeypatch.setattr(memory, "call_ai_to_summarize", lambda *_a, **_k: next(responses))

    result = memory.generate_medium_memory_for_date("rin", "2026-08-07")

    assert result.status == "partial_failure"
    assert json.loads(medium_file.read_text(encoding="utf-8")) == original


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
