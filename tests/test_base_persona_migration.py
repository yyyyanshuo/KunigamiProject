import json

from scripts.migrate_base_persona import run_migration


def _prompt_dir(tmp_path, user_id="12", char_id="hero"):
    path = tmp_path / "users" / user_id / "characters" / char_id / "prompts"
    path.mkdir(parents=True)
    return path


def test_dry_run_reports_legacy_fields_without_touching_data(tmp_path):
    prompts = _prompt_dir(tmp_path)
    persona_path = prompts / "1_base_persona.json"
    original = {
        "system_prompt": "[[LOCK]]\n核心\n[[/LOCK]]\n补充",
        "visual_descriptions": {"tags": "black hair", "description": "黑发"},
        "custom_settings": {"reply_style": "默认"},
    }
    persona_path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")

    report = run_migration(tmp_path / "users")

    assert report["mode"] == "dry-run"
    assert report["counts"] == {"would_migrate": 1}
    assert report["nonempty_visual_descriptions"] == 1
    assert json.loads(persona_path.read_text(encoding="utf-8")) == original
    result = report["results"][0]
    assert set(result["removed_fields"]) == {"custom_settings", "visual_descriptions"}


def test_apply_migrates_json_and_preserves_backup(tmp_path):
    prompts = _prompt_dir(tmp_path)
    persona_path = prompts / "1_base_persona.json"
    original = {
        "system_prompt": "人设正文",
        "visual_descriptions": {"tags": "legacy"},
    }
    persona_path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    backup_root = tmp_path / "backups"

    report = run_migration(
        tmp_path / "users", apply=True, backup_root=backup_root
    )

    assert report["counts"] == {"migrated": 1}
    assert json.loads(persona_path.read_text(encoding="utf-8")) == {
        "system_prompt": "人设正文"
    }
    backup = backup_root / "12" / "characters" / "hero" / "prompts" / persona_path.name
    assert json.loads(backup.read_text(encoding="utf-8")) == original
    assert json.loads((backup_root / "_migration_report.json").read_text(encoding="utf-8"))["counts"] == {"migrated": 1}
    second_report = run_migration(tmp_path / "users")
    assert second_report["counts"] == {"unchanged": 1}


def test_apply_converts_markdown_without_deleting_it(tmp_path):
    prompts = _prompt_dir(tmp_path)
    md_path = prompts / "1_base_persona.md"
    md_path.write_text("原样保留的旧人设\n", encoding="utf-8")
    backup_root = tmp_path / "backups"

    report = run_migration(
        tmp_path / "users", apply=True, backup_root=backup_root
    )

    assert report["counts"] == {"migrated": 1}
    assert json.loads((prompts / "1_base_persona.json").read_text(encoding="utf-8")) == {
        "system_prompt": "原样保留的旧人设\n"
    }
    assert md_path.read_text(encoding="utf-8") == "原样保留的旧人设\n"
    backup = backup_root / "12" / "characters" / "hero" / "prompts" / md_path.name
    assert backup.exists()


def test_invalid_lock_fails_without_writing(tmp_path):
    prompts = _prompt_dir(tmp_path)
    persona_path = prompts / "1_base_persona.json"
    original = {
        "system_prompt": "[[LOCK]]\n没有结束",
        "visual_descriptions": {"tags": "legacy"},
    }
    persona_path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")

    report = run_migration(
        tmp_path / "users", apply=True, backup_root=tmp_path / "backups"
    )

    assert report["counts"] == {"failed": 1}
    assert json.loads(persona_path.read_text(encoding="utf-8")) == original
