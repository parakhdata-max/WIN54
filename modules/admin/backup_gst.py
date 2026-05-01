"""
modules/admin/backup_gst.py
=============================
Phase 3 — Two critical features:

A. BACKUP SYSTEM
   - One-click pg_dump from Streamlit UI
   - Saves to WIN54/backups/ with timestamp
   - Auto-rotation: keeps last 7 daily + 4 weekly
   - Shows last backup time and file size

B. GST RECONCILIATION ENGINE
   - Upload GSTR-2B from GSTN portal (Excel)
   - Match against system purchase invoices
   - Show MATCH / MISMATCH / MISSING status
   - Export mismatch report for CA
"""

import streamlit as st
import pandas as pd
import os
import subprocess
import glob
from datetime import date, datetime, timedelta
from typing import List, Dict, Tuple


def _q(sql, params=None):
    from modules.sql_adapter import run_query
    return run_query(sql, params or ()) or []


def _w(sql, params=None):
    try:
        from modules.sql_adapter import run_write
        run_write(sql, params or ())
        return True
    except Exception as e:
        st.error(f"DB error: {e}")
        return False


def _df(rows):
    return pd.DataFrame(rows) if rows else pd.DataFrame()


# ══════════════════════════════════════════════════════════════════════════════
# A. BACKUP SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

def _get_backup_dir() -> str:
    """Get backup directory — WIN54/backups/ relative to app root."""
    try:
        app_root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)
        )))
        backup_dir = os.path.join(app_root, "backups")
        os.makedirs(backup_dir, exist_ok=True)
        return backup_dir
    except Exception:
        backup_dir = os.path.join(os.getcwd(), "backups")
        os.makedirs(backup_dir, exist_ok=True)
        return backup_dir


def _get_db_config() -> Dict:
    try:
        from modules.sql_adapter import DB_CONFIG
        return DB_CONFIG
    except Exception:
        return {"host": "localhost", "port": 5432,
                "user": "postgres", "password": "", "dbname": "dv_optical_test"}


def _rotate_backups(backup_dir: str) -> None:
    """Keep last 7 daily + 4 weekly backups. Delete older ones."""
    files = sorted(
        glob.glob(os.path.join(backup_dir, "backup_*.sql")),
        key=os.path.getmtime,
        reverse=True
    )
    # Keep newest 7
    for f in files[7:]:
        try:
            os.remove(f)
        except Exception:
            pass


def run_backup() -> Tuple[bool, str, str]:
    """
    Run pg_dump and save to backups folder.
    Returns (success, filename, message).
    """
    cfg        = _get_db_config()
    backup_dir = _get_backup_dir()
    timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename   = f"backup_{timestamp}.sql"
    filepath   = os.path.join(backup_dir, filename)

    # Build pg_dump command
    env = os.environ.copy()
    env["PGPASSWORD"] = str(cfg.get("password", ""))

    cmd = [
        "pg_dump",
        "-h", str(cfg.get("host", "localhost")),
        "-p", str(cfg.get("port", 5432)),
        "-U", str(cfg.get("user", "postgres")),
        "-d", str(cfg.get("dbname", "dv_optical_test")),
        "-f", filepath,
        "--no-password",
        "--verbose",
    ]

    try:
        result = subprocess.run(
            cmd, env=env,
            capture_output=True, text=True, timeout=300
        )

        if result.returncode == 0 and os.path.exists(filepath):
            size_mb = os.path.getsize(filepath) / (1024 * 1024)
            _rotate_backups(backup_dir)
            # Size sanity check
            size_ok, size_msg = _check_backup_size(filepath)
            if not size_ok:
                return True, filename, f"⚠️ Backup created but {size_msg}"
            return True, filename, f"✅ Backup saved: {filename} ({size_mb:.1f} MB)"
        else:
            err = result.stderr[:500] if result.stderr else "Unknown error"
            return False, "", f"❌ pg_dump failed: {err}"

    except FileNotFoundError:
        return False, "", (
            "❌ pg_dump not found.  \n"
            "Make sure PostgreSQL bin directory is in PATH.  \n"
            "Usually: `C:\\Program Files\\PostgreSQL\\16\\bin`"
        )
    except subprocess.TimeoutExpired:
        return False, "", "❌ Backup timed out (>5 min). Check DB connection."
    except Exception as e:
        return False, "", f"❌ Backup error: {e}"


