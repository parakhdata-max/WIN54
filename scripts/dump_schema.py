#!/usr/bin/env python3
"""
Dump the live PostgreSQL schema to a text contract file.

Usage:
    .venv\\Scripts\\python.exe scripts\\dump_schema.py > issue_notes\\live_db_schema.txt
    .venv\\Scripts\\python.exe scripts\\dump_schema.py --db test
    .venv\\Scripts\\python.exe scripts\\dump_schema.py --url postgresql://user:pass@host:5432/db
"""
import argparse
import os
import sys


def _load_db_url(which: str, explicit_url: str) -> str:
    if explicit_url:
        return explicit_url
    env = {}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(root, ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, value = line.split("=", 1)
                    env[key.strip()] = value.strip().strip('"').strip("'")
    key = "DATABASE_TEST" if which == "test" else "DATABASE_PROD"
    url = os.environ.get(key) or env.get(key)
    if not url:
        sys.exit(f"ERROR: {key} not found in environment or .env")
    return url


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", choices=["prod", "test"], default="prod")
    parser.add_argument("--url", default="")
    args = parser.parse_args()

    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except ImportError:
        sys.exit("ERROR: psycopg2 is not installed in this interpreter.")

    conn = psycopg2.connect(_load_db_url(args.db, args.url))
    cur = conn.cursor(cursor_factory=RealDictCursor)

    def q(sql, params=None):
        cur.execute(sql, params or [])
        return cur.fetchall()

    tables = [r["table_name"] for r in q("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema='public' AND table_type='BASE TABLE'
        ORDER BY table_name
    """)]

    out = []
    write = out.append
    write("=" * 70)
    write(f"LIVE DB SCHEMA CONTRACT ({args.db}) tables={len(tables)}")
    write("=" * 70)

    for table in tables:
        cols = q("""
            SELECT column_name, data_type, is_nullable, column_default,
                   character_maximum_length
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name=%s
            ORDER BY ordinal_position
        """, [table])
        write(f"\n### TABLE {table} ({len(cols)} columns)")
        for col in cols:
            typ = col["data_type"]
            if col["character_maximum_length"]:
                typ += f"({col['character_maximum_length']})"
            null = "NULL" if col["is_nullable"] == "YES" else "NOT NULL"
            default = f" DEFAULT {col['column_default']}" if col["column_default"] else ""
            write(f"    {col['column_name']:32s} {typ:24s} {null}{default}")

        cons = q("""
            SELECT con.conname AS name,
                   con.contype AS ctype,
                   pg_get_constraintdef(con.oid) AS def
            FROM pg_constraint con
            JOIN pg_class rel ON rel.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = rel.relnamespace
            WHERE n.nspname='public' AND rel.relname=%s
            ORDER BY con.contype, con.conname
        """, [table])
        for con in cons:
            label = {"p": "PK", "u": "UNIQUE", "f": "FK", "c": "CHECK"}.get(con["ctype"], con["ctype"])
            write(f"    -- {label}: {con['name']}: {con['def']}")

        indexes = q("""
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname='public' AND tablename=%s
            ORDER BY indexname
        """, [table])
        for idx in indexes:
            write(f"    -- INDEX {idx['indexname']}: {idx['indexdef']}")

    cur.close()
    conn.close()
    print("\n".join(out))


if __name__ == "__main__":
    main()
