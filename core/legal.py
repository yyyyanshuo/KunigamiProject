"""Versioned legal-document metadata and consent persistence helpers."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


LEGAL_DOCUMENTS = {
    "terms": {
        "title": "用户协议",
        "version": "1.0",
        "effective_date": "2026年8月11日",
        "path": "/terms",
    },
    "privacy": {
        "title": "隐私政策",
        "version": "1.0",
        "effective_date": "2026年8月11日",
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
        return False, "请先阅读并同意用户协议与隐私政策"

    versions = get_current_legal_versions()
    if data.get("terms_version") != versions["terms"]:
        return False, "用户协议版本已更新，请刷新页面后重新确认"
    if data.get("privacy_version") != versions["privacy"]:
        return False, "隐私政策版本已更新，请刷新页面后重新确认"
    return True, ""


def record_current_legal_consents(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    accepted_at: str | None = None,
) -> str:
    timestamp = accepted_at or datetime.now(timezone.utc).isoformat()
    for document_type, version in get_current_legal_versions().items():
        conn.execute(
            """
            INSERT OR IGNORE INTO legal_consents
                (user_id, document_type, document_version, accepted_at)
            VALUES (?, ?, ?, ?)
            """,
            (int(user_id), document_type, version, timestamp),
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
            ORDER BY accepted_at DESC
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
