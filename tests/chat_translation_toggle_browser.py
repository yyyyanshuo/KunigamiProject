"""Exercise the actual chat translation functions in Chromium without a server or model."""
from pathlib import Path

from playwright.sync_api import sync_playwright


template = (Path(__file__).resolve().parents[1] / "templates/chat.html").read_text()
functions = template[
    template.index("  const CHAT_TRANS_CACHE_PREFIX ="):
    template.index('  // --- 【新增】解决“返回不刷新”的问题 ---')
]
html = """<html lang="zh-CN"><body><div id="chatContainer">
<div class="message-group user" data-id="42" data-original-content="こんにちは">
<div class="message-wrapper"><button>翻译</button></div>
<div class="message-wrapper"><button>翻译</button></div>
</div></div></body></html>"""

with sync_playwright() as playwright:
    browser = playwright.chromium.launch()
    try:
        for group_mode in (False, True):
            context = browser.new_context(service_workers="block")
            requests = []

            def route_request(route):
                if route.request.url.endswith("/api/translate"):
                    requests.append(route.request.post_data_json)
                    route.fulfill(json={"result": "重新翻译的正文"})
                else:
                    route.fulfill(content_type="text/html", body=html)

            context.route("**/*", route_request)
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def load():
                page.goto("http://127.0.0.1/translation-test")
                page.add_script_tag(content="""
                    window.SakiPrivacy = {translationCachePrefix: 'saki:translations:1:'};
                    const chatContainer = document.getElementById('chatContainer');
                    const pendingSentTranslations = new Map();
                    const currentId = 'demo', aiLanguage = 'ja';
                    const currentConfig = {}, groupMembers = {};
                    const SakiI18n = {t: text => text};
                    function needsReadingTranslation() { return true; }
                    function stripRubyTags(text) { return text; }
                """ + f"const isGroupMode = {str(group_mode).lower()};\n" + functions + """
                    document.querySelectorAll('button').forEach(button => {
                        button.onclick = () => translateMessage(button.parentNode, 42,
                            button.closest('.message-group'));
                    });
                    showCachedTranslations();
                """)

            load()
            page.evaluate("""() => {
                const group = document.querySelector('.message-group');
                localStorage.setItem(_getChatSourceCacheKey(42), '原句');
                localStorage.setItem(_getChatTransCacheKey(42, group), '旧缓存译文');
                pendingSentTranslations.set('42', '原句');
                _applyPendingTranslation();
                showCachedTranslations();
            }""")
            assert page.locator('.translation-source, .translation-result').count() == 1
            assert page.locator('.translation-source').inner_text() == '原句'
            # Click from the other bubble: hide the existing source without a request.
            page.locator('button').first.click()
            assert page.locator('.translation-source').is_hidden()
            assert not requests
            page.evaluate("""() => {
                pendingSentTranslations.set('42', '原句');
                _applyPendingTranslation();
                showCachedTranslations();
            }""")
            assert page.locator('.translation-source, .translation-result').count() == 0
            load()
            assert page.locator('.translation-source, .translation-result').count() == 0
            # The second click must translate, ignoring both old cached text and source.
            page.locator('button').first.click()
            page.wait_for_function("document.querySelector('.translation-result')?.textContent === '重新翻译的正文'")
            assert len(requests) == 1
            assert requests[0]['message_id'] == 42
            assert requests[0]['group_id' if group_mode else 'char_id'] == 'demo'
            assert page.locator('.translation-source').count() == 0
            assert page.locator('.translation-result').count() == 1
            load()
            assert page.locator('.translation-source').count() == 0
            assert page.locator('.translation-result').inner_text() == '重新翻译的正文'
            page.locator('button').last.click()
            assert page.locator('.translation-result').is_hidden()
            assert len(requests) == 1
            page.locator('button').first.click()
            page.wait_for_function("document.querySelector('.translation-result')?.textContent === '重新翻译的正文'")
            assert len(requests) == 2
            assert page.locator('.translation-result').count() == 1
            assert not errors, errors
            context.close()
        print('PASS: single/group source toggle, multi-bubble controls, refresh persistence, fresh translation and cache replacement')
    finally:
        browser.close()
