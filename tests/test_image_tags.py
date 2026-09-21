import json
import io
import sqlite3
import subprocess

import pytest
from PIL import Image

from services.image_tags import (
    build_image_tag,
    iter_image_tags,
    normalize_image_description,
    parse_image_tag_at,
    protect_image_tags,
    split_message_bubbles,
    strip_image_tags,
)


COMPLEX_TAG = "[图片](https://cdn.example/a_(1)/b.jpg)(画面有 f(x/y)，以及 A/B（测试）)"


def test_balanced_image_tag_preserves_slashes_and_nested_parentheses():
    tag = parse_image_tag_at(COMPLEX_TAG)

    assert tag is not None
    assert tag.end == len(COMPLEX_TAG)
    assert tag.path == "https://cdn.example/a_(1)/b.jpg"
    assert tag.description == "画面有 f(x/y)，以及 A/B（测试）"
    assert tag.raw == COMPLEX_TAG


def test_bubble_split_only_uses_slashes_outside_image_tags():
    text = f"前一条 / {COMPLEX_TAG} / 后一条"

    assert split_message_bubbles(text) == ["前一条", COMPLEX_TAG, "后一条"]


def test_find_strip_and_protect_multiple_complex_tags():
    second = "[图片](202608/two.jpg)(括号(里面还有(一层)) / 完成)"
    text = f"开头 {COMPLEX_TAG}\n{second} 结尾"

    assert [tag.raw for tag in iter_image_tags(text)] == [COMPLEX_TAG, second]
    assert strip_image_tags(text) == "开头 \n 结尾"
    protected, raw_tags = protect_image_tags(text)
    assert protected == "开头 __IMG_0__\n__IMG_1__ 结尾"
    assert raw_tags == [COMPLEX_TAG, second]


def test_malformed_tag_is_left_as_plain_text():
    malformed = "[图片](a/b.jpg)(没有结束 / 后一条"
    assert parse_image_tag_at(malformed) is None
    assert strip_image_tags(malformed) == malformed


def test_description_normalization_preserves_pairs_only():
    assert normalize_image_description(" 函数 f(x) / 图表（A/B） ") == "函数 f(x) / 图表（A/B）"
    assert normalize_image_description("多余)以及(") == "多余）以及（"
    with pytest.raises(ValueError, match="不能为空"):
        normalize_image_description("  ")
    with pytest.raises(ValueError, match="不得超过500字"):
        normalize_image_description("字" * 501)
    assert build_image_tag("a/b.jpg", "说明(保留)但这里(") == "[图片](a/b.jpg)(说明(保留)但这里（)"


def test_browser_parser_matches_python_parser(project_root):
    script_path = project_root + "/static/image-tags.js"
    node_script = f"""
global.window = global;
require({json.dumps(script_path)});
const text = {json.dumps('开头 / ' + COMPLEX_TAG + ' / 结尾')};
process.stdout.write(JSON.stringify({{
  parts: KunigamiImageTags.splitBubbles(text),
  tags: KunigamiImageTags.findAll(text),
  stripped: KunigamiImageTags.strip(text)
}}));
"""
    try:
        result = subprocess.run(
            ["node", "-e", node_script],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except FileNotFoundError:
        pytest.skip("Node.js is not installed")

    parsed = json.loads(result.stdout)
    assert parsed["parts"] == ["开头", COMPLEX_TAG, "结尾"]
    assert parsed["tags"][0]["path"] == "https://cdn.example/a_(1)/b.jpg"
    assert parsed["tags"][0]["description"] == "画面有 f(x/y)，以及 A/B（测试）"
    assert parsed["stripped"] == "开头 /  / 结尾"


def test_upload_interfaces_offer_ai_and_manual_description(project_root):
    chat_html = open(project_root + "/templates/chat.html", encoding="utf-8").read()
    moments_html = open(project_root + "/templates/moments.html", encoding="utf-8").read()

    assert "description_mode" in chat_html
    assert "手动描述" in chat_html
    assert "image_metadata" in moments_html
    assert "preview-description" in moments_html


def _jpeg_upload():
    buffer = io.BytesIO()
    Image.new("RGB", (12, 12), (240, 180, 120)).save(buffer, format="JPEG")
    buffer.seek(0)
    return buffer


@pytest.fixture
def image_test_user(tmp_path, monkeypatch):
    import core.config

    database = tmp_path / 'users.db'
    with sqlite3.connect(database) as conn:
        conn.execute('CREATE TABLE users (id INTEGER PRIMARY KEY, auth_version INTEGER)')
        conn.execute('INSERT INTO users VALUES (1, 1)')
    monkeypatch.setattr(core.config, 'USERS_DB', str(database))


def _authenticate(client):
    from core.session_security import get_auth_version

    with client.session_transaction() as current_session:
        current_session["user_id"] = 1
        current_session["auth_version"] = get_auth_version(1)


def test_chat_manual_description_skips_vision_model(app_client, monkeypatch, tmp_path, image_test_user):
    import blueprints.media as media_module

    _authenticate(app_client)
    monkeypatch.setattr(media_module, "get_current_user_id", lambda: "manual-user")
    monkeypatch.setattr(media_module.core.config, "USERS_ROOT", str(tmp_path / "users"))
    monkeypatch.setattr(media_module.core.config, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(media_module, "upload_to_cos", lambda *_args, **_kwargs: "ok")
    monkeypatch.setattr(
        media_module,
        "_generate_image_description",
        lambda *_args, **_kwargs: pytest.fail("manual mode must not call vision"),
    )

    response = app_client.post(
        "/api/vision/upload",
        data={
            "file": (_jpeg_upload(), "sample.jpg"),
            "description_mode": "manual",
            "description": "菜单上的 f(x/y) / 今日限定",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["description_mode"] == "manual"
    assert payload["description"] == "菜单上的 f(x/y) / 今日限定"


def test_chat_ai_failure_requests_manual_fallback(app_client, monkeypatch, tmp_path, image_test_user):
    import blueprints.media as media_module

    _authenticate(app_client)
    monkeypatch.setattr(media_module, "get_current_user_id", lambda: "ai-user")
    monkeypatch.setattr(media_module.core.config, "USERS_ROOT", str(tmp_path / "users"))
    monkeypatch.setattr(media_module.core.config, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(
        media_module,
        "_generate_image_description",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("vision unavailable")),
    )

    response = app_client.post(
        "/api/vision/upload",
        data={
            "file": (_jpeg_upload(), "sample.jpg"),
            "description_mode": "ai",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 502
    assert response.get_json()["code"] == "vision_failed"


def test_moments_rejects_invalid_image_metadata_before_publish(app_client, monkeypatch, image_test_user):
    import blueprints.moments as moments_module

    _authenticate(app_client)
    monkeypatch.setattr(moments_module, "get_current_user_id", lambda: "user")
    response = app_client.post(
        "/api/moments/post",
        data={"content": "有正文", "image_metadata": "not-json"},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert "图片描述信息" in response.get_json()["error"]
