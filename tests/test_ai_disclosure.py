from pathlib import Path

from PIL import Image, ImageChops

from services.ai_watermark import WATERMARK_ASSET, apply_ai_watermark


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_copy_footer_is_shared_by_moments_and_chat_export():
    helper = _read("static/ai-disclosure.js")
    moments = _read("templates/moments.html")
    chat = _read("templates/chat.html")

    assert "由 Sakura樱语🌸 AI 生成" in helper
    assert "https://kunigami-project-api.online" in helper
    assert "appendCopyFooter(text)" in moments
    assert "appendCopyFooter(resultText)" in chat
    assert chat.count("copyResultBtn').addEventListener('click'") == 1


def test_visible_disclosure_is_present_on_required_surfaces():
    for relative_path in (
        "templates/chat.html",
        "templates/sakura_chat.html",
        "templates/forum.html",
        "templates/call.html",
    ):
        source = _read(relative_path)
        assert ">内容由 AI 生成</span>" in source

    chat = _read("templates/chat.html")
    assert chat.index('id="chatAiDisclosure"') < chat.index('id="mention-bar"')
    assert "ai-disclosure--capture" in chat
    assert "clonedDisclosure.outerHTML" in chat

    moments = _read("templates/moments.html")
    assert "m.char_id !== USER_ID" in moments
    assert "moment-ai-disclosure" in moments


def test_forum_copy_payload_is_not_modified_with_disclosure():
    forum = _read("templates/forum.html")
    start = forum.index("function copyForumBlock()")
    end = forum.index("function showToast", start)
    copy_function = forum[start:end]

    assert "KunigamiAiDisclosure" not in copy_function
    assert "kunigami-project-api.online" not in copy_function


def test_ai_watermark_is_baked_into_new_image(tmp_path):
    image_path = tmp_path / "gen_test.jpg"
    original = Image.new("RGB", (800, 600), "white")
    original.save(image_path, format="JPEG", quality=95)

    assert apply_ai_watermark(image_path) is True

    with Image.open(image_path) as result:
        result_rgb = result.convert("RGB")
    baseline = Image.new("RGB", result_rgb.size, "white")
    changed = ImageChops.difference(result_rgb, baseline)
    lower_region = changed.crop((0, result_rgb.height // 2, result_rgb.width, result_rgb.height))
    assert lower_region.getbbox() is not None
    assert sum(1 for pixel in lower_region.getdata() if pixel != (0, 0, 0)) > 1000


def test_ai_watermark_uses_bundled_transparent_image():
    assert WATERMARK_ASSET.is_file()
    assert WATERMARK_ASSET.name == "sakura_ai_watermark.png"
    with Image.open(WATERMARK_ASSET) as watermark:
        assert "transparency" in watermark.info or watermark.mode in ("RGBA", "LA")


def test_ai_watermark_rejects_unusable_tiny_image(tmp_path):
    image_path = tmp_path / "gen_tiny.png"
    Image.new("RGB", (16, 16), "white").save(image_path)

    assert apply_ai_watermark(image_path) is False
    with Image.open(image_path) as result:
        assert result.size == (16, 16)
