from core.message_tags import canonicalize_message_tags
import sqlite3

import pytest

from agent_utils import process_agent_actions
from blueprints.chat import (
    TransferActionError,
    apply_assistant_transfer_decision,
    apply_transfer_action,
    normalize_transfer_amount,
    parse_transfer_tag,
)


def _messages_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE messages ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "role TEXT NOT NULL, content TEXT NOT NULL, timestamp TEXT NOT NULL)"
    )
    return conn


@pytest.mark.parametrize("role", ["assistant", "group_character"])
@pytest.mark.parametrize("action,status", [("accept", "ACCEPTED"), ("return", "RETURNED")])
def test_euro_transfer_survives_cleanup_storage_and_resolution(role, action, status):
    raw = "[TRANSFER:100€|Pour tes cafés.]"
    cleaned, _, _ = process_agent_actions("speaker", raw + "\n[NONE]")
    assert cleaned == raw
    with _messages_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
            (role, cleaned, "2026-10-29 12:00:00"),
        )
        source_id = cursor.lastrowid
        message, event = apply_transfer_action(
            cursor, {"transfer_action": action, "transfer_source_id": source_id},
            f"[TRANSFER_{action.upper()}:100€]",
            allow_group_character_source=(role != "assistant"),
        )
        assert message == f"[TRANSFER_{action.upper()}:100€]"
        assert event["amount"] == "100€"
        assert event["note"] == "Pour tes cafés."
        assert conn.execute("SELECT content FROM messages WHERE id = ?", (source_id,)).fetchone()[0] == (
            f"[TRANSFER_{status}:100€|Pour tes cafés.]"
        )


@pytest.mark.parametrize(
    "amount",
    ["88.00元", "1000円", "$12.50", "€10", "£8.5", "₩12000",
     "88.00", "¥88", "￥88", "88USD", "USD88", "88金币", "88瑞士法郎", "₿1.25"],
)
def test_transfer_amount_accepts_optional_currency_notation(amount):
    assert normalize_transfer_amount(amount) == amount


@pytest.mark.parametrize("decision,status", [("ACCEPT", "ACCEPTED"), ("RETURN", "RETURNED")])
def test_assistant_english_transfer_decision_survives_agent_cleanup(decision, status):
    with _messages_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
            ("user", "[TRANSFER:100€|Pour tes cafés.]", "2026-10-29 12:00:00"),
        )
        cleaned, _, _ = process_agent_actions("speaker", f"Merci / [TRANSFER_{decision}:100€]")
        message, event = apply_assistant_transfer_decision(cursor, cleaned)
        assert message == f"Merci / [TRANSFER_{decision}:100€]"
        assert event["source_id"] == 1
        assert event["content"] == f"[TRANSFER_{status}:100€|Pour tes cafés.]"


@pytest.mark.parametrize(
    "amount",
    ["", "元", "USD", "0", "0元", "-1元", "01元", "1.234元", "1000000000元",
     "1USD2", "1..2", "88|USD", "88[USD]", "88/USD", "<b>88</b>"],
)
def test_transfer_amount_rejects_invalid_number_or_tag_delimiters(amount):
    with pytest.raises(TransferActionError):
        normalize_transfer_amount(amount)


@pytest.mark.parametrize("amount", ["88.00", "¥88", "88 USD", "88金币", "₿1.25"])
@pytest.mark.parametrize("action,decision,resolved", [
    ("accept", "领取转账", "已领取转账"),
    ("return", "退回转账", "已退回转账"),
])
def test_transfer_with_optional_currency_can_be_resolved(amount, action, decision, resolved):
    conn = _messages_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
        ("assistant", f"[转账:{amount}|备注]", "2026-09-05 12:00:00"),
    )
    source_id = cursor.lastrowid
    normalized = amount.replace(" ", "")
    message, event = apply_transfer_action(
        cursor, {"transfer_action": action, "transfer_source_id": source_id},
        f"[{decision}:{normalized}]",
    )
    assert message == canonicalize_message_tags(f"[{decision}:{normalized}]")
    assert event["content"] == canonicalize_message_tags(f"[{resolved}:{normalized}|备注]")
    assert conn.execute("SELECT content FROM messages WHERE id = ?", (source_id,)).fetchone()[0] == event["content"]


def test_parse_transfer_tag_preserves_amount_note_and_replacement_range():
    content = "给你 / [转账:88.00元|请你喝奶茶] / 收好"
    transfer = parse_transfer_tag(content)

    assert transfer["kind"] == "转账"
    assert transfer["amount"] == "88.00元"
    assert transfer["note"] == "请你喝奶茶"
    assert content[transfer["start"]:transfer["end"]] == "[转账:88.00元|请你喝奶茶]"


