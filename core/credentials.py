"""Encrypted, per-user storage for third-party API credentials.

The live store reads only encrypted credentials. Standalone migration stores
can inspect legacy fields and remove them on explicit update.
"""

from __future__ import annotations

import json
import os
import tempfile
import hashlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from core.config import USERS_ROOT
from services.character_state import character_state_lock


SUPPORTED_PROVIDERS = frozenset({"gemini", "openrouter", "openai", "anthropic", "xai", "elevenlabs"})
LEGACY_FIELDS = {
    "gemini": "gemini_api_key",
    "openrouter": "openrouter_api_key",
    "elevenlabs": "elevenlabs_api_key",
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "xai": "xai_api_key",
}


class CredentialError(RuntimeError):
    """Base error for credential storage failures."""


class CredentialConfigurationError(CredentialError):
    """Raised when the server-side encryption key is unavailable or invalid."""


class CredentialDecryptError(CredentialError):
    """Raised when stored ciphertext cannot be decrypted by configured keys."""


def _digest(data):
    encoded = json.dumps(data, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()


def _normalize_user_id(user_id: int | str) -> str:
    try:
        value = int(user_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid user id") from exc
    if value <= 0:
        raise ValueError("Invalid user id")
    return str(value)


def _normalize_provider(provider: str) -> str:
    value = str(provider or "").strip().lower()
    if value not in SUPPORTED_PROVIDERS:
        raise ValueError("Unsupported credential provider")
    return value


def _parse_keys(raw: str | None = None) -> list[bytes]:
    source = raw if raw is not None else os.getenv("CREDENTIAL_ENCRYPTION_KEYS", "")
    keys = [part.strip().encode("ascii") for part in source.split(",") if part.strip()]
    if not keys:
        raise CredentialConfigurationError(
            "CREDENTIAL_ENCRYPTION_KEYS is required to store encrypted credentials"
        )
    try:
        for key in keys:
            Fernet(key)
    except (ValueError, TypeError) as exc:
        raise CredentialConfigurationError(
            "CREDENTIAL_ENCRYPTION_KEYS contains an invalid Fernet key"
        ) from exc
    return keys


def validate_encryption_configuration(raw: str | None = None) -> None:
    """Validate that at least one usable Fernet key is configured."""
    _parse_keys(raw)


class CredentialStore:
    def __init__(self, users_root: str | os.PathLike[str] = USERS_ROOT, encryption_keys: str | None = None, *, legacy_reads=True):
        self.users_root = Path(users_root)
        self._explicit_keys = encryption_keys
        self.legacy_reads = legacy_reads

    def _fernet(self) -> MultiFernet:
        return MultiFernet([Fernet(key) for key in _parse_keys(self._explicit_keys)])

    def credential_path(self, user_id: int | str) -> Path:
        uid = _normalize_user_id(user_id)
        return self.users_root / uid / "configs" / "credentials.json"

    def legacy_settings_path(self, user_id: int | str) -> Path:
        uid = _normalize_user_id(user_id)
        return self.users_root / uid / "configs" / "user_settings.json"

    @staticmethod
    def _read_json(path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            with path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
            if not isinstance(value, dict):
                raise CredentialError(f"Invalid credential data format: {path}")
            return value
        except (OSError, json.JSONDecodeError) as exc:
            raise CredentialError(f"Unable to read credential data: {path}") from exc

    @staticmethod
    def _atomic_write_json(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        fd, temporary = tempfile.mkstemp(prefix=".credentials-", suffix=".tmp", dir=path.parent)
        temporary_path = Path(temporary)
        try:
            try:
                os.chmod(temporary_path, 0o600)
            except OSError:
                pass
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
            temporary_path.unlink(missing_ok=True)
            raise

    def _load_store(self, user_id: int | str) -> dict:
        data = self._read_json(self.credential_path(user_id))
        data.setdefault("version", 1)
        if data["version"] != 1:
            raise CredentialError("Unsupported credential store version")
        data.setdefault("providers", {})
        if not isinstance(data["providers"], dict):
            raise CredentialError("Invalid credential store format")
        return data

    def _legacy_value(self, user_id: int | str, provider: str) -> str:
        settings = self._read_json(self.legacy_settings_path(user_id))
        value = settings.get(LEGACY_FIELDS[provider], "")
        return str(value).strip() if value else ""

    @contextmanager
    def locked(self, user_id):
        path = self.credential_path(user_id)
        with character_state_lock(path):
            journal = path.with_name('.model-credential-commit.json')
            if journal.exists():
                self._recover(path, journal)
            yield

    def _recover(self, path, journal):
        # The atomic settings replacement is the commit point. If it did not
        # happen, restore only this account's previous encrypted credential file.
        try:
            token = self._read_json(journal)['sealed']
            record = json.loads(self._fernet().decrypt(token.encode('ascii')))
            if record['uid'] != path.parent.parent.name:
                raise ValueError()
            target = path.with_name('api_settings.json')
            if not target.exists() or _digest(self._read_json(target)) != record['after']:
                if record['before'] is None:
                    path.unlink(missing_ok=True)
                else:
                    self._atomic_write_json(path, record['before'])
        except (InvalidToken, ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise CredentialError("Unable to recover interrupted credential update") from exc
        journal.unlink()

    def _entry(self, secret):
        return {
            'ciphertext': self._fernet().encrypt(secret.encode()).decode('ascii'),
            'last_four': secret[-4:],
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }

    def save_changes(self, user_id, updates, config=None):
        path = self.credential_path(user_id)
        target = path.with_name('api_settings.json')
        with self.locked(user_id):
            if not updates:
                self._atomic_write_json(target, config)
                return
            current = self._load_store(user_id)
            legacy_path = self.legacy_settings_path(user_id)
            settings = self._read_json(legacy_path)
            # When a migration caller still accepts legacy keys, preserve its
            # previous logical value in encrypted form before removing plaintext.
            migrate = False
            cleaned = False
            for provider in updates:
                field = LEGACY_FIELDS[provider]
                if field in settings:
                    old = str(settings.pop(field) or '').strip()
                    cleaned = True
                    if old and self.legacy_reads and not current['providers'].get(provider, {}).get('ciphertext'):
                        current['providers'][provider] = self._entry(old)
                        migrate = True
            if migrate:
                self._atomic_write_json(path, current)
            if cleaned:
                self._atomic_write_json(legacy_path, settings)

            before = self._read_json(path) if path.exists() else None
            for provider, value in updates.items():
                if value is None:
                    current['providers'].pop(provider, None)
                else:
                    current['providers'][provider] = self._entry(value.strip())
            changed = current != (before or {'version': 1, 'providers': {}})
            if not changed:
                if config is not None:
                    self._atomic_write_json(target, config)
                return
            if config is None or (target.exists() and self._read_json(target) == config):
                self._atomic_write_json(path, current)
                return
            record = {'uid': _normalize_user_id(user_id), 'after': _digest(config), 'before': before}
            journal = path.with_name('.model-credential-commit.json')
            token = self._fernet().encrypt(json.dumps(record).encode()).decode('ascii')
            self._atomic_write_json(journal, {'sealed': token})
            try:
                self._atomic_write_json(path, current)
                self._atomic_write_json(target, config)
            except Exception:
                self._recover(path, journal)
                raise
            journal.unlink()

    def get(self, user_id: int | str, provider: str, *, allow_legacy: bool = True) -> str:
        provider = _normalize_provider(provider)
        with self.locked(user_id):
            entry = self._load_store(user_id)["providers"].get(provider)
            if isinstance(entry, dict) and entry.get("ciphertext"):
                try:
                    return self._fernet().decrypt(entry["ciphertext"].encode("ascii")).decode("utf-8")
                except (InvalidToken, ValueError, UnicodeError) as exc:
                    raise CredentialDecryptError(
                        f"Unable to decrypt {provider} credential for user {_normalize_user_id(user_id)}"
                    ) from exc
            return self._legacy_value(user_id, provider) if allow_legacy and self.legacy_reads else ""

    def status(self, user_id: int | str, provider: str) -> dict:
        provider = _normalize_provider(provider)
        with self.locked(user_id):
            entry = self._load_store(user_id)["providers"].get(provider)
            if isinstance(entry, dict) and entry.get("ciphertext"):
                return {
                    "configured": True,
                    "last_four": str(entry.get("last_four") or ""),
                    "storage": "encrypted",
                }
            legacy = self._legacy_value(user_id, provider) if self.legacy_reads else ""
            return {
                "configured": bool(legacy),
                "last_four": legacy[-4:] if legacy else "",
                "storage": "legacy" if legacy else None,
            }

    def statuses(self, user_id: int | str, providers: Iterable[str] = SUPPORTED_PROVIDERS) -> dict:
        with self.locked(user_id):
            return {provider: self.status(user_id, provider) for provider in providers}

    def set(self, user_id: int | str, provider: str, value: str) -> dict:
        provider = _normalize_provider(provider)
        secret = str(value or "").strip()
        if not secret:
            raise ValueError("Credential cannot be empty")
        with self.locked(user_id):
            self.save_changes(user_id, {provider: secret})
            return self.status(user_id, provider)

    def delete(self, user_id: int | str, provider: str) -> None:
        self.save_changes(user_id, {_normalize_provider(provider): None})

    def remove_legacy_value(self, user_id: int | str, provider: str) -> bool:
        provider = _normalize_provider(provider)
        settings_path = self.legacy_settings_path(user_id)
        with self.locked(user_id):
            settings = self._read_json(settings_path)
            field = LEGACY_FIELDS[provider]
            if field not in settings:
                return False
            settings.pop(field, None)
            self._atomic_write_json(settings_path, settings)
            return True


default_store = CredentialStore(legacy_reads=False)


def get_user_credential(user_id: int | str, provider: str, *, allow_legacy: bool = True) -> str:
    return default_store.get(user_id, provider, allow_legacy=allow_legacy)


def get_credential_statuses(user_id: int | str, providers: Iterable[str] = SUPPORTED_PROVIDERS) -> dict:
    return default_store.statuses(user_id, providers)


def set_user_credential(user_id: int | str, provider: str, value: str) -> dict:
    return default_store.set(user_id, provider, value)


def delete_user_credential(user_id: int | str, provider: str) -> None:
    default_store.delete(user_id, provider)
