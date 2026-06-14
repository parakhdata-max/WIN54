"""
Lightweight schema migration runner.

Runs SQL files in this directory in filename order, exactly once per database.
Called from app.py after the active DB URL is stored in Streamlit session state.
"""

from __future__ import annotations

import logging
from pathlib import Path

from modules.sql_adapter import get_transaction_connection, close_connection

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent


def _ensure_migrations_table(cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     TEXT PRIMARY KEY,
            applied_at  TIMESTAMPTZ DEFAULT NOW(),
            description TEXT
        )
        """
    )


def _applied_versions(cursor) -> set[str]:
    cursor.execute("SELECT version FROM schema_migrations")
    return {str(row[0]) for row in (cursor.fetchall() or [])}


def run_pending_migrations() -> list[str]:
    """
    Apply pending SQL migrations.

    Returns a list of applied migration versions. Raises on first failure so
    the app does not run against a half-updated schema.
    """
    conn = None
    cursor = None
    applied_now: list[str] = []
    try:
        conn = get_transaction_connection()
        cursor = conn.cursor()
        # Serialize concurrent migration/self-heal runs. Transaction-scoped, so
        # PostgreSQL releases it automatically on commit/rollback.
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", (815411,))
        _ensure_migrations_table(cursor)
        applied = _applied_versions(cursor)

        for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = sql_file.stem
            if version in applied:
                continue
            sql = sql_file.read_text(encoding="utf-8").strip()
            if not sql:
                continue
            log.info("[migration] applying %s", version)
            cursor.execute(sql)
            cursor.execute(
                """
                INSERT INTO schema_migrations(version, description)
                VALUES (%s, %s)
                ON CONFLICT (version) DO NOTHING
                """,
                (version, sql_file.name),
            )
            applied_now.append(version)

        conn.commit()
        if applied_now:
            log.info("[migration] applied: %s", ", ".join(applied_now))
        return applied_now
    except Exception:
        if conn:
            conn.rollback()
        log.exception("[migration] failed")
        raise
    finally:
        if cursor:
            cursor.close()
        if conn:
            close_connection(conn)