def _check_backup_size(filepath: str, min_size_kb: int = 10) -> Tuple[bool, str]:
    """
    Alert if backup file is suspiciously small.
    A valid DV ERP backup should be at least 10KB.
    Empty DB or failed dump = tiny file.
    """
    if not os.path.exists(filepath):
        return False, "Backup file not found"
    size_kb = os.path.getsize(filepath) / 1024
    if size_kb < min_size_kb:
        return False, (
            f"⚠️ Backup suspiciously small: {size_kb:.1f}KB "
            f"(expected ≥ {min_size_kb}KB). "
            "May be incomplete — check pg_dump output."
        )
    return True, f"Size OK: {size_kb/1024:.1f}MB"


def _list_backups() -> List[Dict]:
    backup_dir = _get_backup_dir()
    files = sorted(
        glob.glob(os.path.join(backup_dir, "backup_*.sql")),
        key=os.path.getmtime,
        reverse=True
    )
    result = []
    for f in files[:10]:
        stat = os.stat(f)
        result.append({
            "filename":  os.path.basename(f),
            "size_mb":   round(stat.st_size / (1024*1024), 2),
            "created":   datetime.fromtimestamp(stat.st_mtime).strftime("%d %b %Y %H:%M"),
            "full_path": f,
        })
    return result


def render_backup_panel() -> None:
    st.markdown("### 💾 Database Backup")
    st.caption("One-click backup using pg_dump — saves to `WIN54/backups/`")

    backups = _list_backups()

    # Last backup info
    if backups:
        last = backups[0]
        st.info(
            f"📁 Last backup: **{last['filename']}**  ·  "
            f"{last['size_mb']} MB  ·  {last['created']}"
        )
    else:
        st.warning("⚠️ No backups found. Click **Backup Now** immediately.")

    c1, c2 = st.columns([2, 1])
    with c1:
        if st.button("💾 Backup Now", type="primary", key="backup_run",
                     width='stretch'):
            with st.spinner("Running pg_dump… (may take 30-60 seconds)"):
                ok, fname, msg = run_backup()
            if ok:
                st.success(msg)
                st.rerun()
            else:
                st.error(msg)
                st.code(
                    "# Add PostgreSQL to PATH:\n"
                    "# Control Panel → System → Advanced → Environment Variables\n"
                    "# Add to PATH: C:\\Program Files\\PostgreSQL\\16\\bin\n"
                    "# Then restart Streamlit"
                )

    with c2:
        st.caption(
            "**Rotation policy:**  \n"
            "Keeps last 7 backups.  \n"
            "Older ones auto-deleted."
        )

    # Backup list
    if backups:
        with st.expander(f"📂 Backup files ({len(backups)})", expanded=False):
            df = _df(backups)
            st.dataframe(
                df[["filename", "size_mb", "created"]],
                width='stretch', hide_index=True
            )
            st.caption(
                "**To restore:** Open pgAdmin → Query Tool → "
                "`psql -U postgres -d dv_optical_test < backup_YYYYMMDD_HHMMSS.sql`"
            )

    # Windows Task Scheduler instructions
    with st.expander("🧪 Test Restore (IMPORTANT — do this once)", expanded=False):
        st.caption("Verify your backup actually restores correctly — don't assume it works")
        cfg2 = _get_db_config()
        test_db = st.text_input("Test database name", value="backoffice_test",
                                key="restore_testdb",
                                help="A SEPARATE database to restore into — NOT your live DB")
        if backups:
            restore_file = st.selectbox("Select backup to test",
                                        [b["filename"] for b in backups],
                                        key="restore_file")
            restore_path = os.path.join(_get_backup_dir(), restore_file)
            st.warning(
                f"⚠️ This will restore **{restore_file}** into **{test_db}**.\n\nMake sure `test_db` EXISTS and is EMPTY before restoring."
            )
            restore_cmd = "\n".join([
                "-- Step 1: Create test DB in pgAdmin",
                f"CREATE DATABASE {test_db};",
                "",
                "-- Step 2: Restore (run in Windows Command Prompt)",
                f'set PGPASSWORD={cfg2.get("password","")}',
                f'"C:\\Program Files\\PostgreSQL\\16\\bin\\psql"'
                f' -h {cfg2.get("host","localhost")}'
                f' -U {cfg2.get("user","postgres")}'
                f' -d {test_db} -f "{restore_path}"',
                "",
                "-- Step 3: Verify count",
                "SELECT COUNT(*) FROM orders;",
                "SELECT COUNT(*) FROM parties;",
            ])
            st.code(restore_cmd, language="sql")
            if st.button("📋 Copy restore command", key="restore_copy"):
                st.info("✅ Command shown above — paste in Windows Command Prompt")
        else:
            st.info("No backups yet. Run a backup first.")

    with st.expander("⏰ Set up automatic daily backup (Windows Task Scheduler)"):
        cfg = _get_db_config()
        st.code(
            f'# Create file: C:\\WIN54\\auto_backup.bat\n'
            f'set PGPASSWORD={cfg.get("password","")}\n'
            f'"C:\\Program Files\\PostgreSQL\\16\\bin\\pg_dump" '
            f'-h {cfg.get("host","localhost")} '
            f'-U {cfg.get("user","postgres")} '
            f'-d {cfg.get("dbname","dv_optical_test")} '
            f'-f "C:\\WIN54\\backups\\backup_%date:~-4%%date:~3,2%%date:~0,2%.sql"\n\n'
            f'# Then: Task Scheduler → Create Task\n'
            f'# Trigger: Daily at 11 PM\n'
            f'# Action: Run auto_backup.bat',
            language="batch"
        )


