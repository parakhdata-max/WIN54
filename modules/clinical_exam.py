# modules/clinical_exam.py
# ==========================================================
# Clinical Examination Module - Complete Implementation
# Version: 1.0 (Production Ready)
# ==========================================================

import streamlit as st
import pandas as pd
from datetime import datetime
from typing import Dict, Optional, List
import uuid

# Import SQL adapter functions
from modules.sql_adapter import run_query, run_write, execute_query


# ==========================================================
# INITIALIZATION
# ==========================================================

def initialize_clinical_state():
    """
    Initialize clinical examination session state
    Call this in retail_punching.py initialize_session_state()
    """
    if "retail_clinical_exam" not in st.session_state:
        st.session_state.retail_clinical_exam = {
            # Visual Acuity
            "va_distance_unaided_r": "6/6",
            "va_distance_unaided_l": "6/6",
            "va_distance_aided_r": "6/6",
            "va_distance_aided_l": "6/6",
            "va_near_r": "N6",
            "va_near_l": "N6",
            
            # Slit Lamp
            "sle_lids": "Normal",
            "sle_conjunctiva": "Normal",
            "sle_cornea": "WNL",
            "sle_ac": "Deep & Quiet",
            "sle_iris": "Normal",
            "sle_lens": "WNL",
            "sle_vitreous": "Clear",
            "sle_fundus": "",
            
            # Orthoptic
            "ortho_cover_test_distance": "Ortho",
            "ortho_cover_test_near": "Ortho",
            "ortho_nystagmus": "Absent",
            "ortho_ocular_motility": "Full EOMS",
            "ortho_convergence": "Normal",
            "ortho_remarks": "",
            
            # Subjective (Phase 2)
            "subj_duochrome_r": "Equal",
            "subj_duochrome_l": "Equal",
            "subj_binocular_balance": "Balanced",
            "subj_worth_4_dot": "Fusion",
            "subj_distance_phoria": "",
            "subj_near_phoria": "",
            
            # Advanced (Phase 3)
            "iop_r": None,
            "iop_l": None,

            # Phase 4 - Notes & Treatment
            "doctor_notes": "",
            "treatment_plan": "",
            "followup_advice": "",
        }
    
    # Doctor mode toggle
    if "clinical_doctor_mode" not in st.session_state:
        st.session_state.clinical_doctor_mode = False
    
    # Clinical exam saved flag
    if "clinical_exam_saved" not in st.session_state:
        st.session_state.clinical_exam_saved = False


# ==========================================================
# MAIN RENDERING FUNCTION
# ==========================================================

def render_clinical_examination():
    """
    Main clinical examination panel
    
    Usage in retail_punching.py:
        # After power entry, before product selection
        from modules.clinical_exam import render_clinical_examination
        render_clinical_examination()
    """
    
    # Initialize if needed
    initialize_clinical_state()
    
    # Main expander
    with st.expander("🩺 Clinical Examination", expanded=False):

        # ── Last examination — shown BEFORE new entry ─────────────────────
        _pid = st.session_state.get("retail_patient_id")
        if _pid:
            try:
                from modules.sql_adapter import run_query as _rq
                _last = _rq("""
                    SELECT visit_date::text AS dt,
                           COALESCE(right_sph::text,'') AS sr, COALESCE(right_cyl::text,'') AS cr,
                           COALESCE(right_axis::text,'') AS ar, COALESCE(right_add::text,'') AS addr,
                           COALESCE(left_sph::text,'') AS sl, COALESCE(left_cyl::text,'') AS cl,
                           COALESCE(left_axis::text,'') AS al, COALESCE(left_add::text,'') AS addl,
                           '' AS var, '' AS val, '' AS notes
                    FROM patient_visits
                    WHERE patient_id = %s
                      AND (right_sph IS NOT NULL OR left_sph IS NOT NULL)
                    ORDER BY visit_date DESC LIMIT 1
                """, (_pid,)) or []
                if _last:
                    v = _last[0]
                    _addr = f" ADD {v['addr']}" if v.get('addr') and v['addr'] not in ('','0.0','0') else ''
                    _addl = f" ADD {v['addl']}" if v.get('addl') and v['addl'] not in ('','0.0','0') else ''
                    st.markdown(
                        f"<div style='background:#eff6ff;border-left:3px solid #3b82f6;"
                        f"padding:8px 12px;border-radius:4px;margin-bottom:10px'>"
                        f"<span style='font-size:11px;font-weight:700;color:#1e40af'>"
                        f"LAST EXAMINATION · {v['dt']}</span><br>"
                        f"<span style='font-size:12px;color:#374151'>"
                        f"<b>R:</b> {v['sr']} / {v['cr']} × {v['ar']}{_addr} &nbsp; VA {v['var'] or '—'}"
                        f" &nbsp;&nbsp; "
                        f"<b>L:</b> {v['sl']} / {v['cl']} × {v['al']}{_addl} &nbsp; VA {v['val'] or '—'}"
                        f"{'<br><i style=color:#64748b>'+v['notes']+'</i>' if v['notes'] else ''}"
                        f"</span></div>",
                        unsafe_allow_html=True
                    )
                else:
                    st.caption("No previous examination on record")
            except Exception:
                pass

        st.markdown("**New examination — this visit:**")
        st.markdown("---")

        # Header with mode toggle
        col_header1, col_header2, col_header3 = st.columns([2, 1, 1])
        
        with col_header1:
            st.markdown("**Record examination findings for this visit**")
            st.caption("Saved per visit · view history in past visits below")
        
        with col_header2:
            if st.session_state.clinical_exam_saved:
                st.success("✅ Findings saved this visit")
        
        with col_header3:
            mode_label = "👨‍⚕️ Doctor mode ON" if st.session_state.clinical_doctor_mode else "🏪 Switch to Doctor mode"
            if st.button(mode_label, key="toggle_clinical_mode", help="Toggle Doctor Mode"):
                st.session_state.clinical_doctor_mode = not st.session_state.clinical_doctor_mode
                st.rerun()
        
        st.markdown("---")
        
        # PHASE 1: Visual Acuity (Always visible)
        render_visual_acuity()
        
        # PHASE 1: Slit Lamp (Collapsed in retail mode)
        if st.session_state.clinical_doctor_mode:
            render_slit_lamp_exam()
        else:
            with st.expander("🔬 Slit Lamp Examination", expanded=False):
                render_slit_lamp_exam()
        
        # PHASE 1: Orthoptic (Collapsed in retail mode)
        if st.session_state.clinical_doctor_mode:
            render_orthoptic_exam()
        else:
            with st.expander("👀 Orthoptic Examination", expanded=False):
                render_orthoptic_exam()
        
        # PHASE 2: Subjective Refraction (Doctor mode only)
        if st.session_state.clinical_doctor_mode:
            with st.expander("🎯 Subjective Refraction", expanded=False):
                render_subjective_refraction()
        
        # PHASE 4: Doctor Notes + Treatment
        render_doctor_notes_treatment()

        # PHASE 4: Clinical Photo Upload
        render_clinical_photo_upload()

        st.markdown("---")
        
        # Save button
        col_save1, col_save2, col_save3 = st.columns([2, 1, 1])
        
        with col_save2:
            if st.button("💾 Save Clinical Exam", key="save_clinical_btn", use_container_width=True):
                if save_clinical_examination():
                    st.session_state.clinical_exam_saved = True
                    st.success("✅ Clinical exam saved!")
                    st.rerun()
                else:
                    st.warning("⚠️ Please select a patient first")
        
        with col_save3:
            if st.button("🔄 Reset", key="reset_clinical_btn", use_container_width=True):
                reset_clinical_exam()
                st.rerun()


