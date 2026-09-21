import json

from blueprints.chat import normalize_relationship_graph


def test_relationship_parser_accepts_markdown_and_legacy_desc():
    raw = """```json
    {
      "角色甲": {"role": "队友", "score": 4.5, "desc": "并肩作战"},
      "角色乙": "旧友"
    }
    ```"""

    assert normalize_relationship_graph(raw) == {
        "角色甲": {
            "role": "队友",
            "score": 4.5,
            "description": "并肩作战",
        },
        "角色乙": {
            "role": "",
            "score": 1,
            "description": "旧友",
        },
    }


def test_relationship_parser_decodes_double_encoded_json_and_clamps_score():
    raw = json.dumps(json.dumps({
        "角色甲": {"role": "宿敌", "score": 99, "description": "竞争"},
    }, ensure_ascii=False), ensure_ascii=False)

    graph = normalize_relationship_graph(raw)

    assert graph["角色甲"]["score"] == 5


def test_relationship_parser_rejects_non_numeric_score():
    try:
        normalize_relationship_graph({"角色甲": {"score": "very close"}})
        assert False, "non-numeric score should fail"
    except ValueError as exc:
        assert "score 必须是数字" in str(exc)
