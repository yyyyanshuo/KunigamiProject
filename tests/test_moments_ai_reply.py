"""Exercise the actual reply route with isolated data and no model/network calls."""

import json
from unittest.mock import Mock

import pytest


URL = "/api/moments/comment/ai_reply"
TIMESTAMP = "2026-10-14 10:00:00"


@pytest.fixture
def reply_case(accounts_client, tmp_path, monkeypatch):
    from blueprints import moments

    monkeypatch.setattr(moments, "USERS_ROOT", str(tmp_path / "users"))
    monkeypatch.setattr(moments, "get_characters_config_for_current_user", lambda: {
        "author": {"name": "Author"}, "speaker": {"name": "Speaker"},
    })
    paths = []
    for user_id in (1, 2):
        path = tmp_path / "users" / str(user_id) / "configs" / "moments_data.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([{
            "char_id": "author", "timestamp": TIMESTAMP, "content": "Original post",
            "comments": [{"commenter_id": "author", "content": "Target comment", "timestamp": TIMESTAMP}],
        }]), encoding="utf-8")
        paths.append(path)
    generate = Mock(return_value="Character reply")
    monkeypatch.setattr(moments, "_generate_ai_reply_to_any_comment", generate)
    payload = {"char_id": "author", "timestamp": TIMESTAMP, "comment_index": 0, "replying_char_id": "speaker"}
    return accounts_client, paths, generate, payload


@pytest.mark.parametrize("index", [0, "0"])
def test_character_replies_to_first_comment_in_current_users_data(reply_case, index):
    client, (path, other_path), generate, payload = reply_case
    other_before = other_path.read_bytes()
    payload.update(comment_index=index, user_id=2)
    response = client.post(URL, json=payload)
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.json["status"] == "success"
    saved = json.loads(path.read_text(encoding="utf-8"))[0]["comments"]
    assert len(saved) == 2
    assert saved[-1] == response.json["comment"]
    assert saved[-1]["commenter_id"] == "speaker"
    assert saved[-1]["reply_to"] == "author"
    assert saved[-1]["content"] == "Character reply"
    assert saved[-1]["timestamp"]
    assert other_path.read_bytes() == other_before
    args = generate.call_args.kwargs
    assert args["user_id"] == 1
    assert args["target_comment_index"] == 0
    assert args["post_content"] == "Original post"
    assert args["replying_char_id"] == "speaker"


@pytest.mark.parametrize("index", [None, "bad", "", -1, 4, 0.5, True, [], {}])
def test_invalid_comment_index_is_json_and_does_not_generate(reply_case, index):
    client, (path, _), generate, payload = reply_case
    before = path.read_bytes()
    payload["comment_index"] = index
    response = client.post(URL, json=payload)
    assert response.status_code in (400, 404)
    assert response.is_json and response.json["error"]
    generate.assert_not_called()
    assert path.read_bytes() == before


@pytest.mark.parametrize("field", ["char_id", "timestamp", "replying_char_id"])
def test_missing_parameters_are_json(reply_case, field):
    client, _, generate, payload = reply_case
    del payload[field]
    response = client.post(URL, json=payload)
    assert response.status_code == 400
    assert response.is_json and response.json["error"]
    generate.assert_not_called()


@pytest.mark.parametrize("character", ["user", "missing-character"])
def test_replying_character_must_belong_to_user(reply_case, character):
    client, _, generate, payload = reply_case
    payload["replying_char_id"] = character
    response = client.post(URL, json=payload)
    assert response.status_code == 400
    assert response.is_json and response.json["error"]
    generate.assert_not_called()


@pytest.mark.parametrize("missing", ["file", "post"])
def test_missing_moment_is_json(reply_case, missing):
    client, (path, _), generate, payload = reply_case
    if missing == "file":
        path.unlink()
    else:
        payload["timestamp"] = "2000-01-01 00:00:00"
    response = client.post(URL, json=payload)
    assert response.status_code == 404
    assert response.is_json and response.json["error"]
    generate.assert_not_called()