# ==========================================================
# VISUAL ACUITY SECTION
# ==========================================================

def render_visual_acuity():
    """Render Visual Acuity section"""
    
    st.markdown("#### 👁️ Visual Acuity")
    
    va_distance_options = ["6/6", "6/9", "6/12", "6/18", "6/24", "6/36", "6/60", "CF", "HM", "PL", "NPL"]
    va_near_options = ["N6", "N8", "N10", "N12", "N18", "N24", "N36"]

    # Quick copy buttons
    vc1, vc2 = st.columns(2)
    with vc1:
        if st.button("➡️ Copy R → L", key="va_copy_r_to_l", use_container_width=True,
                     help="Copy right eye VA to left eye"):
            cx = st.session_state.retail_clinical_exam
            cx["va_distance_unaided_l"] = cx["va_distance_unaided_r"]
            cx["va_distance_aided_l"]   = cx["va_distance_aided_r"]
            cx["va_near_l"]             = cx["va_near_r"]
            st.rerun()
    with vc2:
        if st.button("⬅️ Copy L → R", key="va_copy_l_to_r", use_container_width=True,
                     help="Copy left eye VA to right eye"):
            cx = st.session_state.retail_clinical_exam
            cx["va_distance_unaided_r"] = cx["va_distance_unaided_l"]
            cx["va_distance_aided_r"]   = cx["va_distance_aided_l"]
            cx["va_near_r"]             = cx["va_near_l"]
            st.rerun()

    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("**Right Eye**")
        
        st.session_state.retail_clinical_exam["va_distance_unaided_r"] = st.selectbox(
            "Distance (Unaided)",
            va_distance_options,
            index=va_distance_options.index(st.session_state.retail_clinical_exam["va_distance_unaided_r"]),
            key="clinical_va_dist_unaided_r"
        )
        
        st.session_state.retail_clinical_exam["va_distance_aided_r"] = st.selectbox(
            "Distance (Aided)",
            va_distance_options[:9],  # Exclude PL, NPL for aided
            index=va_distance_options[:9].index(st.session_state.retail_clinical_exam["va_distance_aided_r"]),
            key="clinical_va_dist_aided_r"
        )
        
        st.session_state.retail_clinical_exam["va_near_r"] = st.selectbox(
            "Near Vision",
            va_near_options,
            index=va_near_options.index(st.session_state.retail_clinical_exam["va_near_r"]),
            key="clinical_va_near_r"
        )
    
    with col2:
        st.markdown("**Left Eye**")
        
        st.session_state.retail_clinical_exam["va_distance_unaided_l"] = st.selectbox(
            "Distance (Unaided)",
            va_distance_options,
            index=va_distance_options.index(st.session_state.retail_clinical_exam["va_distance_unaided_l"]),
            key="clinical_va_dist_unaided_l"
        )
        
        st.session_state.retail_clinical_exam["va_distance_aided_l"] = st.selectbox(
            "Distance (Aided)",
            va_distance_options[:9],
            index=va_distance_options[:9].index(st.session_state.retail_clinical_exam["va_distance_aided_l"]),
            key="clinical_va_dist_aided_l"
        )
        
        st.session_state.retail_clinical_exam["va_near_l"] = st.selectbox(
            "Near Vision",
            va_near_options,
            index=va_near_options.index(st.session_state.retail_clinical_exam["va_near_l"]),
            key="clinical_va_near_l"
        )
    
    st.markdown("---")


