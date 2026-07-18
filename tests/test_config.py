"""测试 core/config.py 中的配置常量和辅助函数。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestConfigPaths:
    def test_base_dir_exists(self):
        from core.config import BASE_DIR
        assert os.path.exists(BASE_DIR), f"BASE_DIR {BASE_DIR} does not exist"

    def test_base_dir_is_project_root(self):
        from core.config import BASE_DIR
        assert os.path.exists(os.path.join(BASE_DIR, "app.py")), "app.py not in BASE_DIR"
        assert os.path.exists(os.path.join(BASE_DIR, "core", "config.py")), "core/config.py not in BASE_DIR"

    def test_key_directories_exist(self):
        from core.config import (
            CHARACTERS_DIR, GROUPS_DIR, USERS_ROOT,
        )
        # 这些目录可能为空或不存在，但路径必须是字符串
        assert isinstance(CHARACTERS_DIR, str)
        assert isinstance(GROUPS_DIR, str)
        assert isinstance(USERS_ROOT, str)

    def test_database_paths(self):
        from core.config import (
            USERS_DB, SQUARE_DB, DATABASE_FILE,
        )
        assert USERS_DB.endswith("users.db")
        assert SQUARE_DB.endswith("square.db")
        assert DATABASE_FILE == "chat_history.db"

    def test_configs_dir_exists(self):
        from core.config import USERS_DB
        configs_dir = os.path.dirname(USERS_DB)
        assert os.path.exists(configs_dir), f"configs dir {configs_dir} not found"

    def test_static_dirs(self):
        from core.config import SQUARE_AVATARS_DIR
        assert isinstance(SQUARE_AVATARS_DIR, str)
        assert "square_avatars" in SQUARE_AVATARS_DIR

    def test_sticker_config(self):
        from core.config import (
            STICKERS_ROOT, STICKER_IMAGE_EXT, STICKER_DESCRIPTIONS_FILE,
        )
        assert isinstance(STICKERS_ROOT, str)
        assert ".png" in STICKER_IMAGE_EXT
        assert ".jpg" in STICKER_IMAGE_EXT
        assert ".gif" in STICKER_IMAGE_EXT
        assert isinstance(STICKER_DESCRIPTIONS_FILE, str)


class TestSystemRules:
    def test_get_global_system_rules_zh(self):
        from core.config import get_global_system_rules
        rules = get_global_system_rules("zh", "online")
        assert isinstance(rules, str)
        assert len(rules) > 500
        assert "基本行为" in rules or "Basic" in rules

    def test_get_global_system_rules_ja(self):
        from core.config import get_global_system_rules
        rules = get_global_system_rules("ja", "online")
        assert isinstance(rules, str)
        assert len(rules) > 500

    def test_get_global_system_rules_en(self):
        from core.config import get_global_system_rules
        rules = get_global_system_rules("en", "online")
        assert isinstance(rules, str)
        assert len(rules) > 500

    def test_get_mode_context_online(self):
        from core.config import get_mode_context
        ctx = get_mode_context("zh", "online")
        assert isinstance(ctx, str)
        assert len(ctx) > 10

    def test_get_mode_context_offline(self):
        from core.config import get_mode_context
        ctx = get_mode_context("zh", "offline")
        assert isinstance(ctx, str)
        assert len(ctx) > 10

    def test_online_offline_differ(self):
        from core.config import get_mode_context
        on = get_mode_context("zh", "online")
        off = get_mode_context("zh", "offline")
        assert on != off, "online and offline mode context should differ"


class TestConfigConstants:
    def test_max_context_lines(self):
        from core.config import MAX_CONTEXT_LINES
        assert isinstance(MAX_CONTEXT_LINES, int)
        assert MAX_CONTEXT_LINES > 0

    def test_cached_official_packs(self):
        from core.config import CACHED_OFFICIAL_PACKS
        assert CACHED_OFFICIAL_PACKS is None

    def test_env_keys_present(self):
        from core.config import (
            GEMINI_KEY, OPENROUTER_KEY, OPENROUTER_BASE_URL,
            SILICONFLOW_KEY, SERPER_KEY,
        )
        # Keys may be empty if not configured in .env
        assert isinstance(GEMINI_KEY, str)
        assert isinstance(OPENROUTER_KEY, str)
