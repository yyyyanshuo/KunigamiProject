import sqlite3
import re
import html
import pytest

from core.legal import init_legal_consents_table, get_current_legal_versions, get_user_legal_status, record_current_legal_consents


def _clear_session(client):
    with client.session_transaction() as flask_session:
        flask_session.clear()


@pytest.mark.parametrize("language,title", [("ja","プライバシーポリシー"),("en","Privacy Policy"),("zh-CN","隐私政策")])
def test_legal_document_pages_are_public(app_client, language, title):
    _clear_session(app_client)
    app_client.post("/api/ui/language", json={"user_language":language})
    for path in ("/terms", "/privacy"):
        response=app_client.get(path)
        assert response.status_code==200
        source=response.get_data(as_text=True)
        article=html.unescape(re.sub(r"<[^>]*>", " ", re.search(r"<article>(.*?)</article>", source,re.S)[1]))
        assert '2.0-review.1' in article
        assert '2026-09-23' in article
        assert 'legal-review-notice' in source
        assert '2026年8月11日' not in article
        assert '中华人民共和国法律' not in article
        assert '新加坡' not in article and 'Singapore' not in article and 'シンガポール' not in article
        if language=='en':
            assert 'Not yet in effect' in article
            assert not re.search(r'[\u4e00-\u9fff]',article)
        elif language=='ja':
            assert '未施行' in article
            assert '運営者' in article
        else:
            assert '尚未正式生效' in article
        if path=='/privacy':
            assert title.casefold() in article.casefold()
            for provider in ('OpenAI','Anthropic','xAI','ElevenLabs','Serper','Nominatim'):
                assert provider in article


def test_user_must_acknowledge_documents_after_admin_creation(accounts_client):
    import core.config
    created = accounts_client.post(
        "/api/admin/users", json={"email": "legal@example.com"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    ).get_json()
    assert get_user_legal_status(created["user_id"], db_path=core.config.USERS_DB)["current"] is False
    _clear_session(accounts_client)
    assert accounts_client.post("/api/login", json={
        "username": created["email"], "password": created["initial_password"],
    }).status_code == 200
    assert accounts_client.get("/api/legal/status").get_json()["current"] is False
    assert accounts_client.post("/api/legal/consent", json={}).status_code == 400
    assert accounts_client.post("/api/legal/consent", json={
        "legal_accepted": True, "terms_version": "0.9", "privacy_version": "1.0",
    }).status_code == 400
    versions = get_current_legal_versions()
    assert accounts_client.post("/api/legal/consent", json={
        "legal_accepted": True, "terms_version": versions["terms"], "privacy_version": versions["privacy"],
    }).status_code == 200
    assert accounts_client.get("/api/legal/status").get_json()["current"] is True
    with sqlite3.connect(core.config.USERS_DB) as conn:
        rows = conn.execute(
            "SELECT document_type, document_version, accepted_at FROM legal_consents WHERE user_id=? ORDER BY document_type",
            (created["user_id"],),
        ).fetchall()
    assert [(row[0], row[1]) for row in rows] == [("privacy", versions["privacy"]), ("terms", versions["terms"])]
    assert all(row[2] for row in rows)


def test_old_acceptance_is_not_current_and_is_preserved(tmp_path):
    database=tmp_path/'legal.db'
    with sqlite3.connect(database) as conn:
        init_legal_consents_table(conn)
        for kind in ('terms','privacy'):
            conn.execute('INSERT INTO legal_consents(user_id,document_type,document_version,accepted_at) VALUES (?,?,?,?)',(17,kind,'1.0','2026-08-11T00:00:00+00:00'))
    assert get_user_legal_status(17,db_path=str(database))['current'] is False
    with sqlite3.connect(database) as conn:
        record_current_legal_consents(conn,17)
    current=get_user_legal_status(17,db_path=str(database))
    assert current['current'] is True
    assert all(d['status']=='review' and d['effective_date'] is None for d in current['documents'].values())
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM legal_consents WHERE document_version='1.0'").fetchone()[0]==2
        assert conn.execute('SELECT COUNT(*) FROM legal_consents WHERE document_hash IS NOT NULL').fetchone()[0]==2
