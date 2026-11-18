"""Run against an isolated local Flask server; never calls external services.

SAKI_GUIDE_URL defaults to http://127.0.0.1:5099.
Requires Playwright and Chromium; screenshots go to SAKI_GUIDE_SCREENSHOTS if set.
"""
import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


base = os.environ.get("SAKI_GUIDE_URL", "http://127.0.0.1:5099")
assert urlparse(base).hostname == "127.0.0.1"
screenshots = os.environ.get("SAKI_GUIDE_SCREENSHOTS")
if screenshots:
    Path(screenshots).mkdir(parents=True, exist_ok=True)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(
        args=["--disable-background-networking", "--disable-sync", "--no-first-run"]
    )
    context = browser.new_context(locale="ja-JP", service_workers="block")
    context.route("**/*", lambda route: route.continue_()
                  if urlparse(route.request.url).hostname == "127.0.0.1" else route.abort())
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(base + "/guide")
    for language, title, brand, query in [
        ("ja", "使い方ガイド", "咲き", "記憶"),
        ("en", "User guide", "Saki", "memory"),
        ("zh-CN", "使用文档", "花期", "记忆"),
    ]:
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.select_option("#guide-language", language)
        page.wait_for_function("(language) => document.documentElement.lang === language", arg=language)
        assert page.title() == title + " | " + brand
        assert page.locator(".doc-section").count() == 21
        assert page.locator(".quick-nav a").count() == 21
        assert page.evaluate("""() => [...document.querySelectorAll('a[href^="#"]')]
            .every(a => document.getElementById(a.hash.slice(1)))""")
        assert "Kunigami" not in page.locator("body").inner_text()
        assert "Sakura" not in page.locator("body").inner_text()
        if language == "en":
            assert "角色" not in page.locator("main").inner_text()
        for width in (1280, 390, 320):
            page.set_viewport_size({"width": width, "height": 850})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (language, width)
            if screenshots and width in (1280, 390):
                page.screenshot(path=str(Path(screenshots) / (language + "-" + str(width) + ".png")))
        page.locator("#guide-search-input").fill(query)
        page.wait_for_selector(".search-result")
        assert page.locator(".search-result").count() > 0
        page.locator("#guide-search-input").press("Enter")
        assert page.locator(".search-hit").count() == 1
        assert page.locator("#guide-search-results").is_hidden()
        page.locator("#guide-search-input").fill("zzzz-no-such-topic")
        assert page.locator(".search-empty").is_visible()
        page.locator("#guide-search-clear").click()
        assert page.locator("#guide-search-results").is_hidden()
        page.locator("#guide-top-nav summary").click()
        page.locator('#guide-top-nav a[href="#models"]').click()
        assert page.url.endswith("#models")
        assert not page.locator("#guide-top-nav").evaluate("(node) => node.open")
        page.locator("#guide-back-to-top").click()
        page.wait_for_function("window.scrollY < 5")
        for path in ("/login", "/forgot_password"):
            page.goto(base + path)
            assert page.locator(".auth-guide-link").inner_text().strip() == title
            page.locator(".auth-guide-link").focus()
            with context.expect_page() as opened:
                page.locator(".auth-guide-link").press("Enter")
            guide = opened.value
            guide.wait_for_load_state()
            assert guide.title() == title + " | " + brand
            guide.close()
            # Keyboard activation of the guide must not submit a login form.
            assert page.locator("#error-msg").is_hidden()
            for width in (390, 320):
                page.set_viewport_size({"width": width, "height": 640})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (path, language, width)
                assert page.locator(".auth-guide-link").bounding_box()["y"] >= 0
            if screenshots and path == "/forgot_password":
                page.screenshot(path=str(Path(screenshots) / (language + "-reset.png")), full_page=True)
        page.goto(base + "/login")
        page.locator('a[href="/forgot_password"]').click()
        assert page.url.endswith("/forgot_password")
        assert page.locator("#verify_code").get_attribute("autocomplete") == "one-time-code"
        page.goto(base + "/guide")
    assert errors == [], errors
    context.close()
    browser.close()
print("PASS: 3 locales; mobile/desktop layout; search, contents, keyboard links and password recovery entry")