# ==========================================================
# SLIT LAMP EXAMINATION SECTION
# ==========================================================

def render_slit_lamp_exam():
    """Render Slit Lamp Examination section"""
    
    st.markdown("#### 🔬 Slit Lamp Examination")
    
    # ── Quick fill buttons ─────────────────────────────────────────────────
    qc1, qc2, qc3 = st.columns([1, 1, 2])
    with qc1:
        if st.button("✅ All WNL / Normal", key="sle_wnl_all", use_container_width=True,
                     help="Set all slit lamp findings to Within Normal Limits"):
            defaults = {
                "sle_lids": "Normal", "sle_conjunctiva": "Normal",
                "sle_cornea": "WNL", "sle_ac": "Deep & Quiet",
                "sle_iris": "Normal", "sle_lens": "WNL",
                "sle_vitreous": "Clear", "sle_fundus": "",
            }
            for k, v in defaults.items():
                st.session_state.retail_clinical_exam[k] = v
            st.rerun()
    with qc2:
        if st.button("📋 Clear all", key="sle_clear_all", use_container_width=True,
                     help="Reset all slit lamp findings"):
            for k in ["sle_lids","sle_conjunctiva","sle_cornea","sle_ac",
                      "sle_iris","sle_lens","sle_vitreous","sle_fundus"]:
                st.session_state.retail_clinical_exam[k] = (
                    "Normal" if k in ("sle_lids","sle_conjunctiva","sle_iris")
                    else "WNL" if k in ("sle_cornea","sle_lens")
                    else "Deep & Quiet" if k == "sle_ac"
                    else "Clear" if k == "sle_vitreous"
                    else ""
                )
            st.rerun()

    st.markdown("---")
    col1, col2 = st.columns(2)
    
    with col1:
        st.session_state.retail_clinical_exam["sle_lids"] = st.selectbox(
            "Lids",
            ["Normal", "Blepharitis", "MGD", "Follicles", "Allergic", "Chalazion", "Hordeolum"],
            index=["Normal", "Blepharitis", "MGD", "Follicles", "Allergic", "Chalazion", "Hordeolum"].index(
                st.session_state.retail_clinical_exam["sle_lids"]
            ),
            key="clinical_sle_lids"
        )
        
        st.session_state.retail_clinical_exam["sle_conjunctiva"] = st.selectbox(
            "Conjunctiva",
            ["Normal", "Congested", "Follicular", "Papillary", "Pterygium", "Pinguecula"],
            index=["Normal", "Congested", "Follicular", "Papillary", "Pterygium", "Pinguecula"].index(
                st.session_state.retail_clinical_exam["sle_conjunctiva"]
            ),
            key="clinical_sle_conjunctiva"
        )
        
        st.session_state.retail_clinical_exam["sle_cornea"] = st.selectbox(
            "Cornea",
            ["WNL", "Clear", "Keratitis", "Scar", "Vascularization", "Edema"],
            index=["WNL", "Clear", "Keratitis", "Scar", "Vascularization", "Edema"].index(
                st.session_state.retail_clinical_exam["sle_cornea"]
            ),
            key="clinical_sle_cornea"
        )
        
        st.session_state.retail_clinical_exam["sle_ac"] = st.selectbox(
            "Anterior Chamber",
            ["Deep & Quiet", "Shallow", "Cells", "Flare"],
            index=["Deep & Quiet", "Shallow", "Cells", "Flare"].index(
                st.session_state.retail_clinical_exam["sle_ac"]
            ),
            key="clinical_sle_ac"
        )
    
    with col2:
        st.session_state.retail_clinical_exam["sle_iris"] = st.selectbox(
            "Iris",
            ["Normal", "Atrophy", "NVI", "Synechiae"],
            index=["Normal", "Atrophy", "NVI", "Synechiae"].index(
                st.session_state.retail_clinical_exam["sle_iris"]
            ),
            key="clinical_sle_iris"
        )
        
        st.session_state.retail_clinical_exam["sle_lens"] = st.selectbox(
            "Lens",
            ["WNL", "Clear", "Cataract", "NS", "PSC", "Cortical", "PCIOL", "ACIOL"],
            index=["WNL", "Clear", "Cataract", "NS", "PSC", "Cortical", "PCIOL", "ACIOL"].index(
                st.session_state.retail_clinical_exam["sle_lens"]
            ),
            key="clinical_sle_lens"
        )
        
        st.session_state.retail_clinical_exam["sle_vitreous"] = st.selectbox(
            "Vitreous",
            ["Clear", "Hazy", "Floaters", "Syneresis", "Hemorrhage"],
            index=["Clear", "Hazy", "Floaters", "Syneresis", "Hemorrhage"].index(
                st.session_state.retail_clinical_exam["sle_vitreous"]
            ),
            key="clinical_sle_vitreous"
        )
    
    # Fundus - full width
    st.session_state.retail_clinical_exam["sle_fundus"] = st.text_area(
        "Fundus Examination",
        value=st.session_state.retail_clinical_exam["sle_fundus"],
        placeholder="Disc: ... | Macula: ... | Vessels: ... | Periphery: ...",
        key="clinical_sle_fundus",
        height=80
    )
    
    st.markdown("---")