def test_accept_transfer_updates_source_and_returns_clean_user_tag():
    conn = _messages_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
        ("assistant", "拿着 / [转账:88.00元|请你喝奶茶]", "2026-08-08 12:00:00"),
    )
    source_id = cursor.lastrowid

    user_message, event = apply_transfer_action(
        cursor,
        {"transfer_action": "accept", "transfer_source_id": source_id},
        "[领取转账:88.00元]",
    )
    conn.commit()

    updated = conn.execute(
        "SELECT content FROM messages WHERE id = ?", (source_id,)
    ).fetchone()[0]
    assert updated == canonicalize_message_tags("拿着 / [已领取转账:88.00元|请你喝奶茶]")
    assert user_message == canonicalize_message_tags("[领取转账:88.00元]")
    assert event == {
        "source_id": source_id,
        "status": "accepted",
        "content": updated,
        "amount": "88.00元",
        "note": "请你喝奶茶",
    }


def test_group_user_can_accept_transfer_from_character_role():
    conn = _messages_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
        ("kunigami", "[转账:66元|群红包]", "2026-08-26 12:00:00"),
    )
    source_id = cursor.lastrowid

    user_message, event = apply_transfer_action(
        cursor,
        {"transfer_action": "accept", "transfer_source_id": source_id},
        "[领取转账:66元]",
        allow_group_character_source=True,
    )
    conn.commit()

    assert user_message == canonicalize_message_tags("[领取转账:66元]")
    assert event["status"] == "accepted"
    assert conn.execute(
        "SELECT content FROM messages WHERE id = ?", (source_id,)
    ).fetchone()[0] == "[TRANSFER_ACCEPTED:66元|群红包]"


def test_single_chat_rejects_group_character_role_as_transfer_source():
    conn = _messages_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
        ("kunigami", "[转账:66元]", "2026-08-26 12:00:00"),
    )

    with pytest.raises(TransferActionError) as exc_info:
        apply_transfer_action(
            cursor,
            {"transfer_action": "accept", "transfer_source_id": cursor.lastrowid},
            "[领取转账:66元]",
        )
    assert exc_info.value.code == "transfer_not_found"


def test_return_transfer_updates_source_and_rejects_second_resolution():
    conn = _messages_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
        ("assistant", "[转账:$12.50|Lunch]", "2026-08-08 12:00:00"),
    )
    source_id = cursor.lastrowid
    payload = {"transfer_action": "return", "transfer_source_id": source_id}

    user_message, event = apply_transfer_action(
        cursor, payload, "[退回转账:$12.50]"
    )
    conn.commit()

    assert user_message == canonicalize_message_tags("[退回转账:$12.50]")
    assert event["status"] == "returned"
    assert conn.execute(
        "SELECT content FROM messages WHERE id = ?", (source_id,)
    ).fetchone()[0] == "[TRANSFER_RETURNED:$12.50|Lunch]"

    with pytest.raises(TransferActionError) as exc_info:
        apply_transfer_action(cursor, payload, "[退回转账:$12.50]")
    assert exc_info.value.code == "transfer_already_resolved"
    assert exc_info.value.status_code == 409


def test_transfer_decision_requires_hidden_source_metadata():
    conn = _messages_db()
    with pytest.raises(TransferActionError) as exc_info:
        apply_transfer_action(conn.cursor(), {}, "[领取转账:88.00元]")
    assert exc_info.value.code == "invalid_transfer_action"


def test_transfer_action_rejects_tampered_amount():
    conn = _messages_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
        ("assistant", "[转账:88.00元]", "2026-08-08 12:00:00"),
    )
    source_id = cursor.lastrowid

    with pytest.raises(TransferActionError) as exc_info:
        apply_transfer_action(
            cursor,
            {"transfer_action": "accept", "transfer_source_id": source_id},
            "[领取转账:99.00元]",
        )
    assert exc_info.value.code == "transfer_amount_mismatch"


def test_assistant_decision_uses_latest_pending_transfer_with_same_amount():
    conn = _messages_db()
    cursor = conn.cursor()
    rows = [
        ("user", "[转账:88.00元|较早的同额转账]", "2026-08-08 12:00:00"),
        ("user", "[转账:1000円|更新但不同金额]", "2026-08-08 12:01:00"),
        ("user", "[转账:88.00元|最新的同额转账]", "2026-08-08 12:02:00"),
    ]
    cursor.executemany(
        "INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)", rows
    )

    assistant_message, event = apply_assistant_transfer_decision(
        cursor, "那我收下了 / [领取转账:88.00元]"
    )
    conn.commit()

    assert assistant_message == canonicalize_message_tags("那我收下了 / [领取转账:88.00元]")
    assert event["source_id"] == 3
    assert event["status"] == "accepted"
    contents = {
        row[0]: row[1]
        for row in conn.execute("SELECT id, content FROM messages ORDER BY id")
    }
    assert contents[1] == "[转账:88.00元|较早的同额转账]"
    assert contents[2] == "[转账:1000円|更新但不同金额]"
    assert contents[3] == canonicalize_message_tags("[已领取转账:88.00元|最新的同额转账]")

    _, second_event = apply_assistant_transfer_decision(
        cursor, "[退回转账:88.00元]"
    )
    assert second_event["source_id"] == 1
    assert second_event["status"] == "returned"


