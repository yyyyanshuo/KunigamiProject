def test_register_requires_adult_confirmation(app_client):
    resp = app_client.post(
        "/api/register",
        json={"email": "no-confirm@example.com", "password": "secret"},
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert "年满18周岁" in resp.get_json()["message"]


def test_register_empty_still_rejected(app_client):
    resp = app_client.post("/api/register", json={}, content_type="application/json")
    assert resp.status_code == 400
    assert resp.get_json()["status"] == "error"