# ==========================================================
# ORTHOPTIC EXAMINATION SECTION
# ==========================================================

def render_orthoptic_exam():
    """Render Orthoptic Examination section"""
    
    st.markdown("#### 👀 Orthoptic Examination")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.session_state.retail_clinical_exam["ortho_cover_test_distance"] = st.selectbox(
            "Cover Test (Distance)",
            ["Ortho", "Eso", "Exo", "Hyper R", "Hyper L", "Intermittent"],
            index=["Ortho", "Eso", "Exo", "Hyper R", "Hyper L", "Intermittent"].index(
                st.session_state.retail_clinical_exam["ortho_cover_test_distance"]
            ),
            key="clinical_ortho_ct_dist"
        )
        
        st.session_state.retail_clinical_exam["ortho_cover_test_near"] = st.selectbox(
            "Cover Test (Near)",
            ["Ortho", "Eso", "Exo", "Hyper R", "Hyper L", "Intermittent"],
            index=["Ortho", "Eso", "Exo", "Hyper R", "Hyper L", "Intermittent"].index(
                st.session_state.retail_clinical_exam["ortho_cover_test_near"]
            ),
            key="clinical_ortho_ct_near"
        )
        
        st.session_state.retail_clinical_exam["ortho_nystagmus"] = st.selectbox(
            "Nystagmus",
            ["Absent", "Present - Horizontal", "Present - Vertical", "Present - Rotatory"],
            index=["Absent", "Present - Horizontal", "Present - Vertical", "Present - Rotatory"].index(
                st.session_state.retail_clinical_exam["ortho_nystagmus"]
            ),
            key="clinical_ortho_nystagmus"
        )
    
    with col2:
        st.session_state.retail_clinical_exam["ortho_ocular_motility"] = st.selectbox(
            "Ocular Motility",
            ["Full EOMS", "Restricted", "Palsy - CN III", "Palsy - CN IV", "Palsy - CN VI"],
            index=["Full EOMS", "Restricted", "Palsy - CN III", "Palsy - CN IV", "Palsy - CN VI"].index(
                st.session_state.retail_clinical_exam["ortho_ocular_motility"]
            ),
            key="clinical_ortho_motility"
        )
        
        st.session_state.retail_clinical_exam["ortho_convergence"] = st.selectbox(
            "Convergence",
            ["Normal", "Near Point Recession", "Convergence Insufficiency", "Convergence Excess"],
            index=["Normal", "Near Point Recession", "Convergence Insufficiency", "Convergence Excess"].index(
                st.session_state.retail_clinical_exam["ortho_convergence"]
            ),
            key="clinical_ortho_convergence"
        )
    
    # Remarks - full width
    st.session_state.retail_clinical_exam["ortho_remarks"] = st.text_area(
        "Orthoptic Remarks",
        value=st.session_state.retail_clinical_exam["ortho_remarks"],
        placeholder="Additional observations...",
        key="clinical_ortho_remarks",
        height=60
    )
    
    st.markdown("---")


# ==========================================================
# SUBJECTIVE REFRACTION SECTION (PHASE 2)
# ==========================================================

def render_subjective_refraction():
    """Render Subjective Refraction section (Doctor mode only)"""
    
    st.markdown("#### 🎯 Subjective Refraction")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("**Duochrome Test**")
        st.session_state.retail_clinical_exam["subj_duochrome_r"] = st.selectbox(
            "Right Eye",
            ["Equal", "Red Clearer", "Green Clearer"],
            index=["Equal", "Red Clearer", "Green Clearer"].index(
                st.session_state.retail_clinical_exam["subj_duochrome_r"]
            ),
            key="clinical_subj_duochrome_r"
        )
        
        st.session_state.retail_clinical_exam["subj_duochrome_l"] = st.selectbox(
            "Left Eye",
            ["Equal", "Red Clearer", "Green Clearer"],
            index=["Equal", "Red Clearer", "Green Clearer"].index(
                st.session_state.retail_clinical_exam["subj_duochrome_l"]
            ),
            key="clinical_subj_duochrome_l"
        )
    
    with col2:
        st.markdown("**Binocular Tests**")
        st.session_state.retail_clinical_exam["subj_binocular_balance"] = st.selectbox(
            "Binocular Balance",
            ["Balanced", "R Dominant", "L Dominant"],
            index=["Balanced", "R Dominant", "L Dominant"].index(
                st.session_state.retail_clinical_exam["subj_binocular_balance"]
            ),
            key="clinical_subj_binoc"
        )
        
        st.session_state.retail_clinical_exam["subj_worth_4_dot"] = st.selectbox(
            "Worth 4 Dot",
            ["Fusion", "Diplopia", "Suppression R", "Suppression L", "Alternating"],
            index=["Fusion", "Diplopia", "Suppression R", "Suppression L", "Alternating"].index(
                st.session_state.retail_clinical_exam["subj_worth_4_dot"]
            ),
            key="clinical_subj_w4d"
        )
    
    # Phoria - full width
    col_phoria1, col_phoria2 = st.columns(2)
    
    with col_phoria1:
        st.session_state.retail_clinical_exam["subj_distance_phoria"] = st.text_input(
            "Distance Phoria",
            value=st.session_state.retail_clinical_exam["subj_distance_phoria"],
            placeholder="e.g., 2 Exo",
            key="clinical_subj_dist_phoria"
        )
    
    with col_phoria2:
        st.session_state.retail_clinical_exam["subj_near_phoria"] = st.text_input(
            "Near Phoria",
            value=st.session_state.retail_clinical_exam["subj_near_phoria"],
            placeholder="e.g., 4 Exo",
            key="clinical_subj_near_phoria"
        )
    
    st.markdown("---")


