from pathlib import Path
import sqlite3

import pytest

import services.voice_calls as voice_calls


CALL_ID = "call_" + ("a" * 32)
SECOND_CALL_ID = "call_" + ("b" * 32)


def test_call_model_output_extracts_natural_tone_and_controls():
    parsed = voice_calls.parse_call_model_output(
        "[CALL_ACCEPT]\n"
        "[CALL_TONE](压低声音（像刚醒），带一点沙哑和不耐烦)\n"
        "喂……你最好是真的有重要的事。[END_CALL]"
    )

    assert parsed["controls"] == ["CALL_ACCEPT", "END_CALL"]
    assert parsed["tone"] == "压低声音（像刚醒），带一点沙哑和不耐烦"
    assert parsed["text"] == "喂……你最好是真的有重要的事。"
    assert voice_calls.tts_text_with_tone(parsed["text"], parsed["tone"]) == (
        "[压低声音（像刚醒），带一点沙哑和不耐烦]\n"
        "喂……你最好是真的有重要的事。"
    )


def test_call_model_output_supports_balanced_ascii_parentheses():
    parsed = voice_calls.parse_call_model_output(
        "[CALL_TONE](轻声说话（softly），然后笑一下 (not loudly))你好"
    )

    assert parsed["tone"] == "轻声说话（softly），然后笑一下 (not loudly)"
    assert parsed["text"] == "你好"
    assert voice_calls.normalize_call_spoken_text(
        "等等 / 我马上来//听得到吗"
    ) == "等等。我马上来。听得到吗"


def test_call_model_output_unwraps_accidental_legacy_voice_tag():
    normalized = voice_calls.normalize_legacy_voice_for_call(
        "[voice](你还在吗（喂）)(压低声音（有点担心）)"
    )
    parsed = voice_calls.parse_call_model_output(normalized)

    assert parsed["text"] == "你还在吗（喂）"
    assert parsed["tone"] == "压低声音（有点担心）"


def test_call_model_output_recovers_untagged_japanese_tone_prefix():
    normalized = voice_calls.normalize_untagged_tone_for_call(
        "溜め息混じりに、苛立ちと少しの安堵を隠せない低い声で "
        "…メッセージ送った瞬間に速攻でかけてきやがって。"
    )
    parsed = voice_calls.parse_call_model_output(normalized)

    assert parsed["tone"] == "溜め息混じりに、苛立ちと少しの安堵を隠せない低い声で"
    assert parsed["text"] == "…メッセージ送った瞬間に速攻でかけてきやがって。"


def test_call_model_output_recovers_untagged_tone_after_control_tag():
    normalized = voice_calls.normalize_untagged_tone_for_call(
        "[CALL_ACCEPT]（ため息混じりの低い声で）もしもし。"
    )
    parsed = voice_calls.parse_call_model_output(normalized)

    assert parsed["controls"] == ["CALL_ACCEPT"]
    assert parsed["tone"] == "ため息混じりの低い声で"
    assert parsed["text"] == "もしもし。"


def test_voice_call_tag_round_trip():
    tag = voice_calls.build_voice_call_tag(CALL_ID)
    assert tag == f"[voice_call]({CALL_ID})"
    assert voice_calls.parse_voice_call_tag(tag) == CALL_ID
    assert voice_calls.parse_voice_call_tag("[voice_call](../../bad)") is None


def test_consume_character_call_tag():
    cleaned, requested = voice_calls.consume_call_user_tag(
        "等我一下/[CALL_USER]/我想听听你的声音"
    )
    assert requested is True
    assert "CALL_USER" not in cleaned
    assert cleaned == "等我一下/我想听听你的声音"

    cleaned, requested = voice_calls.consume_call_user_tag("只是普通聊天")
    assert requested is False
    assert cleaned == "只是普通聊天"


def test_call_state_machine_and_turn_idempotency(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_calls, "USERS_ROOT", str(tmp_path))

    created = voice_calls.create_call(
        7,
        call_id=CALL_ID,
        char_id="rin",
        initiator="user",
        anchor_message_id=42,
    )
    assert created["status"] == "ringing"
    assert voice_calls.get_live_call(7)["call_id"] == CALL_ID

    with pytest.raises(voice_calls.VoiceCallConflict):
        voice_calls.create_call(
            7,
            call_id=SECOND_CALL_ID,
            char_id="isagi",
            initiator="assistant",
        )

    active = voice_calls.accept_call(7, CALL_ID)
    assert active["status"] == "active"
    first = voice_calls.append_turn(
        7,
        CALL_ID,
        role="user",
        content="喂，能听见吗？",
        client_turn_id="turn-client-1",
    )
    repeated = voice_calls.append_turn(
        7,
        CALL_ID,
        role="user",
        content="这条不会重复写入",
        client_turn_id="turn-client-1",
    )
    assistant = voice_calls.append_turn(
        7,
        CALL_ID,
        role="assistant",
        content="能听见。",
        tone="很自然地回应",
    )

    assert first["id"] == repeated["id"]
    assert assistant["sequence"] == 2
    assert len(voice_calls.list_turns(7, CALL_ID)) == 2

    ended = voice_calls.end_call(7, CALL_ID, reason="hangup")
    assert ended["status"] == "ended"
    assert ended["end_reason"] == "hangup"
    assert voice_calls.get_live_call(7) is None


