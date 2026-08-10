from pathlib import Path


TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "chat.html"


def _template_source():
    return TEMPLATE.read_text(encoding="utf-8")


def test_forwarded_chatlog_does_not_split_ruby_closing_tags():
    source = _template_source()
    splitter_start = source.index("function splitForwardedChatlogSegments(text)")
    splitter_end = source.index("function appendSafeRubyContent", splitter_start)
    splitter = source[splitter_start:splitter_end]

    assert "let inHtmlTag = false;" in splitter
    assert "char === '/' && !inHtmlTag && !insideUrl" in splitter
    assert "const urlRegex = /https?:\\/\\/[^\\s<]+/g;" in splitter
    assert "String(log.content).split('/')" not in source


def test_forwarded_chatlog_renders_only_safe_ruby_markup():
    source = _template_source()
    renderer_start = source.index("function appendSafeRubyContent(container, text)")
    renderer_end = source.index("// --- 【聊天记录】打开展开弹窗 ---", renderer_start)
    renderer = source[renderer_start:renderer_end]

    assert "const tokenRegex = /<\\/?(?:ruby|rt)>/g;" in renderer
    assert "document.createElement(tagName)" in renderer
    assert "document.createTextNode" in renderer
    assert "innerHTML" not in renderer
    assert "renderForwardedChatlogContent(contentEl, log.content);" in source


def test_forwarded_chatlog_preview_and_reforward_strip_ruby_markup():
    source = _template_source()

    assert "splitForwardedChatlogSegments(\n              stripRubyTags(log.content || '')" in source
    assert "(log.content || '').replace(/\\//g, ' ')" not in source
    assert "const content = stripRubyTags(selectedMessageResolvedContent(group, false));" in source


def test_selected_voice_calls_are_expanded_in_every_record_output():
    source = _template_source()

    assert "async function preloadSelectedCallRecords(groups)" in source
    assert "fetch('/api/calls/resolve-records'" in source
    assert source.count("await preloadSelectedCallRecords(selectedEls);") >= 4
    assert "callBubble.textContent = selectedMessageResolvedContent(group, false);" in source
