"""测试所有 Blueprint 路由是否注册正确，以及端点可达性。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ===== 路由注册测试 =====

BLUEPRINT_ROUTES = {
    "admin": [
        "/admin/dashboard",
        "/api/admin/stats",
        "/api/admin/refresh_stickers",
        "/api/admin/impersonate",
        "/api/admin/impersonation/status",
        "/api/admin/impersonation/exit",
    ],
    "auth": [
        "/login", "/register", "/forgot_password", "/logout",
        "/api/register", "/api/login",
        "/api/forgot_password/send_code", "/api/forgot_password/reset",
        "/api/accounts/recent", "/api/accounts/switch",
        "/api/subscribe", "/api/vapid_public_key",
    ],
    "views": [
        "/", "/profile", "/guide", "/sakura",
        "/manifest.json", "/sw.js",
    ],
    "chat": [
        "/api/<char_id>/mark_read",
        "/api/<char_id>/history",
        "/api/<char_id>/chat",
        "/api/<char_id>/chat_v2",
        "/api/<char_id>/regenerate",
        "/api/<char_id>/messages/<int:msg_id>",
        "/api/<char_id>/chat_background",
        "/api/<char_id>/upload_chat_background",
        "/api/<char_id>/save_chat_background",
        "/api/<char_id>/memory/snapshot",
        "/api/<char_id>/memory/regenerate_medium",
        "/api/<char_id>/memory/regenerate_long",
        "/api/<char_id>/memory/regenerate_short",
        "/api/<char_id>/debug/force_maintenance",
        "/api/<char_id>/prompts_data",
        "/api/<char_id>/relationship_reverse",
        "/api/<char_id>/save_relationship_reverse",
        "/api/<char_id>/save_prompt",
        "/api/<char_id>/search",
        "/api/<char_id>/config",
        "/api/<char_id>/update_meta",
        "/api/<char_id>/upload_avatar",
        "/api/<target_char_id>/copy_schedule",
        "/api/character/<char_id>/delete",
        "/api/agent/chat",
        "/api/agent/notify_user",
        "/api/agent/reply",
        "/api/agent/stop",
        "/api/agent/web_action",
        "/api/agent/state",
    ],
    "group": [
        "/api/group/<group_id>/history",
        "/api/group/<group_id>/chat",
        "/api/group/<group_id>/messages/<int:msg_id>",
        "/api/group/<group_id>/chat_background",
        "/api/group/<group_id>/upload_chat_background",
        "/api/group/<group_id>/save_chat_background",
        "/api/group/<group_id>/memory/snapshot",
        "/api/group/<group_id>/prompts_data",
        "/api/group/<group_id>/save_memory",
        "/api/group/<group_id>/update_meta",
        "/api/group/<group_id>/search",
        "/api/group/<group_id>/config",
        "/api/group/<group_id>/upload_avatar",
        "/api/groups/add",
        "/api/group/<group_id>/delete",
    ],
    "lbs": [
        "/map",
        "/api/map/locations",
        "/api/map/locations/<loc_id>",
        "/api/map/positions",
        "/api/map/character/<char_id>/position",
        "/api/map/user/position",
        "/api/map/character/<char_id>/locations",
        "/api/map/character/<char_id>/known_locations",
        "/api/map/weather",
        "/api/map/weather/<loc_id>",
        "/api/map/geocode",
    ],
    "media": [
        "/api/stickers/allowed_descriptions",
        "/api/stickers/packs",
        "/api/stickers/my_packs",
        "/api/stickers/packs/add",
        "/api/stickers/search",
        "/api/stickers/favorites",
        "/api/stickers/upload",
        "/api/stickers/packs/upload",
        "/api/stickers/file",
        "/api/stickers/resolve",
        "/api/music/search",
        "/api/music/play",
        "/api/music/pause",
        "/api/music/resume",
        "/api/music/stop",
        "/api/music/next",
        "/api/music/prev",
        "/api/music/volume",
        "/api/music/state",
        "/api/music/playlist/list",
        "/api/music/playlist/create",
        "/api/music/playlist/add",
        "/api/music/recommend",
        "/api/vision/upload",
        "/api/furigana",
        "/api/voice_clone",
        "/api/<char_id>/tts",
        "/api/<char_id>/tts_voice",
    ],
    "moments": [
        "/moments",
        "/api/moments",
        "/api/moments/active_enabled",
        "/api/moments/characters",
        "/api/moments/related_characters",
        "/api/moments/like",
        "/api/moments/comment",
        "/api/moments/comment/regenerate",
        "/api/moments/memory/regenerate",
        "/api/moments/force_active",
        "/api/moments/post/ai_comment",
        "/api/moments/comment/user_reply",
        "/api/moments/post",
        "/api/moments/regenerate",
        "/api/moments/delete",
        "/api/moments/edit",
        "/api/moments/comment/delete",
        "/api/moments/comment/edit",
    ],
    "square": [
        "/square",
        "/square/upload",
        "/api/square/ips",
        "/api/square/search_ip",
        "/api/square/list",
        "/api/square/upload",
        "/api/square/like",
        "/api/square/favorite",
        "/api/square/favorites/list",
        "/api/square/my_posts",
        "/api/square/delete",
        "/api/square/comment",
        "/api/square/update",
        "/api/square/add_to_local",
        "/api/square/ai_complete_graph",
    ],
}


class TestRouteRegistration:
    """验证所有 Blueprint 路由在 URL map 中注册正确。"""

    @classmethod
    def setup_class(cls):
        from app import app
        cls.app = app
        cls.client = app.test_client()
        cls.rules = [(r.rule, r.methods) for r in app.url_map.iter_rules()]

    def _rule_exists(self, path, methods=None):
        for rule, rule_methods in self.rules:
            if rule == path:
                if methods is None:
                    return True
                return bool(rule_methods & methods)
        return False

    def test_all_blueprint_routes_registered(self):
        missing = []
        for bp_name, routes in BLUEPRINT_ROUTES.items():
            for route in routes:
                if not self._rule_exists(route):
                    missing.append(f"{bp_name}:{route}")
        assert len(missing) == 0, f"Missing routes:\n" + "\n".join(missing)

    def test_blueprint_count(self):
        assert len(self.app.blueprints) >= 8, f"Expected >= 8, got {len(self.app.blueprints)}"

    def test_admin_routes(self):
        for route in BLUEPRINT_ROUTES["admin"]:
            assert self._rule_exists(route), f"Admin route missing: {route}"

    def test_auth_routes(self):
        for route in BLUEPRINT_ROUTES["auth"]:
            assert self._rule_exists(route), f"Auth route missing: {route}"

    def test_views_routes(self):
        for route in BLUEPRINT_ROUTES["views"]:
            assert self._rule_exists(route), f"Views route missing: {route}"

    def test_chat_routes(self):
        for route in BLUEPRINT_ROUTES["chat"]:
            assert self._rule_exists(route), f"Chat route missing: {route}"

    def test_group_routes(self):
        for route in BLUEPRINT_ROUTES["group"]:
            assert self._rule_exists(route), f"Group route missing: {route}"

    def test_map_routes(self):
        for route in BLUEPRINT_ROUTES["lbs"]:
            assert self._rule_exists(route), f"Map route missing: {route}"

    def test_media_routes(self):
        for route in BLUEPRINT_ROUTES["media"]:
            assert self._rule_exists(route), f"Media route missing: {route}"

    def test_moments_routes(self):
        for route in BLUEPRINT_ROUTES["moments"]:
            assert self._rule_exists(route), f"Moments route missing: {route}"

    def test_square_routes(self):
        for route in BLUEPRINT_ROUTES["square"]:
            assert self._rule_exists(route), f"Square route missing: {route}"

    def test_total_route_count(self):
        count = sum(1 for _ in self.app.url_map.iter_rules())
        assert count == 201, f"Expected 201 routes, got {count}"


class TestRouteEndpoints:
    """验证路由端点返回非 500 错误（未授权 302/401 是正常行为）。"""

    @classmethod
    def setup_class(cls):
        from app import app
        app.config["TESTING"] = True
        cls.client = app.test_client()

    def _get(self, path, expected_codes=(200, 302, 401, 403, 404)):
        resp = self.client.get(path)
        assert resp.status_code in expected_codes, \
            f"GET {path} → {resp.status_code} (expected {expected_codes})"
        return resp

    def _post(self, path, json_data=None, expected_codes=(200, 302, 400, 401, 403, 404, 415)):
        resp = self.client.post(path, json=json_data or {}, content_type="application/json")
        assert resp.status_code in expected_codes, \
            f"POST {path} → {resp.status_code} (expected {expected_codes})"
        return resp

    # Auth pages
    def test_login_page(self): self._get("/login")
    def test_register_page(self): self._get("/register")
    def test_forgot_password_page(self): self._get("/forgot_password")

    # Main pages (redirect to login when no session)
    def test_index_redirects(self):
        resp = self._get("/", expected_codes=(200, 302))
    def test_profile_redirects(self):
        resp = self._get("/profile", expected_codes=(200, 302))
    def test_guide_is_public(self):
        resp = self._get("/guide", expected_codes=(200,))
        assert "使用文档" in resp.get_data(as_text=True)

    # Auth API - error handling
    def test_login_empty(self):
        self._post("/api/login", {}, expected_codes=(400, 401))
    def test_register_empty(self):
        self._post("/api/register", {}, expected_codes=(400, 401))

    # Static files
    def test_manifest(self): self._get("/manifest.json")
    def test_sw(self): self._get("/sw.js")

    # GET endpoints that should return data even without auth
    def test_vapid_public_key(self):
        resp = self.client.get("/api/vapid_public_key")
        assert resp.status_code in (200, 401), f"Expected 200 or 401, got {resp.status_code}"

    # POST endpoints that require auth should return 401/302
    def test_chat_requires_auth(self):
        resp = self.client.post("/api/test_char/chat", json={"message": "hi"},
                                content_type="application/json")
        assert resp.status_code in (302, 401), f"Expected 302 or 401, got {resp.status_code}"

    def test_group_chat_requires_auth(self):
        resp = self.client.post("/api/group/test_group/chat", json={"message": "hi"},
                                content_type="application/json")
        assert resp.status_code in (302, 401), f"Expected 302 or 401, got {resp.status_code}"

    # Global config (should be accessible)
    def test_global_config(self):
        resp = self.client.get("/api/global_config")
        assert resp.status_code in (200, 302, 401)

    # System config
    def test_system_config_get(self):
        resp = self.client.get("/api/system_config")
        assert resp.status_code in (200, 302, 401)
