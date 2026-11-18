"""Moments short memory preserves published content without summary requests."""

import json
from unittest.mock import Mock

import pytest


POST_TIME = "2026-10-14 23:59:00"
REPLY_TIME = "2026-10-15 00:01:00"
POST_CONTENT = "完整朋友圈原文" * 70 + "\n[IMAGE](photo.jpg)(照片说明)\n最后一行"
PARENT_CONTENT = "上游评论原文" * 60 + "\n上游末行"
REPLY_CONTENT = "回复第一行\n回复第二行"


@pytest.fixture
def memory_case(accounts_client, tmp_path, monkeypatch):
    import app
    from blueprints import moments
    from services import memory

    users_root = tmp_path / "users"
    monkeypatch.setattr(moments, "USERS_ROOT", str(users_root))
    characters = {"author": {"name": "发布者"}, "speaker": {"name": "回复者"}}
    config = users_root / "1" / "configs" / "characters.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps(characters), encoding="utf-8")
    monkeypatch.setattr(moments, "_get_characters_config_file", lambda **kw: str(config))
    monkeypatch.setattr(moments, "get_characters_config_for_current_user", lambda: characters)
    monkeypatch.setattr(app, "get_char_name", lambda cid: characters.get(cid, {}).get("name", cid))
    monkeypatch.setattr(moments, "get_paths", lambda cid, user_id=None: (
        str(users_root / str(user_id) / cid / "chat.db"),
        str(users_root / str(user_id) / cid / "prompts"),
    ))
    forbidden = Mock(side_effect=AssertionError("Memory recording must not call a model"))
    for module, names in ((app, ("call_ai_to_summarize", "call_openrouter", "call_gemini")),
                          (memory, ("call_ai_to_summarize",))):
        for name in names:
            monkeypatch.setattr(module, name, forbidden)

    post = {"char_id": "author", "timestamp": POST_TIME, "content": POST_CONTENT, "comments": []}
    path = config.parent / "moments_data.json"

    def save():
        path.write_text(json.dumps([post], ensure_ascii=False), encoding="utf-8")

    def events(cid):
        file = users_root / "1" / cid / "prompts" / "6_memory_short.json"
        data = json.loads(file.read_text(encoding="utf-8"))
        return [(day, event) for day, value in data.items() for event in value["events"]]

    save()
    yield accounts_client, moments, post, save, events
    forbidden.assert_not_called()
    assert not (users_root / "2" / "speaker").exists()


@pytest.mark.parametrize("entry", ["post", "comment", "self_comment", "character_reply", "user_reply"])
def test_remember_uses_full_content_and_original_event_day(memory_case, entry):
    client, _, post, save, events = memory_case
    payload = {"char_id": "author", "timestamp": POST_TIME, "user_id": 2}
    target = "author" if entry in {"post", "self_comment"} else "speaker"
    if entry != "post":
        if entry in {"character_reply", "user_reply"}:
            parent_id = "user" if entry == "user_reply" else "author"
            post["comments"].append({"commenter_id": parent_id, "content": PARENT_CONTENT})
        comment = {"commenter_id": target, "content": REPLY_CONTENT, "timestamp": REPLY_TIME}
        if entry in {"character_reply", "user_reply"}:
            comment["reply_to"] = parent_id
        post["comments"].append(comment)
        payload["comment_index"] = len(post["comments"]) - 1
    save()
    for _ in range(2):
        response = client.post("/api/moments/memory/regenerate", json=payload)
        assert response.status_code == 200, response.json
    stored = events(target)
    assert len(stored) == 1
    day, event = stored[0]
    assert day == ("2026-10-14" if entry == "post" else "2026-10-15")
    assert event["time"] == ("23:59" if entry == "post" else "00:01")
    assert event["event"].startswith("[朋友圈]")
    assert POST_CONTENT in event["event"]
    if entry != "post":
        assert REPLY_CONTENT in event["event"]
    if entry in {"character_reply", "user_reply"}:
        assert PARENT_CONTENT in event["event"]


