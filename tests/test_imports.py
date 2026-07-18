"""测试所有模块能否正确导入，验证重构后模块间依赖完整。"""

import importlib
import sys
import os

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_import_core_config():
    from core.config import (
        BASE_DIR, USERS_DB, SQUARE_DB, USERS_ROOT, CHARACTERS_DIR,
        GEMINI_KEY, USE_OPENROUTER, OPENROUTER_KEY,
        get_global_system_rules, get_mode_context,
        COS_BASE_URL, CACHED_OFFICIAL_PACKS,
    )
    assert BASE_DIR is not None
    assert USERS_DB is not None
    assert USERS_ROOT is not None


def test_import_core_context():
    from core.context import (
        _background_user_var,
        set_background_user,
        clear_background_user,
        get_current_user_id,
        list_all_user_ids,
        init_users_db,
        GEMINI_FATAL_CODES,
        RELAY_FATAL_CODES,
        reset_api_fatal_error,
        mark_api_fatal_error,
        get_api_fatal_error,
    )
    assert set_background_user is not None
    assert get_current_user_id is not None


def test_import_core_utils():
    from core.utils import (
        _add_furigana_to_japanese,
        get_paths,
        safe_save_json,
        _get_characters_config_file,
        _get_groups_config_file,
        get_characters_config_for_current_user,
        get_groups_config_for_current_user,
        get_all_char_ids_for_current_user,
        get_all_group_ids_for_current_user,
        _get_locations_file,
        _get_character_positions_file,
        _get_user_position_file,
        get_current_username,
        get_effective_gemini_key,
        get_effective_openrouter_key,
        kks, EMOJI_SPLIT_RE,
        init_map_data, load_locations, save_locations,
        load_character_positions, save_character_positions,
        load_user_position, save_user_position,
        calc_distance, get_location_by_id, get_location_at_coord,
    )
    assert _add_furigana_to_japanese is not None
    assert get_paths is not None
    assert safe_save_json is not None
    assert calc_distance is not None


def test_import_services():
    from services.ai_client import (
        call_gemini, call_openrouter, get_model_config,
        get_relay_provider, log_full_prompt,
    )
    from services.prompt_builder import (
        build_system_prompt_v2, build_system_prompt,
        get_ai_language, get_char_name,
    )
    from services.memory import (
        call_ai_to_summarize, update_short_memory_for_date,
    )
    assert call_gemini is not None
    assert build_system_prompt_v2 is not None
    assert call_ai_to_summarize is not None


def test_import_all_blueprints():
    blueprints = [
        "admin", "auth", "chat", "forum", "group", "lbs",
        "media", "moments", "square", "views",
    ]
    for bp_name in blueprints:
        module_name = f"blueprints.{bp_name}"
        mod = importlib.import_module(module_name)
        # lbs blueprint uses 'map_bp' as variable name
        if bp_name == "lbs":
            assert hasattr(mod, "map_bp"), f"{module_name} missing map_bp"
        else:
            bp_attr = f"{bp_name}_bp"
            assert hasattr(mod, bp_attr), f"{module_name} missing {bp_attr}"
        assert mod.__name__ is not None
        print(f"  ✅ {module_name}")


def test_import_app():
    import app
    assert app.app is not None
    assert len(app.app.blueprints) >= 8, f"Expected >= 8 blueprints, got {len(app.app.blueprints)}"


def test_import_external_modules():
    import memory_jobs
    import agent_utils
    import music_api
    import music_manager
    import cos_utils
    import weather_api
    assert memory_jobs is not None
    assert agent_utils is not None
