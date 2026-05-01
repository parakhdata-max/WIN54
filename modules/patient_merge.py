"""
Patient Duplicate Merge Tool
Reunites fragmented visit history caused by creating new patient IDs on revisits.
"""
import streamlit as st
import pandas as pd
from modules.sql_adapter import run_query, run_write


def render_patient_merge():
    st.title("🔀 Patient Record Merge")
    st.caption(
        "Patients who visited multiple times but were registered fresh each time "
        "have fragmented history. This tool reunites their complete visit history under one record."
    )

    # ── Tabs ─────────────────────────────────────────────────────────────────
    tab1, tab2, tab3 = st.tabs(["🔍 Find Duplicates", "🔀 Merge History", "✅ Merged Log"])

    # ── TAB 1: Find duplicates ────────────────────────────────────────────────
    with tab1:
        st.subheader("Duplicate Patient Groups")

        dups = run_query("""
            SELECT
                LOWER(TRIM(master_name))              AS name_clean,
                COUNT(*)                              AS total,
                ARRAY_AGG(id::text      ORDER BY created_at ASC) AS ids,
                ARRAY_AGG(master_name   ORDER BY created_at ASC) AS names,
                ARRAY_AGG(COALESCE(mobile,'—') ORDER BY created_at ASC) AS mobiles,
                ARRAY_AGG(COALESCE(record_no,'—') ORDER BY created_at ASC) AS records,
                ARRAY_AGG(COALESCE(barcode,'—') ORDER BY created_at ASC) AS barcodes,
                ARRAY_AGG(created_at   ORDER BY created_at ASC) AS dates
            FROM patients
            WHERE master_name IS NOT NULL
            GROUP BY LOWER(TRIM(master_name))
            HAVING COUNT(*) > 1
            ORDER BY total DESC, name_clean
        """) or []

        if not dups:
            st.success("✅ No duplicate patients found!"); return

        # Summary
        col1, col2, col3 = st.columns(3)
        col1.metric("Duplicate Groups", len(dups))
        col2.metric("Extra Records", sum(int(d['total'])-1 for d in dups))
        col3.metric("Visits to Reunite",
    run_query("""
                SELECT COUNT(*) AS cnt FROM patient_visits
                WHERE patient_id IN (
                    SELECT id FROM (
                        SELECT id, ROW_NUMBER() OVER (
                            PARTITION BY LOWER(TRIM(master_name))
                            ORDER BY created_at ASC
                        ) AS rn
                        FROM patients
                        WHERE master_name IS NOT NULL
                    ) sub WHERE rn > 1
                )
            """)[0]['cnt'] or 0
        )

        search = st.text_input("🔍 Filter by name")
        show_dups = [d for d in dups if not search or search.lower() in str(d['name_clean'])]

        for i, grp in enumerate(show_dups[:100]):
            name_title = str(grp['name_clean']).title()
            total = int(grp['total'])
            ids = grp['ids']; mobiles = grp['mobiles']
            records = grp['records']; dates = grp['dates']

            # Get visit counts per record
            visit_counts = {}
            for pid in ids:
                vc = run_query("SELECT COUNT(*) AS c FROM patient_visits WHERE patient_id=%s::uuid", (pid,))
                visit_counts[pid] = int(vc[0]['c']) if vc else 0

            total_visits = sum(visit_counts.values())

            # Classify: same mobile = definitely same person
            unique_mobiles = set(m for m in mobiles if m != '—')
            if len(unique_mobiles) == 1 and len([m for m in mobiles if m != '—']) == total:
                same_person_flag = "🟢 Same mobile on all records"
            elif len(unique_mobiles) == 1 and len([m for m in mobiles if m != '—']) > 0:
                same_person_flag = "🟡 One has mobile — review"
            elif len(unique_mobiles) > 1:
                same_person_flag = "🟠 Different mobiles — review carefully"
            else:
                same_person_flag = "⚪ No mobile on any record"

            with st.expander(
                f"👤 {name_title}  |  {total} records  |  {total_visits} total visits  |  {same_person_flag}",
                expanded=False
            ):
                # Visit history per record
                st.markdown("**📋 Visit history per record:**")

                tbl_data = []
                for j, pid in enumerate(ids):
                    visits = run_query("""
                        SELECT visit_date, right_sph, right_cyl, left_sph, left_cyl
                        FROM patient_visits WHERE patient_id=%s::uuid
                        ORDER BY visit_date DESC LIMIT 3
                    """, (pid,)) or []
                    last_visit = str(visits[0]['visit_date'])[:10] if visits else "No visits"
                    rx_summary = ""
                    if visits:
                        v = visits[0]
                        def _fmt(val):
                            if val is None: return None
                            try:
                                import math
                                if math.isnan(float(val)): return None
                            except: pass
                            return str(val)
                        rs = _fmt(v.get('right_sph')); rc = _fmt(v.get('right_cyl'))
                        ls = _fmt(v.get('left_sph'));  lc = _fmt(v.get('left_cyl'))
                        r_str = rs if rs else '—'
                        r_str += f"/{rc}" if rc else ''
                        l_str = ls if ls else '—'
                        l_str += f"/{lc}" if lc else ''
                        rx_summary = f"R: {r_str} | L: {l_str}"

                    tbl_data.append({
                        "Record": records[j],
                        "Mobile": mobiles[j],
                        "Registered": str(dates[j])[:10] if dates[j] else "—",
                        "Visits": visit_counts[pid],
                        "Last Visit": last_visit,
                        "Last RX": rx_summary,
                    })

                df = pd.DataFrame(tbl_data)
                st.dataframe(df, use_container_width=True, hide_index=True)

                st.markdown("---")

                # Merge section
                st.markdown("**🔀 Merge Action:**")
                st.caption(
                    "Select the MASTER record to keep. All visit history from other records "
                    "will be moved here. The duplicate records will be deleted."
                )

                master_opts = {
                    f"{records[j]} — {visit_counts[ids[j]]} visits (registered {str(dates[j])[:10] if dates[j] else '?'})": ids[j]
                    for j in range(total)
                }

                sel = st.selectbox("Keep as master:", list(master_opts.keys()), key=f"ms_{i}")
                master_id = master_opts[sel]
                dups_to_merge = [pid for pid in ids if pid != master_id]
                visits_to_move = sum(visit_counts[p] for p in dups_to_merge)

                st.info(
                    f"Will move **{visits_to_move} visit(s)** from {len(dups_to_merge)} duplicate record(s) "
                    f"→ master record **{records[ids.index(master_id)]}**. "
                    f"Duplicate patient IDs will be permanently deleted."
                )

                if st.button(f"🔀 Merge & Reunite History", key=f"mb_{i}", type="primary"):
                    try:
                        for dup_id in dups_to_merge:
                            run_write(
                                "UPDATE patient_visits SET patient_id=%s::uuid WHERE patient_id=%s::uuid",
                                (master_id, dup_id)
                            )
                            run_write("DELETE FROM patients WHERE id=%s::uuid", (dup_id,))

                        st.success(
                            f"✅ Done! {visits_to_move} visits reunited under master record. "
                            f"{len(dups_to_merge)} duplicate ID(s) removed."
                        )
                        st.rerun()
                    except Exception as e:
                        st.error(f"❌ Error: {e}")

    # ── TAB 2: Manual merge by ID ─────────────────────────────────────────────
    with tab2:
        st.subheader("Manual Merge by Record No / Mobile")
        st.caption("Use this when two patients have different names but are the same person.")

        c1, c2 = st.columns(2)
        with c1:
            src = st.text_input("Record No / Mobile to MERGE FROM (will be deleted):", key="manual_src")
        with c2:
            dst = st.text_input("Record No / Mobile to MERGE INTO (master, kept):", key="manual_dst")

        if src and dst:
            src_rows = run_query(
                "SELECT id::text AS pid, master_name, record_no, mobile FROM patients "
                "WHERE record_no=%s OR mobile=%s LIMIT 1", (src, src)
            )
            dst_rows = run_query(
                "SELECT id::text AS pid, master_name, record_no, mobile FROM patients "
                "WHERE record_no=%s OR mobile=%s LIMIT 1", (dst, dst)
            )

            if src_rows and dst_rows:
                sp = src_rows[0]; dp = dst_rows[0]
                st.markdown(f"**FROM:** {sp['master_name']} | {sp['record_no']} | {sp['mobile']}")
                st.markdown(f"**INTO:** {dp['master_name']} | {dp['record_no']} | {dp['mobile']}")

                vc_src = run_query("SELECT COUNT(*) AS c FROM patient_visits WHERE patient_id=%s::uuid", (sp['pid'],))
                st.info(f"{int(vc_src[0]['c']) if vc_src else 0} visit(s) will be moved from FROM → INTO record.")

                if st.button("🔀 Confirm Manual Merge", type="primary", key="manual_merge_btn"):
                    try:
                        run_write("UPDATE patient_visits SET patient_id=%s::uuid WHERE patient_id=%s::uuid",
                                  (dp['pid'], sp['pid']))
                        run_write("DELETE FROM patients WHERE id=%s::uuid", (sp['pid'],))
                        st.success("✅ Merged successfully!")
                    except Exception as e:
                        st.error(f"❌ Error: {e}")
            else:
                if not src_rows: st.warning("FROM patient not found")
                if not dst_rows: st.warning("INTO patient not found")

    # ── TAB 3: Stats after cleanup ────────────────────────────────────────────
    with tab3:
        st.subheader("Post-Merge Status")
        stats = run_query("""
            SELECT
                COUNT(*)                                        AS total_patients,
                COUNT(barcode)                                  AS with_barcode,
                COUNT(*) - COUNT(barcode)                       AS missing_barcode,
                COUNT(DISTINCT LOWER(TRIM(master_name)))        AS unique_names,
                COUNT(*) - COUNT(DISTINCT LOWER(TRIM(master_name))) AS still_duplicate_names
            FROM patients
        """)
        if stats:
            s = stats[0]
            col1,col2,col3,col4 = st.columns(4)
            col1.metric("Total Patients", s['total_patients'])
            col2.metric("With Barcode",   s['with_barcode'])
            col3.metric("Missing Barcode",s['missing_barcode'])
            col4.metric("Still Duplicate Names", s['still_duplicate_names'])

        if st.button("🏷️ Generate Barcodes for All Patients Without One"):
            try:
                run_write("""
                    WITH ranked AS (
                        SELECT id,
                               EXTRACT(YEAR FROM COALESCE(created_at,NOW()))::text AS yr,
                               ROW_NUMBER() OVER (
                                   PARTITION BY EXTRACT(YEAR FROM COALESCE(created_at,NOW()))
                                   ORDER BY created_at ASC NULLS LAST, id ASC
                               ) AS seq
                        FROM patients WHERE barcode IS NULL OR barcode = ''
                    )
                    UPDATE patients p
                    SET barcode = CONCAT('PAT-', r.yr, LPAD(r.seq::text, 5, '0'))
                    FROM ranked r WHERE p.id = r.id
                """)
                st.success("✅ Barcodes generated for all patients!")
                st.rerun()
            except Exception as e:
                st.error(f"❌ Error: {e}")
