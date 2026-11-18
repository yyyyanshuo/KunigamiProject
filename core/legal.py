"""Versioned legal-document metadata and consent persistence helpers."""

from __future__ import annotations

import sqlite3
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone


LEGAL_DOCUMENTS = {
    "terms": {
        "title": "用户协议",
        "version": "2.0-review.1",
        "effective_date": None,
        "updated_date": "2026-09-23",
        "status": "review",
        "path": "/terms",
    },
    "privacy": {
        "title": "隐私政策",
        "version": "2.0-review.1",
        "effective_date": None,
        "updated_date": "2026-09-23",
        "status": "review",
        "path": "/privacy",
    },
}


def init_legal_consents_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS legal_consents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            document_type TEXT NOT NULL,
            document_version TEXT NOT NULL,
            accepted_at TEXT NOT NULL,
            UNIQUE(user_id, document_type, document_version)
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(legal_consents)")}
    for field in ("accepted_language", "document_hash", "document_snapshot"):
        if field not in columns:
            conn.execute(f"ALTER TABLE legal_consents ADD COLUMN {field} TEXT")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_legal_consents_user
        ON legal_consents (user_id, document_type)
        """
    )


def get_legal_documents() -> dict[str, dict[str, str]]:
    return {key: dict(value) for key, value in LEGAL_DOCUMENTS.items()}


def get_legal_document(document_type: str) -> dict[str, str] | None:
    document = LEGAL_DOCUMENTS.get(document_type)
    return dict(document) if document else None


def get_current_legal_versions() -> dict[str, str]:
    return {
        document_type: document["version"]
        for document_type, document in LEGAL_DOCUMENTS.items()
    }


def validate_legal_acceptance(data: dict) -> tuple[bool, str]:
    if data.get("legal_accepted") is not True:
        return False, "请先阅读并确认当前条款与隐私审阅稿"

    versions = get_current_legal_versions()
    if data.get("terms_version") != versions["terms"]:
        return False, "文档版本已更新，请刷新页面后重新阅读确认"
    if data.get("privacy_version") != versions["privacy"]:
        return False, "文档版本已更新，请刷新页面后重新阅读确认"
    return True, ""


def _document_evidence(document_type, language):
    """Snapshot authored legal text and its translations, not private user data.

    Existing consent rows remain untouched: their historical text is unknown.
    This is source evidence, not a claim that we captured a browser screenshot.
    """
    root = Path(__file__).resolve().parents[1]
    source = (root / "templates" / f"{document_type}.html").read_text(encoding="utf-8")
    wrapper = (root / "templates" / "legal_base.html").read_text(encoding="utf-8")
    from core.localization import CATALOG
    payload = {
        "document": LEGAL_DOCUMENTS[document_type], "language": language,
        "source": source, "wrapper": wrapper,
        "translations": {key: value.get(language, key) for key, value in CATALOG.items() if key in source or key in wrapper},
        "branding_source": (root / "core" / "branding.py").read_text(encoding="utf-8"),
    }
    snapshot = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(snapshot.encode("utf-8")).hexdigest(), snapshot


def record_current_legal_consents(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    accepted_at: str | None = None,
) -> str:
    from core.localization import user_language
    init_legal_consents_table(conn)
    language = user_language(user_id)
    timestamp = accepted_at or datetime.now(timezone.utc).isoformat()
    for document_type, version in get_current_legal_versions().items():
        digest, snapshot = _document_evidence(document_type, language)
        conn.execute(
            """
            INSERT OR IGNORE INTO legal_consents
                (user_id, document_type, document_version, accepted_at, accepted_language, document_hash, document_snapshot)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (int(user_id), document_type, version, timestamp, language, digest, snapshot),
        )
    return timestamp


def get_user_legal_status(user_id: int, *, db_path: str | None = None) -> dict:
    if db_path is None:
        from core.config import USERS_DB
        db_path = USERS_DB
    documents = get_legal_documents()
    consent_by_type: dict[str, dict[str, str]] = {}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        init_legal_consents_table(conn)
        rows = conn.execute(
            """
            SELECT document_type, document_version, accepted_at
            FROM legal_consents
            WHERE user_id = ?
            ORDER BY accepted_at DESC, id DESC
            """,
            (int(user_id),),
        ).fetchall()
        conn.commit()
    finally:
        conn.close()

    for row in rows:
        document_type = row["document_type"]
        if document_type not in consent_by_type:
            consent_by_type[document_type] = {
                "version": row["document_version"],
                "accepted_at": row["accepted_at"],
            }

    current = True
    for document_type, document in documents.items():
        consent = consent_by_type.get(document_type)
        document["accepted_version"] = consent["version"] if consent else None
        document["accepted_at"] = consent["accepted_at"] if consent else None
        document["is_current"] = bool(
            consent and consent["version"] == document["version"]
        )
        current = current and document["is_current"]

    return {"current": current, "documents": documents}
