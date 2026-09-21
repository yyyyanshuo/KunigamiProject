from pathlib import Path


TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "memory.html"


def test_relationship_editor_does_not_reparse_dynamic_cards():
    source = TEMPLATE.read_text(encoding="utf-8")
    relation_editor_start = source.index("// 4. Relation 编辑器")
    relation_editor_end = source.index("function updateRelationJsonStatus", relation_editor_start)
    relation_editor = source[relation_editor_start:relation_editor_end]

    assert "container.appendChild(listDiv);" in relation_editor
    assert "container.innerHTML +=" not in relation_editor
    assert relation_editor.count("container.insertAdjacentHTML(") >= 3