def test_reply_remember_keeps_selected_parent_when_author_has_multiple_comments(memory_case, monkeypatch):
    client, moments, post, save, events = memory_case
    post["comments"] = [
        {"commenter_id": "author", "content": PARENT_CONTENT, "timestamp": POST_TIME},
        {"commenter_id": "author", "content": "另一条无关评论", "timestamp": REPLY_TIME},
    ]
    save()
    monkeypatch.setattr(moments, "_generate_ai_reply_to_any_comment", Mock(return_value=REPLY_CONTENT))
    response = client.post("/api/moments/comment/ai_reply", json={
        "char_id": "author", "timestamp": POST_TIME, "comment_index": 0, "replying_char_id": "speaker",
    })
    assert response.status_code == 200, response.json
    assert response.json["comment"]["reply_to_comment"]["content"] == PARENT_CONTENT
    response = client.post("/api/moments/memory/regenerate", json={
        "char_id": "author", "timestamp": POST_TIME, "comment_index": 2,
    })
    assert response.status_code == 200, response.json
    text = events("speaker")[0][1]["event"]
    assert POST_CONTENT in text and PARENT_CONTENT in text and REPLY_CONTENT in text
    assert "另一条无关评论" not in text


@pytest.mark.parametrize("entry", ["comment", "user_reply", "comment_reply", "publish", "regenerate"])
def test_live_actions_only_use_model_for_content_not_memory(memory_case, monkeypatch, entry):
    import app

    client, moments, post, _, events = memory_case
    content = POST_CONTENT if entry in {"publish", "regenerate"} else REPLY_CONTENT
    model = Mock(return_value=content)
    monkeypatch.setattr(app, "call_openrouter", model)
    monkeypatch.setattr(app, "get_model_config", lambda *a, **kw: ("relay", "test"))
    monkeypatch.setattr(app, "should_use_prompt_v2", lambda *a: True)
    monkeypatch.setattr(app, "build_system_prompt_v2", lambda *a, **kw: "Persona")
    monkeypatch.setattr(app, "get_ai_language", lambda *a, **kw: "zh")
    monkeypatch.setattr(app, "process_agent_actions", lambda cid, text, *a, **kw: (text, None, None))
    monkeypatch.setattr(moments, "sync_memory_before_moments", lambda *a, **kw: (True, None))
    monkeypatch.setattr(moments, "process_moments_media_tags", lambda text, *a, **kw: text)
    monkeypatch.setattr(moments, "_get_moments_name_to_id", lambda **kw: {})
    monkeypatch.setattr(moments, "_background_generate_moment_reactions", Mock())

    target = "author" if entry in {"user_reply", "publish", "regenerate"} else "speaker"
    if entry == "regenerate":
        response = client.post("/api/moments/regenerate", json={"char_id": "author", "timestamp": POST_TIME})
        assert response.status_code == 200, response.json
    else:
        with client.application.test_request_context():
            if entry == "comment":
                assert moments._generate_moment_comment("speaker", "author", POST_CONTENT, user_id=1) == content
            elif entry == "user_reply":
                assert moments._generate_moment_reply_to_user("author", POST_CONTENT, PARENT_CONTENT, user_id=1) == content
            elif entry == "comment_reply":
                assert moments._generate_ai_reply_to_any_comment(
                    "speaker", "author", POST_CONTENT,
                    [{"commenter_id": "author", "content": PARENT_CONTENT}], 0, user_id=1,
                ) == content
            else:
                assert moments.trigger_active_moments("author", user_id=1)
    model.assert_called_once()
    stored = events(target)
    assert len(stored) == 1
    text = stored[0][1]["event"]
    assert POST_CONTENT in text
    if entry in {"comment", "user_reply", "comment_reply"}:
        assert REPLY_CONTENT in text
    if entry in {"user_reply", "comment_reply"}:
        assert PARENT_CONTENT in text
