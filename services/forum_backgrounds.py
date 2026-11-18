"""Account-scoped forum background sources and immutable text snapshots."""
import json
from pathlib import Path

from core.config import USERS_ROOT


def validate_config(value):
    if not isinstance(value, dict) or not isinstance(value.get("items", []), list):
        raise ValueError("Invalid background configuration")
    items = []
    seen = set()
    for item in value.get("items", []):
        if not isinstance(item, dict):
            raise ValueError("Invalid background item")
        fields = {key: item.get(key, "") for key in ("id", "kind", "label", "content")}
        if any(not isinstance(text, str) for text in fields.values()):
            raise ValueError("Invalid background text")
        if not fields["id"] or len(fields["id"]) > 200 or fields["kind"] not in {"user", "character", "saved"}:
            raise ValueError("Invalid background source")
        if len(fields["label"]) > 200 or not fields["content"].strip():
            raise ValueError("Invalid background content")
        if fields["id"] in seen:
            raise ValueError("Duplicate background source")
        seen.add(fields["id"])
        items.append(fields)
    note = value.get("note", "")
    if not isinstance(note, str) or len(items) > 100 or len(note) + sum(len(i["content"]) for i in items) > 200000:
        raise ValueError("Background is too large")
    return {"items": items, "note": note.strip()}


def validate_request(value):
    if not isinstance(value, dict) or not isinstance(value.get("mode"), str) or value["mode"] not in {"once", "save"}:
        raise ValueError("Invalid background mode")
    name = value.get("name", "")
    if not isinstance(name, str) or len(name.strip()) > 100:
        raise ValueError("Invalid background name")
    config = validate_config(value)
    if value["mode"] == "once" and name.strip():
        raise ValueError("Temporary backgrounds cannot be saved to the library")
    if name.strip() and not render_background(config):
        raise ValueError("Cannot save an empty background")
    return dict(config, mode=value["mode"], name=name.strip())


def stored_config(row):
    raw = (row or {}).get("background_config_json")
    if raw:
        # A malformed saved snapshot must fail rather than silently lose items.
        return validate_config(json.loads(raw))
    return {"items": [], "note": (row or {}).get("supplementary_info") or ""}


def render_background(config):
    labels = {"user": "用户设定", "character": "角色设定", "saved": "已保存背景"}
    parts = [f"【{labels[item['kind']]}：{item['label']}】\n{item['content']}" for item in config["items"]]
    if config["note"]:
        parts.append(config["note"])
    return "\n\n".join(parts)


def prepare_background(row, supplementary_info, request_config):
    prior = stored_config(row)
    if request_config is None:
        # Old clients retain the previous text-field behavior.
        text = (supplementary_info or "").strip()
        config = {"items": [], "note": text} if text and text != render_background(prior) else prior
        return {"text": render_background(config), "config": config, "persist": True, "name": ""}
    config = validate_config(request_config)
    return {
        "text": render_background(config), "config": config,
        "persist": request_config["mode"] == "save", "name": request_config["name"],
    }


def _read_json(path):
    try:
        result = json.loads(path.read_text(encoding="utf-8-sig"))
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def persona_sources(user_id):
    root = (Path(USERS_ROOT) / str(int(user_id))).resolve()
    sources = []
    try:
        persona = (root / "configs/global_user_persona.md").read_text(encoding="utf-8-sig").strip()
    except OSError:
        persona = ""
    if persona:
        settings = _read_json(root / "configs/user_settings.json")
        name = str(settings.get("current_user_name") or "")
        sources.append({"id": "user", "kind": "user", "label": name, "content": persona})
    for char_id, info in _read_json(root / "configs/characters.json").items():
        if not isinstance(info, dict) or not char_id or any(c in char_id for c in ("/", "\\")) or char_id in {".", ".."}:
            continue
        path = (root / "characters" / char_id / "prompts/1_base_persona.json").resolve()
        if not path.is_relative_to(root):
            continue
        content = _read_json(path).get("system_prompt", "")
        if isinstance(content, str) and content.strip():
            sources.append({"id": f"character:{char_id}", "kind": "character",
                            "label": str(info.get("name") or info.get("remark") or char_id), "content": content})
    return sources


def library_source(row):
    config = validate_config(json.loads(row["snapshot_json"]))
    return {"id": f"saved:{row['id']}", "kind": "saved", "label": row["name"],
            "content": render_background(config)}
