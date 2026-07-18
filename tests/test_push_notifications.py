import json

from flask import Flask

import blueprints.auth as auth_module
from blueprints.auth import auth_bp


def _make_client(tmp_path, monkeypatch, initial_data):
    subscriptions_file = tmp_path / "subscriptions.json"
    subscriptions_file.write_text(json.dumps(initial_data), encoding="utf-8")
    monkeypatch.setattr(auth_module, "SUBSCRIPTIONS_FILE", str(subscriptions_file))

    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="test-secret")
    app.register_blueprint(auth_bp)
    return app.test_client(), subscriptions_file


def _subscription(endpoint):
    return {
        "endpoint": endpoint,
        "keys": {"p256dh": "test-p256dh", "auth": "test-auth"},
    }


def test_subscribe_rejects_anonymous_user(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch, {})

    response = client.post("/api/subscribe", json=_subscription("https://push/one"))

    assert response.status_code == 401


def test_subscribe_replaces_legacy_list_with_user_mapping(tmp_path, monkeypatch):
    client, subscriptions_file = _make_client(
        tmp_path, monkeypatch, [_subscription("https://push/legacy")]
    )
    with client.session_transaction() as sess:
        sess["user_id"] = 1

    response = client.post("/api/subscribe", json=_subscription("https://push/user-1"))

    assert response.status_code == 200
    assert json.loads(subscriptions_file.read_text(encoding="utf-8")) == {
        "1": [_subscription("https://push/user-1")]
    }


def test_subscribe_allows_same_endpoint_for_explicitly_enabled_accounts(
    tmp_path, monkeypatch
):
    shared = _subscription("https://push/shared")
    client, subscriptions_file = _make_client(
        tmp_path,
        monkeypatch,
        {"1": [shared], "2": [_subscription("https://push/user-2")]},
    )
    with client.session_transaction() as sess:
        sess["user_id"] = 2

    response = client.post("/api/subscribe", json=shared)

    assert response.status_code == 200
    saved = json.loads(subscriptions_file.read_text(encoding="utf-8"))
    assert saved["1"] == [shared]
    assert saved["2"] == [_subscription("https://push/user-2"), shared]


def test_subscribe_deduplicates_endpoint_within_current_account(
    tmp_path, monkeypatch
):
    old = _subscription("https://push/shared")
    updated = {
        "endpoint": old["endpoint"],
        "keys": {"p256dh": "updated-p256dh", "auth": "updated-auth"},
    }
    client, subscriptions_file = _make_client(
        tmp_path, monkeypatch, {"1": [old]}
    )
    with client.session_transaction() as sess:
        sess["user_id"] = 1

    response = client.post("/api/subscribe", json=updated)

    assert response.status_code == 200
    saved = json.loads(subscriptions_file.read_text(encoding="utf-8"))
    assert saved["1"] == [updated]


def test_send_blocks_legacy_list(tmp_path, monkeypatch):
    _make_client(tmp_path, monkeypatch, [_subscription("https://push/legacy")])
    sent = []
    monkeypatch.setattr(auth_module, "webpush", lambda **kwargs: sent.append(kwargs))

    auth_module.send_push_notification("title", "body", user_id=1)

    assert sent == []


def test_send_blocks_missing_user_id(tmp_path, monkeypatch):
    _make_client(tmp_path, monkeypatch, {"1": [_subscription("https://push/user-1")]})
    sent = []
    monkeypatch.setattr(auth_module, "webpush", lambda **kwargs: sent.append(kwargs))

    auth_module.send_push_notification("title", "body")

    assert sent == []


def test_send_only_targets_requested_user(tmp_path, monkeypatch):
    user_one = _subscription("https://push/user-1")
    user_two = _subscription("https://push/user-2")
    _make_client(tmp_path, monkeypatch, {"1": [user_one], "2": [user_two]})
    sent = []
    monkeypatch.setattr(auth_module, "webpush", lambda **kwargs: sent.append(kwargs))

    auth_module.send_push_notification("title", "body", user_id=1)

    assert [call["subscription_info"] for call in sent] == [user_one]
