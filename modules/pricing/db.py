"""
modules/pricing/db.py
======================
DB connection for pricing engine — delegates to shared sql_adapter pool.
No hardcoded credentials.
"""
from psycopg2.extras import RealDictCursor


def get_conn():
    from modules.sql_adapter import get_connection
    return get_connection()


def fetch_all(query: str, params=None) -> list:
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params or ())
            rows = cur.fetchall()
        return [dict(r) for r in rows] if rows else []
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(f"[pricing.db] fetch_all: {exc}")
        return []
