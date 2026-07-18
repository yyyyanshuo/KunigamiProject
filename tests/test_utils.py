"""测试 core/utils.py 中的工具函数。"""

import os
import json
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestFurigana:
    def test_empty_string(self):
        from core.utils import _add_furigana_to_japanese
        assert _add_furigana_to_japanese("") == ""
        assert _add_furigana_to_japanese(None) is None

    def test_plain_ascii(self):
        from core.utils import _add_furigana_to_japanese
        result = _add_furigana_to_japanese("Hello World")
        assert "Hello" in result
        assert "World" in result

    def test_no_ruby_for_no_kanji(self):
        from core.utils import _add_furigana_to_japanese
        result = _add_furigana_to_japanese("こんにちは")
        assert "<ruby>" not in result

    def test_emoji_preserved(self):
        from core.utils import _add_furigana_to_japanese
        result = _add_furigana_to_japanese("😀 こんにちは")
        assert "😀" in result

    def test_sticker_tag_preserved(self):
        from core.utils import _add_furigana_to_japanese
        result = _add_furigana_to_japanese("[表情]开心")
        assert "[表情]开心" in result


class TestCalcDistance:
    def test_same_point(self):
        from core.utils import calc_distance
        assert calc_distance(0, 0, 0, 0) == 0.0

    def test_345_triangle(self):
        from core.utils import calc_distance
        assert abs(calc_distance(0, 0, 3, 4) - 5.0) < 0.0001

    def test_diagonal(self):
        from core.utils import calc_distance
        d = calc_distance(0, 0, 1, 1)
        assert abs(d - 1.4142) < 0.001


class TestSafeSaveJson:
    def test_write_and_read(self, tmp_json_file):
        from core.utils import safe_save_json
        data = {"key": "value", "number": 42}
        safe_save_json(tmp_json_file, data)
        with open(tmp_json_file, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded == data

    def test_overwrite(self, tmp_json_file):
        from core.utils import safe_save_json
        safe_save_json(tmp_json_file, {"a": 1})
        safe_save_json(tmp_json_file, {"b": 2})
        with open(tmp_json_file, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded == {"b": 2}

    def test_unicode(self, tmp_json_file):
        from core.utils import safe_save_json
        data = {"name": "日本語テスト"}
        safe_save_json(tmp_json_file, data)
        with open(tmp_json_file, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded["name"] == "日本語テスト"

    def test_nested_dict(self, tmp_json_file):
        from core.utils import safe_save_json
        data = {"user": {"name": "Alice", "age": 30}}
        safe_save_json(tmp_json_file, data)
        with open(tmp_json_file, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded["user"]["name"] == "Alice"


class TestGetPaths:
    def test_returns_tuple(self):
        from core.utils import get_paths
        result = get_paths("test_char")
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert result[0].endswith("chat.db")
        assert result[1].endswith("prompts")

    def test_same_char_same_path(self):
        from core.utils import get_paths
        a = get_paths("test_char")
        b = get_paths("test_char")
        assert a == b


class TestMapData:
    def test_init_map_data(self):
        from core.utils import init_map_data
        # 应该不抛异常
        init_map_data()

    def test_load_locations(self):
        from core.utils import init_map_data, load_locations
        init_map_data()
        locs = load_locations()
        assert "locations" in locs
        assert len(locs["locations"]) >= 1
        assert locs["locations"][0]["id"] == "home"

    def test_get_location_by_id(self):
        from core.utils import init_map_data, get_location_by_id
        init_map_data()
        home = get_location_by_id("home")
        assert home is not None
        assert home["name"] == "家"

    def test_get_location_by_id_nonexistent(self):
        from core.utils import init_map_data, get_location_by_id
        init_map_data()
        result = get_location_by_id("nonexistent_place")
        assert result is None

    def test_get_location_at_coord(self):
        from core.utils import init_map_data, get_location_at_coord
        init_map_data()
        result = get_location_at_coord(0, 0)
        assert result is not None
        assert result["id"] == "home"

    def test_get_location_at_coord_far(self):
        from core.utils import init_map_data, get_location_at_coord
        init_map_data()
        result = get_location_at_coord(999, 999)
        assert result is None

    def test_save_and_load_locations(self):
        from core.utils import init_map_data, load_locations, save_locations
        init_map_data()
        original = load_locations()
        original["locations"].append({"id": "temp_test", "name": "Temp", "x": 1, "y": 2, "is_default": False})
        save_locations(original)
        reloaded = load_locations()
        ids = [l["id"] for l in reloaded["locations"]]
        assert "temp_test" in ids
        # cleanup
        reloaded["locations"] = [l for l in reloaded["locations"] if l["id"] != "temp_test"]
        save_locations(reloaded)

    def test_load_user_position(self):
        from core.utils import init_map_data, load_user_position
        init_map_data()
        pos = load_user_position()
        assert "x" in pos
        assert "y" in pos
        assert "location_id" in pos


class TestConfigGetters:
    def test_get_characters_config_file(self):
        from core.utils import _get_characters_config_file
        path = _get_characters_config_file()
        assert isinstance(path, str)
        assert path.endswith("characters.json")

    def test_get_groups_config_file(self):
        from core.utils import _get_groups_config_file
        path = _get_groups_config_file()
        assert isinstance(path, str)
        assert path.endswith("groups.json")

    def test_get_locations_file(self):
        from core.utils import _get_locations_file
        path = _get_locations_file()
        assert isinstance(path, str)
        assert path.endswith("locations.json")

    def test_get_current_username_default(self):
        from core.utils import get_current_username
        name = get_current_username()
        assert isinstance(name, str)


class TestEmojiSplitRe:
    def test_emoji_regex_exists(self):
        from core.utils import EMOJI_SPLIT_RE
        assert EMOJI_SPLIT_RE is not None
        import re
        assert isinstance(EMOJI_SPLIT_RE, re.Pattern)


class TestKakasiInit:
    def test_kks_initialized(self):
        from core.utils import kks
        assert kks is not None