def test_assistant_decision_does_not_touch_newer_different_amount():
    conn = _messages_db()
    cursor = conn.cursor()
    cursor.executemany(
        "INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
        [
            ("user", "[转账:88.00元]", "2026-08-08 12:00:00"),
            ("user", "[转账:99.00元]", "2026-08-08 12:01:00"),
        ],
    )

    _, event = apply_assistant_transfer_decision(
        cursor, "[领取转账:88.00元]"
    )

    assert event["source_id"] == 1
    assert conn.execute(
        "SELECT content FROM messages WHERE id = 2"
    ).fetchone()[0] == "[转账:99.00元]"


def test_assistant_decision_without_matching_amount_leaves_stack_unchanged():
    conn = _messages_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
        ("user", "[转账:88.00元]", "2026-08-08 12:00:00"),
    )

    assistant_message, event = apply_assistant_transfer_decision(
        cursor, "我好像看错了 / [领取转账:99.00元]"
    )

    assert assistant_message == "我好像看错了 / [领取转账:99.00元]"
    assert event is None
    assert conn.execute(
        "SELECT content FROM messages WHERE id = 1"
    ).fetchone()[0] == "[转账:88.00元]"


def test_transfer_frontend_uses_cards_hidden_source_id_and_inline_edit_refresh():
    from pathlib import Path
    template = (Path(__file__).resolve().parents[1] / "templates/chat.html").read_text(encoding="utf-8")

    assert "transfer-card" in template
    assert "transfer_source_id: pending.sourceId" in template
    assert "[${decision}:${pending.amount}]" in template
    assert "replaceRenderedMessageGroup" in template
    assert "if (isGroupMode || !sourceId" not in template
    assert "isAssistant && !isGroupMode" not in template
    assert "`/api/group/${encodeURIComponent(currentId)}/chat`" in template

    edit_handler = template[
        template.index("document.getElementById('saveEditBtn').onclick"):
        template.index("async function deleteMessage")
    ]
    assert "replaceRenderedMessageGroup" in edit_handler
    assert "location.reload()" not in edit_handler


@pytest.mark.parametrize('action,kind', [('accept', 'TRANSFER_ACCEPT'), ('return', 'TRANSFER_RETURN')])
@pytest.mark.parametrize('group', [False, True])
def test_transfer_resolution_preserves_caption_and_resolves_exact_source(action, kind, group):
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY, role TEXT, content TEXT)')
    cur = conn.execute('INSERT INTO messages(role,content) VALUES (?,?)',
                       ('character' if group else 'assistant', '[TRANSFER:12.50USD|Lunch]'))
    source_id = cur.lastrowid
    text = f'Thanks / 多行\n留言 / [{kind}:12.50USD]'
    message, event = apply_transfer_action(conn.cursor(),
        {'transfer_action':action, 'transfer_source_id':source_id}, text,
        allow_group_character_source=group)
    assert message == canonicalize_message_tags(text)
    assert event['source_id'] == source_id
    assert event['status'] == ('accepted' if action == 'accept' else 'returned')
    with pytest.raises(TransferActionError):
        apply_transfer_action(conn.cursor(), {'transfer_action':action, 'transfer_source_id':source_id}, text,
                              allow_group_character_source=group)


@pytest.mark.parametrize('text', ['Thanks / [TRANSFER_ACCEPT:1USD]', '[TRANSFER_RETURN:1USD] / Bye'])
def test_transfer_caption_cannot_bypass_source_metadata(text):
    with pytest.raises(TransferActionError):
        apply_transfer_action(sqlite3.connect(':memory:').cursor(), {}, text)


def test_multiple_transfer_decisions_rejected_before_mutation():
    conn = sqlite3.connect(':memory:')
    with pytest.raises(TransferActionError):
        apply_transfer_action(conn.cursor(), {'transfer_action':'accept', 'transfer_source_id':1},
                              '[TRANSFER_ACCEPT:1USD] / [TRANSFER_ACCEPT:1USD]')