# ==========================================================
# SAVE FUNCTIONS
# ==========================================================

def save_clinical_examination() -> bool:
    """
    Save clinical examination to database
    Returns True if saved, False if no patient selected
    """
    
    # Get patient and visit info
    patient_id = st.session_state.get("retail_patient_id")
    visit_id = st.session_state.get("retail_selected_visit_id")
    record_no = st.session_state.get("retail_case_no", "")
    
    if not patient_id:
        return False
    
    # Get clinical data
    clinical = st.session_state.retail_clinical_exam
    
    # Check if any meaningful data is entered
    has_data = any([
        clinical.get("sle_fundus", "").strip(),
        clinical.get("ortho_remarks", "").strip(),
        clinical.get("va_distance_unaided_r") != "6/6",
        clinical.get("va_distance_unaided_l") != "6/6",
        clinical.get("sle_lids") != "Normal",
        clinical.get("sle_cornea") != "WNL",
        clinical.get("doctor_notes", "").strip(),
        clinical.get("treatment_plan", "").strip(),
        clinical.get("followup_advice", "").strip(),
    ])
    
    if not has_data:
        # Nothing meaningful to save
        return True
    
    try:
        # Check if record exists
        check_sql = """
            SELECT id FROM patient_clinicals 
            WHERE patient_id = %(patient_id)s 
            AND COALESCE(visit_id::text, '') = %(visit_id)s
        """
        
        existing = run_query(check_sql, {
            "patient_id": str(patient_id),
            "visit_id": str(visit_id) if visit_id else ""
        })
        
        if existing:
            # UPDATE existing record
            sql = """
                UPDATE patient_clinicals SET
                    va_distance_unaided_r = %(va_distance_unaided_r)s,
                    va_distance_unaided_l = %(va_distance_unaided_l)s,
                    va_distance_aided_r = %(va_distance_aided_r)s,
                    va_distance_aided_l = %(va_distance_aided_l)s,
                    va_near_r = %(va_near_r)s,
                    va_near_l = %(va_near_l)s,
                    sle_lids = %(sle_lids)s,
                    sle_conjunctiva = %(sle_conjunctiva)s,
                    sle_cornea = %(sle_cornea)s,
                    sle_ac = %(sle_ac)s,
                    sle_iris = %(sle_iris)s,
                    sle_lens = %(sle_lens)s,
                    sle_vitreous = %(sle_vitreous)s,
                    sle_fundus = %(sle_fundus)s,
                    ortho_cover_test_distance = %(ortho_cover_test_distance)s,
                    ortho_cover_test_near = %(ortho_cover_test_near)s,
                    ortho_nystagmus = %(ortho_nystagmus)s,
                    ortho_ocular_motility = %(ortho_ocular_motility)s,
                    ortho_convergence = %(ortho_convergence)s,
                    ortho_remarks = %(ortho_remarks)s,
                    subj_duochrome_r = %(subj_duochrome_r)s,
                    subj_duochrome_l = %(subj_duochrome_l)s,
                    subj_binocular_balance = %(subj_binocular_balance)s,
                    subj_worth_4_dot = %(subj_worth_4_dot)s,
                    subj_distance_phoria = %(subj_distance_phoria)s,
                    subj_near_phoria = %(subj_near_phoria)s,
                    doctor_notes = %(doctor_notes)s,
                    treatment_plan = %(treatment_plan)s,
                    followup_advice = %(followup_advice)s,
                    updated_at = NOW(),
                    updated_by = %(updated_by)s
                WHERE patient_id = %(patient_id)s 
                AND COALESCE(visit_id::text, '') = %(visit_id)s
            """
        else:
            # INSERT new record
            sql = """
                INSERT INTO patient_clinicals (
                    patient_id, visit_id, record_no,
                    va_distance_unaided_r, va_distance_unaided_l,
                    va_distance_aided_r, va_distance_aided_l,
                    va_near_r, va_near_l,
                    sle_lids, sle_conjunctiva, sle_cornea, sle_ac,
                    sle_iris, sle_lens, sle_vitreous, sle_fundus,
                    ortho_cover_test_distance, ortho_cover_test_near,
                    ortho_nystagmus, ortho_ocular_motility,
                    ortho_convergence, ortho_remarks,
                    subj_duochrome_r, subj_duochrome_l,
                    subj_binocular_balance, subj_worth_4_dot,
                    subj_distance_phoria, subj_near_phoria,
                    doctor_notes, treatment_plan, followup_advice,
                    created_by, updated_by
                ) VALUES (
                    %(patient_id)s, %(visit_id)s, %(record_no)s,
                    %(va_distance_unaided_r)s, %(va_distance_unaided_l)s,
                    %(va_distance_aided_r)s, %(va_distance_aided_l)s,
                    %(va_near_r)s, %(va_near_l)s,
                    %(sle_lids)s, %(sle_conjunctiva)s, %(sle_cornea)s, %(sle_ac)s,
                    %(sle_iris)s, %(sle_lens)s, %(sle_vitreous)s, %(sle_fundus)s,
                    %(ortho_cover_test_distance)s, %(ortho_cover_test_near)s,
                    %(ortho_nystagmus)s, %(ortho_ocular_motility)s,
                    %(ortho_convergence)s, %(ortho_remarks)s,
                    %(subj_duochrome_r)s, %(subj_duochrome_l)s,
                    %(subj_binocular_balance)s, %(subj_worth_4_dot)s,
                    %(subj_distance_phoria)s, %(subj_near_phoria)s,
                    %(doctor_notes)s, %(treatment_plan)s, %(followup_advice)s,
                    %(updated_by)s, %(updated_by)s
                )
            """
        
        params = {
            "patient_id": str(patient_id),
            "visit_id": str(visit_id) if visit_id else None,
            "record_no": record_no,
            "updated_by": "system",  # TODO: Replace with actual user
            **clinical
        }
        
        run_write(sql, params)

        # Save clinical photo if uploaded
        if st.session_state.get("clinical_uploaded_photo"):
            try:
                import os
                file = st.session_state.clinical_uploaded_photo
                folder = f"clinical_media/{str(patient_id)}/{str(visit_id or 'no_visit')}"
                os.makedirs(folder, exist_ok=True)
                filename = f"{uuid.uuid4()}.jpg"
                filepath = f"{folder}/{filename}"
                with open(filepath, "wb") as img_f:
                    img_f.write(file.getbuffer())
                execute_query(
                    "INSERT INTO clinical_media(patient_id, visit_id, image_path) VALUES (%s, %s, %s)",
                    params=(str(patient_id), str(visit_id) if visit_id else None, filepath)
                )
                st.session_state.clinical_uploaded_photo = None
            except Exception as photo_err:
                st.warning("Photo save failed: " + str(photo_err))

        return True
        
    except Exception as e:
        st.error(f"❌ Error saving clinical exam: {e}")
        import traceback
        st.error(traceback.format_exc())
        return False


