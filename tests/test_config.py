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

    def test_deep_sleep_rules_cover_temporary_unavailability_and_overwrite(self):
        from core.config import (
            GLOBAL_SYSTEM_RULES_EN_AGENT,
            GLOBAL_SYSTEM_RULES_JA_AGENT,
            GLOBAL_SYSTEM_RULES_ZH_AGENT,
        )

        assert "比赛、上课" in GLOBAL_SYSTEM_RULES_ZH_AGENT
        assert "每次设置都会覆盖原时间段" in GLOBAL_SYSTEM_RULES_ZH_AGENT
        assert "把结束时间改为" in GLOBAL_SYSTEM_RULES_ZH_AGENT

        assert "試合や授業" in GLOBAL_SYSTEM_RULES_JA_AGENT
        assert "以前の時間帯を上書き" in GLOBAL_SYSTEM_RULES_JA_AGENT
        assert "終了時刻" in GLOBAL_SYSTEM_RULES_JA_AGENT

        assert "match or class" in GLOBAL_SYSTEM_RULES_EN_AGENT
        assert "replaces the previous window" in GLOBAL_SYSTEM_RULES_EN_AGENT
        assert "change the end" in GLOBAL_SYSTEM_RULES_EN_AGENT

    def test_offline_context_includes_calls_without_blocking_action_tags(self):
        from core.config import get_mode_context

        zh = get_mode_context("zh", "offline")
        ja = get_mode_context("ja", "offline")
        en = get_mode_context("en", "offline")

        assert "见面、语音通话或视频通话" in zh
        assert "语音通话时不得声称看见用户" in zh
        assert "Agent Action Tags 不受此限制" in zh

        assert "対面、音声通話、ビデオ通話" in ja
        assert "音声通話ではユーザーが見えると述べない" in ja
        assert "Agent Action Tags はこの制限の対象外" in ja

        assert "in person, on an audio call, or on a video call" in en
        assert "On an audio call, do not claim to see the user" in en
        assert "Agent Action Tags are exempt" in en

    def test_online_special_messages_offer_private_transfer_with_currency(self):
        from core.config import (
            GLOBAL_SYSTEM_RULES_EN_MODE_ONLINE,
            GLOBAL_SYSTEM_RULES_JA_MODE_ONLINE,
            GLOBAL_SYSTEM_RULES_ZH_MODE_ONLINE,
        )

        for rules in (
            GLOBAL_SYSTEM_RULES_ZH_MODE_ONLINE,
            GLOBAL_SYSTEM_RULES_JA_MODE_ONLINE,
            GLOBAL_SYSTEM_RULES_EN_MODE_ONLINE,
        ):
            assert "[转账:88.00元" in rules
            assert "[转账:1000円]" in rules
            assert "[转账:$12.50" in rules

        assert "仅限与用户单聊" in GLOBAL_SYSTEM_RULES_ZH_MODE_ONLINE
        assert "個別チャットのみ" in GLOBAL_SYSTEM_RULES_JA_MODE_ONLINE
        assert "private chat with the user only" in GLOBAL_SYSTEM_RULES_EN_MODE_ONLINE


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