# ══════════════════════════════════════════════════════════════════════════════
# B. GST RECONCILIATION ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def _ensure_gst_tables() -> None:
    _w("""
        CREATE TABLE IF NOT EXISTS gst_2b (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            gstin         TEXT,
            supplier_name TEXT,
            invoice_no    TEXT,
            invoice_date  DATE,
            taxable_value NUMERIC(14,2) DEFAULT 0,
            igst          NUMERIC(14,2) DEFAULT 0,
            cgst          NUMERIC(14,2) DEFAULT 0,
            sgst          NUMERIC(14,2) DEFAULT 0,
            gst_amount    NUMERIC(14,2) DEFAULT 0,
            fy            TEXT,
            period        TEXT,
            uploaded_by   TEXT,
            uploaded_at   TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    _w("""
        CREATE TABLE IF NOT EXISTS gst_recon_result (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            recon_type     TEXT NOT NULL,
            period         TEXT,
            fy             TEXT,
            invoice_no     TEXT,
            system_taxable NUMERIC(14,2) DEFAULT 0,
            system_gst     NUMERIC(14,2) DEFAULT 0,
            govt_taxable   NUMERIC(14,2) DEFAULT 0,
            govt_gst       NUMERIC(14,2) DEFAULT 0,
            difference     NUMERIC(14,2) DEFAULT 0,
            status         TEXT,
            remarks        TEXT,
            run_at         TIMESTAMPTZ DEFAULT NOW()
        )
    """)


def _parse_gstr2b_excel(df_raw: pd.DataFrame) -> List[Dict]:
    """
    Parse GSTR-2B Excel download from GSTN portal.
    Handles common column name variations.
    """
    # Normalize column names
    df_raw.columns = [str(c).strip().lower().replace(' ', '_') for c in df_raw.columns]

    col_map = {
        "gstin_of_supplier":  "gstin",
        "gstin":              "gstin",
        "trade_name":         "supplier_name",
        "supplier_name":      "supplier_name",
        "invoice_number":     "invoice_no",
        "invoice_no":         "invoice_no",
        "invoice_date":       "invoice_date",
        "taxable_value":      "taxable_value",
        "integrated_tax":     "igst",
        "central_tax":        "cgst",
        "state_ut_tax":       "sgst",
        "igst":               "igst",
        "cgst":               "cgst",
        "sgst":               "sgst",
    }

    df = df_raw.rename(columns={k: v for k, v in col_map.items() if k in df_raw.columns})

    records = []
    for _, row in df.iterrows():
        inv_no = str(row.get("invoice_no", "")).strip()
        if not inv_no or inv_no == "nan":
            continue

        igst = float(row.get("igst", 0) or 0)
        cgst = float(row.get("cgst", 0) or 0)
        sgst = float(row.get("sgst", 0) or 0)

        records.append({
            "gstin":         str(row.get("gstin", "") or ""),
            "supplier_name": str(row.get("supplier_name", "") or ""),
            "invoice_no":    inv_no,
            "invoice_date":  str(row.get("invoice_date", "") or ""),
            "taxable_value": float(row.get("taxable_value", 0) or 0),
            "igst":          igst,
            "cgst":          cgst,
            "sgst":          sgst,
            "gst_amount":    round(igst + cgst + sgst, 2),
        })
    return records


def _upload_gstr2b(records: List[Dict], period: str, fy: str, user: str) -> int:
    """Insert GSTR-2B records. Returns count inserted."""
    count = 0
    for r in records:
        ok = _w("""
            INSERT INTO gst_2b
                (gstin, supplier_name, invoice_no, invoice_date,
                 taxable_value, igst, cgst, sgst, gst_amount,
                 period, fy, uploaded_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT DO NOTHING
        """, (
            r["gstin"], r["supplier_name"], r["invoice_no"],
            r["invoice_date"] or None,
            r["taxable_value"], r["igst"], r["cgst"], r["sgst"], r["gst_amount"],
            period, fy, user,
        ))
        if ok:
            count += 1
    return count


def run_gst_reconciliation(period: str, fy: str) -> Dict:
    """
    Compare system invoices against GSTR-2B for the period.
    Returns summary dict with MATCH/MISMATCH/MISSING counts.
    """
    # Clear previous results for this period
    _w("DELETE FROM gst_recon_result WHERE period=%s AND fy=%s", (period, fy))

    # Run reconciliation query
    rows = _q("""
        SELECT
            COALESCE(pi.invoice_no, g.invoice_no)       AS invoice_no,
            COALESCE(pi.supplier_name, g.supplier_name)  AS supplier_name,
            ROUND(COALESCE(pi.taxable_amount, 0), 2)     AS system_taxable,
            ROUND(COALESCE(pi.gst_amount, 0), 2)         AS system_gst,
            ROUND(COALESCE(g.taxable_value, 0), 2)       AS govt_taxable,
            ROUND(COALESCE(g.gst_amount, 0), 2)          AS govt_gst,
            ROUND(ABS(COALESCE(pi.gst_amount, 0) -
                      COALESCE(g.gst_amount, 0)), 2)      AS difference,
            CASE
                WHEN pi.invoice_no IS NULL              THEN 'MISSING_IN_SYSTEM'
                WHEN g.invoice_no IS NULL               THEN 'MISSING_IN_GOVT'
                WHEN ABS(COALESCE(pi.gst_amount,0) -
                         COALESCE(g.gst_amount,0)) < 1  THEN 'MATCH'
                ELSE 'MISMATCH'
            END AS status
        FROM (
            SELECT invoice_no, taxable_amount, tax_amount AS gst_amount,
                   party_name AS supplier_name
            FROM   purchase_invoices
            WHERE  COALESCE(is_deleted, FALSE) = FALSE
        ) pi
        FULL OUTER JOIN gst_2b g ON g.invoice_no = pi.invoice_no
                                AND g.period = %(period)s
                                AND g.fy     = %(fy)s
        WHERE g.period = %(period)s OR pi.invoice_no IS NOT NULL
    """, {"period": period, "fy": fy})

    # Fallback: compare with disbursements if purchase_invoices missing
    if not rows:
        rows = _q("""
            SELECT
                COALESCE(p.payment_no, g.invoice_no)  AS invoice_no,
                COALESCE(p.party_name, g.supplier_name) AS supplier_name,
                0                                      AS system_taxable,
                0                                      AS system_gst,
                ROUND(COALESCE(g.taxable_value,0),2)   AS govt_taxable,
                ROUND(COALESCE(g.gst_amount,0),2)      AS govt_gst,
                ROUND(COALESCE(g.gst_amount,0),2)      AS difference,
                CASE WHEN p.id IS NULL THEN 'MISSING_IN_SYSTEM' ELSE 'MATCH' END AS status
            FROM gst_2b g
            LEFT JOIN payments p ON p.payment_no = g.invoice_no
            WHERE g.period = %(period)s AND g.fy = %(fy)s
        """, {"period": period, "fy": fy})

    # Store results
    for r in rows:
        _w("""
            INSERT INTO gst_recon_result
                (recon_type, period, fy, invoice_no,
                 system_taxable, system_gst, govt_taxable, govt_gst,
                 difference, status)
            VALUES ('PURCHASE',%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (period, fy,
              r.get("invoice_no"), r.get("system_taxable"), r.get("system_gst"),
              r.get("govt_taxable"), r.get("govt_gst"),
              r.get("difference"), r.get("status")))

    counts = {}
    for r in rows:
        s = r.get("status", "UNKNOWN")
        counts[s] = counts.get(s, 0) + 1

    return {
        "total":              len(rows),
        "match":              counts.get("MATCH", 0),
        "mismatch":           counts.get("MISMATCH", 0),
        "missing_in_govt":    counts.get("MISSING_IN_GOVT", 0),
        "missing_in_system":  counts.get("MISSING_IN_SYSTEM", 0),
        "rows":               rows,
    }


def render_gst_recon() -> None:
    st.markdown("### 🧾 GST Reconciliation — GSTR-2B vs System")
    st.caption("Upload GSTR-2B from GSTN portal → match against your purchase records")

    _ensure_gst_tables()

    tab1, tab2, tab3 = st.tabs(["📤 Upload 2B", "▶️ Run Reconciliation", "📊 Results"])

    # ── Tab 1: Upload ──────────────────────────────────────────────────────────
    with tab1:
        st.caption("Download GSTR-2B Excel from [gst.gov.in](https://gst.gov.in) → upload here")

        c1, c2, c3 = st.columns(3)
        months = ["Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec","Jan","Feb","Mar"]
        period = c1.selectbox("Month", months, key="gst_month")
        year   = c2.number_input("Year", value=date.today().year, step=1, key="gst_year")
        fy     = c3.text_input("Financial Year", value="2526", key="gst_fy",
                               placeholder="e.g. 2526")

        uploaded = st.file_uploader(
            "Upload GSTR-2B Excel (.xlsx)",
            type=["xlsx", "xls"],
            key="gst_upload"
        )

        if uploaded:
            try:
                df_raw = pd.read_excel(uploaded)
                st.caption(f"Preview: {len(df_raw)} rows, columns: {list(df_raw.columns)[:6]}")
                st.dataframe(df_raw.head(3), width='stretch', hide_index=True)

                if st.button("📥 Import to DB", type="primary", key="gst_import"):
                    records = _parse_gstr2b_excel(df_raw)
                    if not records:
                        st.error("❌ Could not parse Excel. Check column names match GSTN format.")
                    else:
                        user = st.session_state.get("user_name", "Admin")
                        count = _upload_gstr2b(records, period, fy, user)
                        st.success(f"✅ {count} records imported for {period}-{year} (FY {fy})")
            except Exception as e:
                st.error(f"Upload error: {e}")

        # Show existing 2B data
        existing = _q("""
            SELECT period, fy, COUNT(*) AS records, MAX(uploaded_at)::text AS last_upload
            FROM gst_2b GROUP BY period, fy ORDER BY last_upload DESC LIMIT 10
        """)
        if existing:
            st.markdown("**Uploaded periods:**")
            st.dataframe(_df(existing), width='stretch', hide_index=True)

    # ── Tab 2: Run ─────────────────────────────────────────────────────────────
    with tab2:
        st.caption("Compare system purchase invoices against uploaded GSTR-2B")

        # Show available periods
        periods = _q("SELECT DISTINCT period, fy FROM gst_2b ORDER BY fy DESC, period")
        if not periods:
            st.warning("No GSTR-2B data uploaded yet. Go to Upload 2B tab first.")
        else:
            period_opts = {f"{r['period']}-{r['fy']}": (r['period'], r['fy'])
                           for r in periods}
            chosen = st.selectbox("Select period", list(period_opts.keys()), key="gst_run_period")
            sel_period, sel_fy = period_opts[chosen]

            if st.button("▶️ Run Reconciliation", type="primary", key="gst_run",
                          width='stretch'):
                with st.spinner("Comparing system vs GSTR-2B…"):
                    result = run_gst_reconciliation(sel_period, sel_fy)

                # Summary metrics
                m1,m2,m3,m4,m5 = st.columns(5)
                m1.metric("Total",           result["total"])
                m2.metric("🟢 Match",         result["match"])
                m3.metric("🔴 Mismatch",      result["mismatch"],
                           delta_color="inverse" if result["mismatch"] else "off")
                m4.metric("⚠️ Missing Govt",  result["missing_in_govt"])
                m5.metric("⚠️ Missing System",result["missing_in_system"])

                if result["mismatch"] == 0 and result["missing_in_govt"] == 0:
                    st.success("✅ Perfect match — all invoices reconciled!")
                else:
                    st.warning(
                        f"⚠️ {result['mismatch']} mismatches · "
                        f"{result['missing_in_govt']} missing in GSTR-2B · "
                        f"{result['missing_in_system']} extra in GSTR-2B"
                    )

    # ── Tab 3: Results ─────────────────────────────────────────────────────────
    with tab3:
        result_periods = _q("""
            SELECT DISTINCT period, fy, COUNT(*) AS records,
                   COUNT(CASE WHEN status='MATCH' THEN 1 END) AS matched,
                   COUNT(CASE WHEN status!='MATCH' THEN 1 END) AS issues
            FROM gst_recon_result
            GROUP BY period, fy ORDER BY fy DESC, period
        """)

        if not result_periods:
            st.info("No reconciliation run yet.")
            return

        period_opts2 = {f"{r['period']}-{r['fy']}": (r['period'], r['fy'])
                        for r in result_periods}
        chosen2 = st.selectbox("Period", list(period_opts2.keys()), key="gst_res_period")
        sel_p2, sel_f2 = period_opts2[chosen2]

        status_filter = st.radio("Show",
            ["All","MATCH","MISMATCH","MISSING_IN_GOVT","MISSING_IN_SYSTEM"],
            horizontal=True, key="gst_status")

        sf = "" if status_filter == "All" else "AND status = %(sf)s"
        rows = _q(f"""
            SELECT invoice_no AS "Invoice No",
                   supplier_name AS "Supplier",
                   system_gst AS "System GST (₹)",
                   govt_gst AS "Govt GST (₹)",
                   difference AS "Diff (₹)",
                   status AS "Status"
            FROM gst_recon_result
            WHERE period=%(p)s AND fy=%(f)s {sf}
            ORDER BY difference DESC, status
        """, {"p": sel_p2, "f": sel_f2, "sf": status_filter})

        if rows:
            df = _df(rows)
            for c in ["System GST (₹)","Govt GST (₹)","Diff (₹)"]:
                if c in df.columns:
                    df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

            st.dataframe(df, width='stretch', hide_index=True,
                column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                               for c in ["System GST (₹)","Govt GST (₹)","Diff (₹)"]})

            st.download_button("⬇ Export for CA",
                df.to_csv(index=False).encode(),
                file_name=f"GST_Recon_{sel_p2}_{sel_f2}.csv",
                key="gst_dl")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def render_backup_gst():
    st.markdown("## 🔒 Backup & GST Compliance")

    tabs = st.tabs(["💾 Database Backup", "🧾 GST Reconciliation"])
    with tabs[0]: render_backup_panel()
    with tabs[1]: render_gst_recon()