def load_clinical_examination(patient_id: str, visit_id: str = None):
    """
    Load existing clinical examination from database
    Call this when visit is selected
    """
    
    # Safety guard: prevent crashes on empty patient_id
    if not patient_id:
        return False
    
    # Ensure clean state before loading
    initialize_clinical_state()
    
    try:
        sql = """
            SELECT * FROM patient_clinicals
            WHERE patient_id = %(patient_id)s
        """
        
        params = {"patient_id": str(patient_id)}
        
        if visit_id:
            sql += " AND visit_id = %(visit_id)s"
            params["visit_id"] = str(visit_id)
        
        sql += " ORDER BY created_at DESC LIMIT 1"
        
        result = run_query(sql, params)
        
        if result and len(result) > 0:
            clinical_data = result[0]
            
            # Update session state with loaded data
            for key in list(st.session_state.retail_clinical_exam.keys()):
                if key in clinical_data and clinical_data[key] is not None:
                    st.session_state.retail_clinical_exam[key] = clinical_data[key]
            
            st.session_state.clinical_exam_saved = True
            return True
        
        return False
        
    except Exception as e:
        st.error(f"❌ Error loading clinical exam: {e}")
        return False


def reset_clinical_exam():
    """Hard reset clinical exam - clears all old data"""
    if "retail_clinical_exam" in st.session_state:
        del st.session_state.retail_clinical_exam
    
    initialize_clinical_state()
    st.session_state.clinical_exam_saved = False


# ==========================================================
# DISPLAY IN PATIENT HISTORY
# ==========================================================

def render_clinical_summary_in_history(patient_id: str, visit_id: str = None):
    """
    Show clinical summary in patient visit history
    Compact card format
    
    Usage in retail_punching.py:
        from modules.clinical_exam import render_clinical_summary_in_history
        render_clinical_summary_in_history(patient_id, visit_id)
    """
    
    try:
        sql = """
            SELECT * FROM patient_clinicals
            WHERE patient_id = %(patient_id)s
        """
        
        params = {"patient_id": str(patient_id)}
        
        if visit_id:
            sql += " AND visit_id = %(visit_id)s"
            params["visit_id"] = str(visit_id)
        
        sql += " ORDER BY created_at DESC LIMIT 1"
        
        result = run_query(sql, params)
        
        if not result or len(result) == 0:
            return
        
        clinical = result[0]
        
        with st.expander("🩺 Clinical Notes", expanded=False):
            col1, col2, col3 = st.columns(3)
            
            with col1:
                st.markdown("**Visual Acuity**")
                st.caption(f"📍 Unaided: {clinical['va_distance_unaided_r']} / {clinical['va_distance_unaided_l']}")
                st.caption(f"👓 Aided: {clinical['va_distance_aided_r']} / {clinical['va_distance_aided_l']}")
                st.caption(f"📖 Near: {clinical['va_near_r']} / {clinical['va_near_l']}")
            
            with col2:
                st.markdown("**Slit Lamp**")
                st.caption(f"Lids: {clinical['sle_lids']}")
                st.caption(f"Cornea: {clinical['sle_cornea']}")
                st.caption(f"Lens: {clinical['sle_lens']}")
            
            with col3:
                st.markdown("**Orthoptic**")
                st.caption(f"Cover Test: {clinical['ortho_cover_test_distance']}")
                st.caption(f"Motility: {clinical['ortho_ocular_motility']}")
                st.caption(f"Convergence: {clinical['ortho_convergence']}")
            
            # Additional details if present
            if clinical.get('sle_fundus'):
                st.markdown("**Fundus Examination:**")
                st.caption(clinical['sle_fundus'])
            
            if clinical.get('ortho_remarks'):
                st.markdown("**Clinical Remarks:**")
                st.caption(clinical['ortho_remarks'])
            
            # Metadata
            st.caption(f"*Recorded: {clinical['created_at'].strftime('%d/%m/%Y %H:%M') if clinical.get('created_at') else 'N/A'}*")
    
    except Exception as e:
        st.error(f"Error loading clinical summary: {e}")


