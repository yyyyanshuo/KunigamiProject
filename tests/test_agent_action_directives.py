from unittest.mock import patch

from agent_utils import (
    _normalize_character_lookup_key,
    _resolve_char_id,
    process_agent_actions,
)


def _resolve(name, user_id=None):
    return {
        "角色甲": "char_a",
        "角色乙": "char_b",
    }.get(name)


def test_group_directive_accepts_fullwidth_brackets_and_separators():
    raw = "继续聊吧\n【DIRECT_TO_GROUP：角色甲，角色乙，+user】"
    with patch("agent_utils._resolve_char_id", side_effect=_resolve):
        cleaned, _, directive = process_agent_actions("speaker", raw, "user-1")

    assert cleaned == "继续聊吧"
    assert directive == {
        "type": "group",
        "member_ids": ["char_a", "char_b"],
        "include_user": True,
        "custom_name": None,
    }


def test_group_directive_accepts_fullwidth_custom_name_separator():
    raw = "出发\n[DIRECT_TO_GROUP: 小队｜角色甲、角色乙]"
    with patch("agent_utils._resolve_char_id", side_effect=_resolve):
        cleaned, _, directive = process_agent_actions("speaker", raw, "user-1")

    assert cleaned == "出发"
    assert directive["member_ids"] == ["char_a", "char_b"]
    assert directive["custom_name"] == "小队"
    assert directive["include_user"] is False


def test_user_directive_accepts_fullwidth_brackets():
    cleaned, _, directive = process_agent_actions(
        "speaker", "单独说吧\n【DIRECT_TO_USER】", "user-1"
    )

    assert cleaned == "单独说吧"
    assert directive == {"type": "user"}


def test_safety_alert_returns_frontend_event_and_cleans_text():
    cleaned, _, directive, events = process_agent_actions(
        "speaker",
        "先陪你待一会儿\n[SAFETY_ALERT: 请立刻联系身边可信任的人或当地紧急服务]",
        return_events=True,
    )

    assert cleaned == "先陪你待一会儿"
    assert directive is None
    assert events == [
        {
            "type": "safety_alert",
            "message": "请立刻联系身边可信任的人或当地紧急服务",
        }
    ]


def test_bare_safety_alert_returns_default_frontend_event():
    cleaned, _, directive, events = process_agent_actions(
        "speaker",
        "我在这里\n【SAFETY_ALERT】",
        return_events=True,
    )

    assert cleaned == "我在这里"
    assert directive is None
    assert events == [{"type": "safety_alert", "message": ""}]


def test_web_click_ref_directive_is_preserved_for_browser_agent():
    raw = "这就点下去！\n[CLICK_REF:btn-2]"

    cleaned, affinity_delta, directive = process_agent_actions(
        "speaker",
        raw,
        "user-1",
    )

    assert cleaned == raw
    assert affinity_delta is None
    assert directive is None


def test_voice_directive_is_preserved_for_frontend_renderer():
    raw = "先听我说\n[voice](最近还好吗)(温柔中带着一点担心)"

    cleaned, affinity_delta, directive = process_agent_actions(
        "speaker",
        raw,
        "user-1",
    )

    assert cleaned == raw
    assert affinity_delta is None
    assert directive is None


def test_character_lookup_normalizes_middle_dot_variants():
    assert _normalize_character_lookup_key("アレクシス・ネス") == (
        _normalize_character_lookup_key("アレクシス·ネス")
    )


def test_group_directive_accepts_display_name_punctuation_variant():
    raw = "一起聊吧\n[DIRECT_TO_GROUP: アレクシス・ネス]"

    def resolve_with_config_semantics(name, user_id=None):
        configured_name = "アレクシス·ネス"
        if _normalize_character_lookup_key(name) == _normalize_character_lookup_key(configured_name):
            return "ness"
        return None

    with patch("agent_utils._resolve_char_id", side_effect=resolve_with_config_semantics):
        cleaned, _, directive = process_agent_actions("speaker", raw, "user-1")

    assert cleaned == "一起聊吧"
    assert directive == {
        "type": "group",
        "member_ids": ["ness"],
        "include_user": False,
        "custom_name": None,
    }


def test_resolve_char_id_matches_normalized_configured_name(tmp_path):
    config_path = tmp_path / "characters.json"
    config_path.write_text(
        '{"ness": {"name": "アレクシス·ネス", "remark": "ネス"}}',
        encoding="utf-8",
    )

    with patch("app._get_characters_config_file", return_value=str(config_path)):
        assert _resolve_char_id("アレクシス・ネス", user_id="user-1") == "ness"