@pytest.mark.parametrize("failure", [None, "exception"])
def test_generation_failure_does_not_save_a_reply(reply_case, failure):
    client, (path, _), generate, payload = reply_case
    before = path.read_bytes()
    generate.return_value = None
    if failure == "exception":
        generate.side_effect = RuntimeError("Synthetic model failure")
    response = client.post(URL, json=payload)
    assert response.status_code in (500, 502)
    assert response.is_json and response.json["error"]
    assert path.read_bytes() == before


def test_reply_requires_login(reply_case):
    client, _, generate, payload = reply_case
    with client.session_transaction() as session:
        session.clear()
    response = client.post(URL, json=payload)
    assert response.status_code == 401
    assert response.is_json
    generate.assert_not_called()


@pytest.mark.parametrize("kind", ["missing-route", "wrong-method", "malformed-json", "wrong-content-type"])
def test_http_errors_return_json_instead_of_html(reply_case, kind):
    client, _, generate, _ = reply_case
    if kind == "missing-route":
        response = client.post("/api/moments/does-not-exist", json={})
        expected = 404
    elif kind == "wrong-method":
        response = client.get(URL)
        expected = 405
        assert "POST" in response.headers["Allow"]
    elif kind == "malformed-json":
        response = client.post(URL, data="{", content_type="application/json")
        expected = 400
    else:
        response = client.post(URL, data="plain text", content_type="text/plain")
        expected = 415
    assert response.status_code == expected
    assert response.is_json and response.json["status"] == "error"
    generate.assert_not_called()


def test_non_api_404_keeps_html_response(reply_case):
    client, _, _, _ = reply_case
    response = client.get("/page-that-does-not-exist")
    assert response.status_code == 404
    assert response.mimetype == "text/html"


@pytest.mark.parametrize("route", ["relay", "gemini"])
@pytest.mark.parametrize("failed", [False, True])
def test_generator_uses_target_relationship_and_skips_provider_error_memory(accounts_client, monkeypatch, route, failed):
    import app as app_module
    from blueprints import moments
    from services.ai_client import ai_error_text

    sync = Mock()
    prompt = Mock(return_value="Character persona")
    model = Mock(return_value=ai_error_text("Synthetic failure") if failed else "Generated reply")
    memory = Mock()
    actions = Mock(return_value=("Generated reply", None, None))
    monkeypatch.setattr(moments, "sync_memory_before_moments", sync)
    monkeypatch.setattr(moments, "append_moment_event_to_short_memory", memory)
    monkeypatch.setattr(app_module, "get_char_name", lambda cid: cid)
    monkeypatch.setattr(app_module, "should_use_prompt_v2", lambda cid: True)
    monkeypatch.setattr(app_module, "build_system_prompt_v2", prompt)
    monkeypatch.setattr(app_module, "get_ai_language", lambda *a, **kw: "en")
    monkeypatch.setattr(app_module, "get_model_config", lambda *a, **kw: (route, "offline"))
    monkeypatch.setattr(app_module, "call_openrouter", model)
    monkeypatch.setattr(app_module, "call_gemini", model)
    monkeypatch.setattr(app_module, "process_agent_actions", actions)
    with app_module.app.test_request_context():
        result = moments._generate_ai_reply_to_any_comment(
            "speaker", "author", "Original post",
            [{"commenter_id": "target", "content": "Target comment"}], 0, user_id=1,
        )
    sync.assert_called_once_with("speaker", user_id=1)
    assert prompt.call_args.kwargs["target_char_id"] == "target"
    assert prompt.call_args.kwargs["user_id"] == 1
    assert model.call_args.kwargs["user_id"] == 1
    assert "Target comment" in model.call_args.args[0][1]["content"]
    if failed:
        assert result is None
        actions.assert_not_called()
        memory.assert_not_called()
    else:
        assert result == "Generated reply"
        memory.assert_called_once()
