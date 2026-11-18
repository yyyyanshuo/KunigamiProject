import pytest


@pytest.mark.parametrize("authenticated", [False, True])
@pytest.mark.parametrize("payload", [
    {},
    {"email": "no-confirm@example.com", "password": "secret"},
    {"email": "complete@example.com", "password": "secret", "adult_confirmed": True,
     "legal_accepted": True, "terms_version": "2.0-review.1", "privacy_version": "2.0-review.1"},
])
def test_public_registration_is_removed(accounts_client, authenticated, payload):
    if not authenticated:
        with accounts_client.session_transaction() as session:
            session.clear()
    assert accounts_client.get("/register").status_code == 404
    assert accounts_client.post("/api/register", json=payload).status_code == 404
    assert accounts_client.get("/api/register").status_code == 404
