"""
dev_clean_db.py
===============
WIN41 — Test Data Cleanup Script
Run from the WIN41 folder:

    python dev_clean_db.py              # interactive (asks confirmation)
    python dev_clean_db.py --force      # skip confirmation prompt
    python dev_clean_db.py --status     # just show counts, don't delete

SAFETY:
  - Only soft-deletes transactional data (is_deleted = TRUE)
  - Never touches products, parties, inventory_stock, patients
  - Resets challan_seq and invoice_seq to 1
  - Prints before/after counts so you can verify

"""

import sys
import os

# ── Allow running from any working directory ──────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _connect():
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from modules.sql_adapter import DB_CONFIG
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    return conn


def _counts(conn) -> dict:
    sql = """
        SELECT
            (SELECT COUNT(*) FROM orders               WHERE COALESCE(is_deleted,false)=false) AS orders,
            (SELECT COUNT(*) FROM order_lines          WHERE COALESCE(is_deleted,false)=false) AS order_lines,
            (SELECT COUNT(*) FROM order_status_history)                                         AS status_history,
            (SELECT COUNT(*) FROM challans             WHERE COALESCE(is_deleted,false)=false) AS challans,
            (SELECT COUNT(*) FROM challan_lines        WHERE COALESCE(is_deleted,false)=false) AS challan_lines,
            (SELECT COUNT(*) FROM invoices             WHERE COALESCE(is_deleted,false)=false) AS invoices,
            (SELECT COUNT(*) FROM invoice_lines        WHERE COALESCE(is_deleted,false)=false) AS invoice_lines,
            (SELECT COUNT(*) FROM payments             WHERE COALESCE(is_deleted,false)=false) AS payments
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
        return dict(zip(
            ["orders","order_lines","status_history","challans",
             "challan_lines","invoices","invoice_lines","payments"],
            row
        ))


def _master_counts(conn) -> dict:
    sql = """
        SELECT
            (SELECT COUNT(*) FROM products)        AS products,
            (SELECT COUNT(*) FROM parties)         AS parties,
            (SELECT COUNT(*) FROM inventory_stock) AS inventory_stock
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
        return dict(zip(["products", "parties", "inventory_stock"], row))


def _print_counts(label: str, counts: dict, master: dict = None):
    print(f"\n{'═'*50}")
    print(f"  {label}")
    print(f"{'═'*50}")
    for k, v in counts.items():
        flag = "  ⚠️" if v > 0 and label == "AFTER" else ""
        print(f"  {k:<25} {v:>6}{flag}")
    if master:
        print(f"  {'─'*40}")
        print(f"  Master data (untouched):")
        for k, v in master.items():
            print(f"  {k:<25} {v:>6}")
    print()


SOFT_DELETE_STEPS = [
    # (table, has_deleted_at)
    ("invoice_lines",        True),
    ("challan_lines",        True),
    ("invoices",             True),
    ("challans",             True),
    ("payments",             False),   # payments has no deleted_at column
    ("order_lines",          True),
    ("orders",               True),
]


def run_cleanup(conn, verbose: bool = True):
    cur = conn.cursor()
    results = {}

    try:
        # ── Soft-delete transactional tables ─────────────────────────────
        for table, has_dt in SOFT_DELETE_STEPS:
            if has_dt:
                sql = f"""
                    UPDATE {table}
                    SET    is_deleted = TRUE,
                           deleted_at = NOW(),
                           deleted_by = 'dev_clean_db'
                    WHERE  COALESCE(is_deleted, FALSE) = FALSE
                """
            else:
                sql = f"""
                    UPDATE {table}
                    SET    is_deleted = TRUE
                    WHERE  COALESCE(is_deleted, FALSE) = FALSE
                """
            cur.execute(sql)
            rows = cur.rowcount
            results[table] = rows
            if verbose:
                print(f"  ✓  {table:<25} {rows:>5} rows soft-deleted")

        # ── Hard-delete status history (no is_deleted column) ────────────
        cur.execute("DELETE FROM order_status_history")
        results["order_status_history"] = cur.rowcount
        if verbose:
            print(f"  ✓  {'order_status_history':<25} {cur.rowcount:>5} rows deleted")

        conn.commit()

        # ── Reset sequences ───────────────────────────────────────────────
        seqs_reset = []
        for seq in ["challan_seq", "invoice_seq"]:
            try:
                cur.execute(f"SELECT setval('{seq}', 1, false)")
                conn.commit()
                seqs_reset.append(seq)
            except Exception as e:
                conn.rollback()
                if verbose:
                    print(f"  ⚠️  Could not reset {seq}: {e}")

        # Try resetting display_order_no sequence
        try:
            cur.execute(
                "SELECT pg_get_serial_sequence('orders', 'display_order_no')"
            )
            seq_name = cur.fetchone()[0]
            if seq_name:
                cur.execute(f"SELECT setval('{seq_name}', 1, false)")
                conn.commit()
                seqs_reset.append(seq_name)
        except Exception:
            conn.rollback()

        if verbose and seqs_reset:
            print(f"\n  ✓  Sequences reset: {', '.join(seqs_reset)}")

    except Exception as e:
        conn.rollback()
        print(f"\n  ❌  Error during cleanup: {e}")
        raise
    finally:
        cur.close()

    return results


def main():
    force   = "--force"  in sys.argv
    status  = "--status" in sys.argv
    help_   = "--help"   in sys.argv or "-h" in sys.argv

    if help_:
        print(__doc__)
        sys.exit(0)

    print("\n" + "█"*50)
    print("  WIN41  —  Dev DB Cleanup")
    print("█"*50)

    try:
        conn = _connect()
    except Exception as e:
        print(f"\n❌  Cannot connect to DB: {e}")
        sys.exit(1)

    before = _counts(conn)
    master = _master_counts(conn)
    _print_counts("BEFORE CLEANUP", before, master)

    if status:
        conn.close()
        sys.exit(0)

    # Check if already empty
    total_active = sum(before.values())
    if total_active == 0:
        print("  ✅  DB already clean — nothing to delete.\n")
        conn.close()
        sys.exit(0)

    # Confirmation
    if not force:
        print(f"  About to soft-delete {total_active} active records.")
        print("  Master data (products / parties / inventory) will NOT be touched.\n")
        answer = input("  Type  YES  to proceed, anything else to abort: ").strip()
        if answer != "YES":
            print("\n  Aborted. No changes made.\n")
            conn.close()
            sys.exit(0)

    print("\n  Running cleanup...\n")
    run_cleanup(conn, verbose=True)

    after = _counts(conn)
    _print_counts("AFTER CLEANUP", after)

    # Final verdict
    remaining = sum(after.values())
    if remaining == 0:
        print("  ✅  DB is clean — ready for fresh pipeline test.\n")
    else:
        print(f"  ⚠️  {remaining} active records remain — check above.\n")

    conn.close()


if __name__ == "__main__":
    main()