# ==========================================================
# UTILITY FUNCTIONS
# ==========================================================

def get_clinical_summary_text(patient_id: str, visit_id: str = None) -> str:
    """
    Get clinical summary as formatted text (for reports/printing)
    """
    
    try:
        sql = """
            SELECT * FROM patient_clinicals
            WHERE patient_id = %(patient_id)s
        """
        
        params = {"patient_id": str(patient_id)}
        
        if visit_id:
            sql += " AND visit_id = %(visit_id)s"
            params["visit_id"] = str(visit_id)
        
        sql += " ORDER BY created_at DESC LIMIT 1"
        
        result = run_query(sql, params)
        
        if not result or len(result) == 0:
            return "No clinical data recorded"
        
        clinical = result[0]
        
        summary = f"""
CLINICAL EXAMINATION REPORT

Visual Acuity:
  Distance (Unaided): R: {clinical['va_distance_unaided_r']} | L: {clinical['va_distance_unaided_l']}
  Distance (Aided):   R: {clinical['va_distance_aided_r']} | L: {clinical['va_distance_aided_l']}
  Near Vision:        R: {clinical['va_near_r']} | L: {clinical['va_near_l']}

Slit Lamp Examination:
  Lids:          {clinical['sle_lids']}
  Conjunctiva:   {clinical['sle_conjunctiva']}
  Cornea:        {clinical['sle_cornea']}
  AC:            {clinical['sle_ac']}
  Iris:          {clinical['sle_iris']}
  Lens:          {clinical['sle_lens']}
  Vitreous:      {clinical['sle_vitreous']}
  Fundus:        {clinical['sle_fundus'] or 'Not examined'}

Orthoptic Examination:
  Cover Test (Distance): {clinical['ortho_cover_test_distance']}
  Cover Test (Near):     {clinical['ortho_cover_test_near']}
  Nystagmus:             {clinical['ortho_nystagmus']}
  Ocular Motility:       {clinical['ortho_ocular_motility']}
  Convergence:           {clinical['ortho_convergence']}
  Remarks:               {clinical['ortho_remarks'] or 'None'}

Recorded: {clinical['created_at'].strftime('%d/%m/%Y %H:%M') if clinical.get('created_at') else 'N/A'}
"""
        
        return summary
        
    except Exception as e:
        return f"Error generating summary: {e}"


def has_clinical_data(patient_id: str, visit_id: str = None) -> bool:
    """
    Check if clinical data exists for patient/visit
    """
    
    try:
        sql = """
            SELECT COUNT(*) as count FROM patient_clinicals
            WHERE patient_id = %(patient_id)s
        """
        
        params = {"patient_id": str(patient_id)}
        
        if visit_id:
            sql += " AND visit_id = %(visit_id)s"
            params["visit_id"] = str(visit_id)
        
        result = run_query(sql, params)
        
        return result[0]['count'] > 0 if result else False
        
    except:
        return False


# ==========================================================
# DOCTOR NOTES & TREATMENT SECTION (PHASE 4)
# ==========================================================

def render_doctor_notes_treatment():
    """Render Doctor Notes and Treatment Plan section"""

    st.markdown("---")
    st.markdown("#### Doctor Notes & Treatment")

    col1, col2 = st.columns(2)

    with col1:
        st.session_state.retail_clinical_exam["doctor_notes"] = st.text_area(
            "Clinical Observations / Doctor Notes",
            value=st.session_state.retail_clinical_exam.get("doctor_notes", ""),
            placeholder="e.g., Central corneal opacity noted. Patient c/o photophobia...",
            key="clinical_doctor_notes",
            height=120
        )

        st.session_state.retail_clinical_exam["followup_advice"] = st.text_input(
            "Follow-up Advice",
            value=st.session_state.retail_clinical_exam.get("followup_advice", ""),
            placeholder="e.g., Review after 2 weeks / After spectacle dispensing",
            key="clinical_followup_advice"
        )

    with col2:
        st.session_state.retail_clinical_exam["treatment_plan"] = st.text_area(
            "Treatment Plan / Medicines",
            value=st.session_state.retail_clinical_exam.get("treatment_plan", ""),
            placeholder="e.g., Tab. Vitamin A 50000 IU OD x 7 days\nMoxifloxacin 0.5% QID x 5 days",
            key="clinical_treatment_plan",
            height=120
        )


