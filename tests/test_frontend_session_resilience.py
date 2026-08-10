from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_shared_auth_guard_redirects_and_preserves_return_path():
    source = _read("static/auth-guard.js")

    assert "response.status !== 401" in source
    assert "post_login_return_to" in source
    assert "window.location.replace('/login?reason=session_expired')" in source


def test_primary_authenticated_pages_use_the_auth_guard():
    for template_name in ("chat.html", "contacts.html", "memory.html", "profile.html"):
        source = _read(f"templates/{template_name}")
        assert '<script src="/static/auth-guard.js"></script>' in source
        assert "fetchWithAuthGuard(" in source
        assert "handleApiUnauthorized(" in source


def test_chat_and_memory_initialization_stop_after_auth_failure():
    chat = _read("templates/chat.html")
    memory = _read("templates/memory.html")

    assert "const uiReady = await initUI();" in chat
    assert "if (!uiReady) return;" in chat
    assert "const metaReady = await loadCharMeta();" in memory
    assert "if (!metaReady) return;" in memory
    assert "data.remark || data.name || charId" in memory


def test_contacts_rejects_non_array_api_payloads():
    contacts = _read("templates/contacts.html")

    assert "if (!Array.isArray(contacts))" in contacts
    assert "加载失败，请稍后重试" in contacts


def test_login_returns_to_page_saved_by_auth_guard():
    login = _read("templates/login.html")

    assert "post_login_return_to" in login
    assert "saved.startsWith('/')" in login
    assert "!saved.startsWith('//')" in login


def test_profile_validates_core_configuration_responses():
    profile = _read("templates/profile.html")

    assert "if (handleApiUnauthorized(resConfig)) return false;" in profile
    assert "if (handleApiUnauthorized(resUser)) return false;" in profile
    assert "线路配置数据格式错误" in profile
    assert "if (!Array.isArray(logs))" in profile