def test_stale_active_call_is_disconnected(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_calls, "USERS_ROOT", str(tmp_path))
    voice_calls.create_call(
        9, call_id=CALL_ID, char_id="rin", initiator="user"
    )
    voice_calls.accept_call(9, CALL_ID)
    conn = sqlite3.connect(voice_calls._db_path(9))
    conn.execute(
        "UPDATE voice_calls SET heartbeat_at = ? WHERE call_id = ?",
        ("2000-01-01T00:00:00+00:00", CALL_ID),
    )
    conn.commit()
    conn.close()

    stale = voice_calls.expire_stale_active_calls(9)
    assert [item["call_id"] for item in stale] == [CALL_ID]
    ended = voice_calls.get_call(9, CALL_ID)
    assert ended["status"] == "ended"
    assert ended["end_reason"] == "disconnected"


def test_separate_users_can_have_independent_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_calls, "USERS_ROOT", str(tmp_path))

    voice_calls.create_call(
        1, call_id=CALL_ID, char_id="rin", initiator="assistant"
    )
    voice_calls.create_call(
        2, call_id=SECOND_CALL_ID, char_id="rin", initiator="assistant"
    )

    assert voice_calls.get_live_call(1)["call_id"] == CALL_ID
    assert voice_calls.get_live_call(2)["call_id"] == SECOND_CALL_ID


def test_latest_ended_call_can_be_scoped_to_character(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_calls, "USERS_ROOT", str(tmp_path))
    voice_calls.create_call(1, call_id=CALL_ID, char_id="rin", initiator="assistant")
    voice_calls.end_call(1, CALL_ID, reason="rejected")
    voice_calls.create_call(
        1, call_id=SECOND_CALL_ID, char_id="isagi", initiator="assistant"
    )
    voice_calls.end_call(1, SECOND_CALL_ID, reason="rejected")
    conn = sqlite3.connect(voice_calls._db_path(1))
    conn.execute(
        "UPDATE voice_calls SET ended_at = ? WHERE call_id = ?",
        ("2026-08-19T01:00:00+00:00", CALL_ID),
    )
    conn.execute(
        "UPDATE voice_calls SET ended_at = ? WHERE call_id = ?",
        ("2026-08-19T02:00:00+00:00", SECOND_CALL_ID),
    )
    conn.commit()
    conn.close()

    assert voice_calls.get_latest_ended_call(1)["call_id"] == SECOND_CALL_ID
    assert voice_calls.get_latest_ended_call(1, "rin")["call_id"] == CALL_ID
    assert voice_calls.get_latest_ended_call(1, "missing") is None


def test_delete_calls_for_character_cascades_turns(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_calls, "USERS_ROOT", str(tmp_path))
    voice_calls.create_call(3, call_id=CALL_ID, char_id="rin", initiator="user")
    voice_calls.accept_call(3, CALL_ID)
    voice_calls.append_turn(3, CALL_ID, role="user", content="你好")

    assert voice_calls.delete_calls_for_character(3, "rin") == 1
    assert voice_calls.get_call(3, CALL_ID) is None
    assert voice_calls.list_turns(3, CALL_ID) == []


def test_voice_call_anchor_expands_to_transcript_for_ai(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_calls, "USERS_ROOT", str(tmp_path))
    voice_calls.create_call(5, call_id=CALL_ID, char_id="rin", initiator="user")
    voice_calls.accept_call(5, CALL_ID)
    voice_calls.append_turn(5, CALL_ID, role="user", content="你听得到吗")
    voice_calls.append_turn(5, CALL_ID, role="assistant", content="听得到")
    voice_calls.end_call(5, CALL_ID, reason="hangup")

    expanded = voice_calls.voice_call_for_ai(5, voice_calls.build_voice_call_tag(CALL_ID))
    assert "语音通话记录｜已结束" in expanded
    assert "用户：你听得到吗" in expanded
    assert "角色：听得到" in expanded


def test_voice_call_anchor_expands_chronologically_for_selected_record(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(voice_calls, "USERS_ROOT", str(tmp_path))
    voice_calls.create_call(5, call_id=CALL_ID, char_id="rin", initiator="user")
    voice_calls.accept_call(5, CALL_ID)
    voice_calls.append_turn(5, CALL_ID, role="user", content="第一句")
    voice_calls.append_turn(5, CALL_ID, role="assistant", content="第二句")
    voice_calls.end_call(5, CALL_ID, reason="hangup")

    expanded = voice_calls.voice_call_for_record(
        5, voice_calls.build_voice_call_tag(CALL_ID)
    )

    assert "语音通话记录｜已结束" in expanded
    assert expanded.index("用户：第一句") < expanded.index("角色：第二句")
