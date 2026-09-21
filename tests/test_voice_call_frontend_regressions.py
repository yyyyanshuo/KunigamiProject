from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_exported_chat_image_keeps_voice_call_card():
    source = (ROOT / "templates" / "chat.html").read_text(encoding="utf-8")
    start = source.index("async function generateExportImageContent")
    end = source.index("// 监听窗口尺寸变化", start)
    export_image_block = source[start:end]

    assert "waitForSelectedVoiceCallCards(selectedEls)" in export_image_block
    assert "callBubble.textContent = selectedMessageResolvedContent" not in export_image_block
    assert "callBubble.classList.remove('bubble-call')" not in export_image_block


def test_incoming_call_buttons_render_model_reactions():
    source = (ROOT / "templates" / "call.html").read_text(encoding="utf-8")

    assert "beginActive(data.opening_turn||null)" in source
    assert "data.message||data.response_warning||''" in source
    assert "disableIncomingActions(true)" in source


def test_call_ai_disclosure_has_no_white_background():
    source = (ROOT / "static" / "ai-disclosure.css").read_text(encoding="utf-8")
    start = source.index(".ai-disclosure.ai-disclosure--call")
    block = source[start:source.index("}", start)]

    assert "background: none" in block
    assert "box-shadow: none" in block
