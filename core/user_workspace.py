"""Generate private defaults for a new account without reading shared configs."""
import json
from pathlib import Path

from core.model_settings import defaults as model_defaults


def initialize_user_workspace(users_root, user_id: int) -> None:
    """Create missing files only; preserve every existing user file verbatim."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("user_id must be a positive integer")

    user_root = Path(users_root) / str(user_id)
    for directory in ("characters", "groups", "configs", "logs"):
        (user_root / directory).mkdir(parents=True, exist_ok=True)

    configs = user_root / "configs"
    json_defaults = {
        "characters.json": {},
        "groups.json": {},
        "user_settings.json": {},
        "read_status.json": {},
        "api_settings.json": model_defaults(),
    }
    files = {
        name: json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        for name, value in json_defaults.items()
    }
    files["global_user_persona.md"] = ""

    for name, content in files.items():
        try:
            # Exclusive creation also preserves existing files during a retry.
            with (configs / name).open("x", encoding="utf-8") as handle:
                handle.write(content)
        except FileExistsError:
            continue