# ==========================================================
# CLINICAL PHOTO UPLOAD SECTION (PHASE 4)
# ==========================================================

PHOTO_TAGS = ["Cornea", "Retina", "Lids", "Anterior Segment", "Fundus", "Trauma", "Other"]


def render_clinical_photo_upload():
    """Render clinical photo upload section"""

    st.markdown("---")

    with st.expander("Clinical Photos (Optional)", expanded=False):

        # Tag selector
        tag = st.selectbox(
            "Photo Category",
            PHOTO_TAGS,
            key="clinical_photo_tag"
        )

        uploaded = st.file_uploader(
            "Upload clinical image (JPG/PNG, max 2MB)",
            type=["jpg", "jpeg", "png"],
            key="clinical_photo_upload"
        )

        if uploaded:
            # Size check
            size_mb = len(uploaded.getbuffer()) / (1024 * 1024)
            if size_mb > 2:
                st.error("File too large. Max 2MB allowed.")
            else:
                col1, col2 = st.columns([1, 1])
                with col1:
                    st.image(uploaded, caption=f"Preview - {tag}", use_column_width=True)
                with col2:
                    st.success("Ready to save with clinical exam")
                    st.caption(f"Category: {tag}")
                    st.caption(f"Size: {size_mb:.2f} MB")
                st.session_state.clinical_uploaded_photo = uploaded
                st.session_state.clinical_photo_tag = tag

        # Show previously saved photos for this patient
        patient_id = st.session_state.get("retail_patient_id")
        if patient_id:
            _render_saved_photos(patient_id)


def _render_saved_photos(patient_id):
    """Show previously saved photos for patient"""
    import os

    try:
        result = run_query(
            "SELECT image_path, notes, created_at FROM clinical_media "
            "WHERE patient_id = %(pid)s ORDER BY created_at DESC LIMIT 10",
            {"pid": str(patient_id)}
        ) or []
    except Exception:
        result = []

    if not result:
        return

    st.markdown("**Previously Saved Photos**")
    cols = st.columns(3)
    for i, row in enumerate(result):
        path = row.get("image_path", "")
        if os.path.exists(path):
            with cols[i % 3]:
                st.image(path, use_column_width=True)
                if row.get("created_at"):
                    try:
                        st.caption(row["created_at"].strftime("%d %b %Y"))
                    except Exception:
                        st.caption(str(row.get("created_at", "")))


# ==========================================================
# CLINICAL TIMELINE (for backoffice history view)
# ==========================================================

def render_clinical_timeline(patient_id: str):
    """
    Renders full clinical visit timeline for a patient.
    Use in backoffice patient history view.

    Usage:
        from modules.clinical_exam import render_clinical_timeline
        render_clinical_timeline(patient_id)
    """
    import os

    try:
        visits = run_query(
            """
            SELECT pc.*, cm.image_path, cm.created_at as photo_date
            FROM patient_clinicals pc
            LEFT JOIN clinical_media cm ON cm.patient_id = pc.patient_id
            WHERE pc.patient_id = %(patient_id)s
            ORDER BY pc.created_at DESC
            """,
            {"patient_id": str(patient_id)}
        )
    except Exception as e:
        st.error("Timeline load error: " + str(e))
        return

    if not visits:
        st.info("No clinical history found.")
        return

    for i, visit in enumerate(visits):
        created = ""
        try:
            created = visit["created_at"].strftime("%d %b %Y %I:%M %p")
        except Exception:
            created = str(visit.get("created_at", ""))

        with st.expander(f"Visit {i+1} — {created}", expanded=(i == 0)):

            col1, col2, col3 = st.columns(3)

            with col1:
                st.markdown("**Visual Acuity**")
                st.caption(f"Unaided: {visit.get('va_distance_unaided_r','—')} / {visit.get('va_distance_unaided_l','—')}")
                st.caption(f"Aided: {visit.get('va_distance_aided_r','—')} / {visit.get('va_distance_aided_l','—')}")
                st.caption(f"Near: {visit.get('va_near_r','—')} / {visit.get('va_near_l','—')}")

            with col2:
                st.markdown("**Slit Lamp**")
                st.caption(f"Cornea: {visit.get('sle_cornea','—')}")
                st.caption(f"Lens: {visit.get('sle_lens','—')}")
                st.caption(f"Lids: {visit.get('sle_lids','—')}")

            with col3:
                st.markdown("**Orthoptic**")
                st.caption(f"Cover Test: {visit.get('ortho_cover_test_distance','—')}")
                st.caption(f"Motility: {visit.get('ortho_ocular_motility','—')}")
                st.caption(f"Convergence: {visit.get('ortho_convergence','—')}")

            if visit.get("doctor_notes"):
                st.markdown("**Doctor Notes**")
                st.info(visit["doctor_notes"])

            if visit.get("treatment_plan"):
                st.markdown("**Treatment Plan**")
                st.success(visit["treatment_plan"])

            if visit.get("followup_advice"):
                st.caption(f"Follow-up: {visit['followup_advice']}")

            # Photo
            photo_path = visit.get("image_path", "")
            if photo_path and os.path.exists(photo_path):
                st.markdown("**Clinical Photo**")
                st.image(photo_path, use_column_width=True)
