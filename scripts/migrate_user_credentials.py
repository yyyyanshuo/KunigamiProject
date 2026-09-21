#!/usr/bin/env python3
"""Migrate plaintext per-user API keys into the encrypted credential store.

The command is dry-run by default.  Run it on the server only after setting
``CREDENTIAL_ENCRYPTION_KEYS``.  It never prints credential values.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

from core.credentials import (  # noqa: E402
    CredentialStore,
    LEGACY_FIELDS,
    validate_encryption_configuration,
)


def _read_settings(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("settings root must be an object")
    return value


def discover_users(users_root: Path, selected_ids: set[int] | None = None) -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    if not users_root.exists():
        return found
    for child in users_root.iterdir():
        if not child.is_dir() or not child.name.isdigit():
            continue
        user_id = int(child.name)
        if selected_ids and user_id not in selected_ids:
            continue
        settings_path = child / "configs" / "user_settings.json"
        if settings_path.exists():
            found.append((user_id, settings_path))
    return sorted(found, key=lambda item: item[0])


def has_password_hash(users_db: Path, user_id: int) -> bool:
    if not users_db.exists():
        return False
    try:
        conn = sqlite3.connect(users_db)
        try:
            row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (int(user_id),)).fetchone()
        finally:
            conn.close()
        return bool(row and row[0])
    except sqlite3.Error:
        return False


def _backup(settings_path: Path, users_root: Path, backup_root: Path) -> Path:
    relative = settings_path.relative_to(users_root)
    destination = backup_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(destination.parent, 0o700)
    except OSError:
        pass
    shutil.copy2(settings_path, destination)
    try:
        os.chmod(destination, 0o600)
    except OSError:
        pass
    return destination


def _atomic_write_settings(path: Path, data: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def migrate_global_settings(
    store: CredentialStore,
    target_user_id: int,
    settings_path: Path,
    *,
    apply_changes: bool,
    backup_root: Path | None,
    password_hash_ready: bool = False,
) -> dict:
    settings = _read_settings(settings_path)
    candidates = {
        provider: str(settings.get(field) or "").strip()
        for provider, field in LEGACY_FIELDS.items()
        if str(settings.get(field) or "").strip()
    }
    has_legacy_password = bool(settings.get("password"))
    result = {
        "user_id": target_user_id,
        "migrated": [],
        "already_encrypted": [],
        "conflicts": [],
        "password_cleanup": "ready" if has_legacy_password and password_hash_ready else (
            "blocked" if has_legacy_password else "none"
        ),
    }
    for provider, plaintext in candidates.items():
        target_value = store.get(target_user_id, provider, allow_legacy=True)
        if target_value and target_value != plaintext:
            result["conflicts"].append(provider)
        elif target_value:
            result["already_encrypted"].append(provider)
        else:
            result["migrated"].append(provider)
    if not apply_changes or result["conflicts"] or (not candidates and result["password_cleanup"] != "ready"):
        return result
    if backup_root is None:
        raise ValueError("backup_root is required when applying changes")

    destination = backup_root / "_global" / "configs" / "user_settings.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(settings_path, destination)
    try:
        os.chmod(destination, 0o600)
    except OSError:
        pass
    for provider in result["migrated"]:
        store.set(target_user_id, provider, candidates[provider])
    for provider in result["already_encrypted"]:
        # The matching target may still be a per-user legacy value.
        if store.status(target_user_id, provider).get("storage") == "legacy":
            store.set(target_user_id, provider, candidates[provider])
    latest = _read_settings(settings_path)
    for provider in candidates:
        latest.pop(LEGACY_FIELDS[provider], None)
    if result["password_cleanup"] == "ready":
        latest.pop("password", None)
    _atomic_write_settings(settings_path, latest)
    return result


def migrate_user(
    store: CredentialStore,
    user_id: int,
    settings_path: Path,
    *,
    apply_changes: bool,
    users_root: Path,
    backup_root: Path | None,
    password_hash_ready: bool = False,
) -> dict:
    settings = _read_settings(settings_path)
    candidates = {
        provider: str(settings.get(field) or "").strip()
        for provider, field in LEGACY_FIELDS.items()
        if str(settings.get(field) or "").strip()
    }
    has_legacy_password = bool(settings.get("password"))
    result = {
        "user_id": user_id,
        "migrated": [],
        "already_encrypted": [],
        "conflicts": [],
        "password_cleanup": "ready" if has_legacy_password and password_hash_ready else (
            "blocked" if has_legacy_password else "none"
        ),
    }
    if not candidates and result["password_cleanup"] != "ready":
        return result

    existing: dict[str, str] = {}
    for provider, plaintext in candidates.items():
        encrypted_value = store.get(user_id, provider, allow_legacy=False)
        if encrypted_value and encrypted_value != plaintext:
            result["conflicts"].append(provider)
        elif encrypted_value:
            existing[provider] = plaintext
            result["already_encrypted"].append(provider)
        else:
            result["migrated"].append(provider)

    if not apply_changes or result["conflicts"]:
        return result

    if backup_root is None:
        raise ValueError("backup_root is required when applying changes")
    _backup(settings_path, users_root, backup_root)
    for provider in result["migrated"]:
        store.set(user_id, provider, candidates[provider])
    for provider in result["already_encrypted"]:
        store.remove_legacy_value(user_id, provider)
    if result["password_cleanup"] == "ready":
        latest = _read_settings(settings_path)
        latest.pop("password", None)
        _atomic_write_settings(settings_path, latest)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write encrypted credentials and remove plaintext fields")
    parser.add_argument("--user-id", action="append", type=int, help="limit migration to one or more user IDs")
    parser.add_argument("--users-root", type=Path, default=PROJECT_ROOT / "users")
    parser.add_argument("--users-db", type=Path, default=PROJECT_ROOT / "configs" / "users.db")
    parser.add_argument(
        "--global-user-id",
        type=int,
        help="explicit user ID that owns credentials in legacy configs/user_settings.json",
    )
    parser.add_argument(
        "--backup-root",
        type=Path,
        help="plaintext backup directory; defaults to credential-migration-backups/<UTC timestamp>",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    validate_encryption_configuration()
    users_root = args.users_root.resolve()
    users_db = args.users_db.resolve()
    selected = set(args.user_id or []) or None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = (
        args.backup_root.resolve()
        if args.backup_root
        else PROJECT_ROOT / "credential-migration-backups" / timestamp
    )
    store = CredentialStore(users_root=users_root)

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"Credential migration mode: {mode}")
    conflicts = 0
    changed_users = 0
    backup_created = False
    scanned = discover_users(users_root, selected)
    for user_id, settings_path in scanned:
        try:
            result = migrate_user(
                store,
                user_id,
                settings_path,
                apply_changes=args.apply,
                users_root=users_root,
                backup_root=backup_root if args.apply else None,
                password_hash_ready=has_password_hash(users_db, user_id),
            )
        except Exception as exc:
            conflicts += 1
            print(f"user={user_id} status=ERROR reason={type(exc).__name__}")
            continue
        provider_count = len(result["migrated"]) + len(result["already_encrypted"])
        if result["password_cleanup"] == "blocked":
            conflicts += 1
        if provider_count or result["password_cleanup"] == "ready":
            changed_users += 1
            backup_created = backup_created or args.apply
        conflicts += len(result["conflicts"])
        print(
            f"user={user_id} migrate={','.join(result['migrated']) or '-'} "
            f"already={','.join(result['already_encrypted']) or '-'} "
            f"conflict={','.join(result['conflicts']) or '-'} "
            f"legacy_password={result['password_cleanup']}"
        )

    global_settings = PROJECT_ROOT / "configs" / "user_settings.json"
    if global_settings.exists():
        try:
            global_data = _read_settings(global_settings)
            global_fields = [field for field in LEGACY_FIELDS.values() if global_data.get(field)]
            if global_fields or global_data.get("password"):
                if args.global_user_id:
                    global_result = migrate_global_settings(
                        store,
                        args.global_user_id,
                        global_settings,
                        apply_changes=args.apply,
                        backup_root=backup_root if args.apply else None,
                        password_hash_ready=has_password_hash(users_db, args.global_user_id),
                    )
                    conflicts += len(global_result["conflicts"])
                    if global_result["password_cleanup"] == "blocked":
                        conflicts += 1
                    global_count = len(global_result["migrated"]) + len(global_result["already_encrypted"])
                    backup_created = backup_created or bool(
                        args.apply
                        and (global_count or global_result["password_cleanup"] == "ready")
                        and not global_result["conflicts"]
                    )
                    print(
                        f"global->user={args.global_user_id} "
                        f"migrate={','.join(global_result['migrated']) or '-'} "
                        f"already={','.join(global_result['already_encrypted']) or '-'} "
                        f"conflict={','.join(global_result['conflicts']) or '-'} "
                        f"legacy_password={global_result['password_cleanup']}"
                    )
                else:
                    print(
                        "NOTICE: global configs/user_settings.json still contains legacy credentials or password; "
                        "re-run with --global-user-id <owner id> after confirming ownership."
                    )
        except Exception as exc:
            conflicts += 1
            print(f"global status=ERROR reason={type(exc).__name__}")

    print(f"Scanned users: {len(scanned)}; users requiring changes: {changed_users}; conflicts/errors: {conflicts}")
    if args.apply and backup_created:
        print(f"Plaintext backups: {backup_root}")
        print("After verification, securely remove the plaintext backup directory.")
    elif not args.apply:
        print("No files changed. Re-run with --apply after reviewing this report.")
    return 2 if conflicts else 0


if __name__ == "__main__":
    raise SystemExit(main())
