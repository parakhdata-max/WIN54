import streamlit as st
import datetime


def render_printable_summary(order_lines, patient_name, mobile, provisional_id):
    clinical = st.session_state.get("retail_clinical_exam") or {}
    rx_r     = st.session_state.get("retail_new_rx_r") or st.session_state.get("retail_old_rx_r") or {}
    rx_l     = st.session_state.get("retail_new_rx_l") or st.session_state.get("retail_old_rx_l") or {}
    old_rx_r = st.session_state.get("retail_old_rx_r") or {}
    old_rx_l = st.session_state.get("retail_old_rx_l") or {}

    st.markdown("---")
    st.markdown("### Provisional Order Summary")
    st.write("Date:", datetime.date.today().strftime("%d %b %Y"))
    st.write("ID:", provisional_id)
    st.write("Patient:", patient_name, "  |  Mobile:", mobile)

    st.markdown("---")
    st.markdown("#### Visual Acuity")
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1: st.write("")
    with c2: st.markdown("**Right Eye**")
    with c3: st.markdown("**Left Eye**")
    for label, key_r, key_l in [
        ("Distance Unaided", "va_distance_unaided_r", "va_distance_unaided_l"),
        ("Distance Aided",   "va_distance_aided_r",   "va_distance_aided_l"),
        ("Near Vision",      "va_near_r",             "va_near_l"),
    ]:
        c1, c2, c3 = st.columns([2, 1, 1])
        with c1: st.write(label)
        with c2: st.write(str(clinical.get(key_r) or "-"))
        with c3: st.write(str(clinical.get(key_l) or "-"))

    st.markdown("---")
    st.markdown("#### Prescription (New Rx)")
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1: st.markdown("**Eye**")
    with c2: st.markdown("**SPH**")
    with c3: st.markdown("**CYL**")
    with c4: st.markdown("**AXIS**")
    with c5: st.markdown("**ADD**")
    for eye_label, rx in [("R", rx_r), ("L", rx_l)]:
        c1, c2, c3, c4, c5 = st.columns(5)
        with c1: st.write(eye_label)
        with c2: st.write(str(rx.get("sph") or "-"))
        with c3: st.write(str(rx.get("cyl") or "-"))
        with c4: st.write(str(rx.get("axis") or "-"))
        with c5: st.write(str(rx.get("add") or "-"))

    has_old_rx = any(v for v in list(old_rx_r.values()) + list(old_rx_l.values()) if v)
    if has_old_rx:
        st.markdown("#### Old Rx")
        c1, c2, c3, c4, c5 = st.columns(5)
        with c1: st.markdown("**Eye**")
        with c2: st.markdown("**SPH**")
        with c3: st.markdown("**CYL**")
        with c4: st.markdown("**AXIS**")
        with c5: st.markdown("**ADD**")
        for eye_label, rx in [("R", old_rx_r), ("L", old_rx_l)]:
            c1, c2, c3, c4, c5 = st.columns(5)
            with c1: st.write(eye_label)
            with c2: st.write(str(rx.get("sph") or "-"))
            with c3: st.write(str(rx.get("cyl") or "-"))
            with c4: st.write(str(rx.get("axis") or "-"))
            with c5: st.write(str(rx.get("add") or "-"))

    st.markdown("---")
    st.markdown("#### Slit Lamp Examination")
    sle_fields = [
        ("Lids",        "sle_lids"),
        ("Conjunctiva", "sle_conjunctiva"),
        ("Cornea",      "sle_cornea"),
        ("AC",          "sle_ac"),
        ("Iris",        "sle_iris"),
        ("Lens",        "sle_lens"),
        ("Vitreous",    "sle_vitreous"),
        ("Fundus",      "sle_fundus"),
    ]
    any_sle = False
    for label, key in sle_fields:
        val = clinical.get(key)
        if val:
            any_sle = True
            c1, c2 = st.columns([1, 2])
            with c1: st.write("**" + label + "**")
            with c2: st.write(str(val))
    iop_r = clinical.get("iop_r")
    iop_l = clinical.get("iop_l")
    if iop_r or iop_l:
        c1, c2 = st.columns([1, 2])
        with c1: st.write("**IOP**")
        with c2: st.write("R: " + str(iop_r or "-") + "  |  L: " + str(iop_l or "-"))
    if not any_sle and not iop_r and not iop_l:
        st.caption("No slit lamp data recorded.")

    st.markdown("---")
    st.markdown("#### Orthoptic Examination")
    ortho_fields = [
        ("Cover Test Distance", "ortho_cover_test_distance"),
        ("Cover Test Near",     "ortho_cover_test_near"),
        ("Nystagmus",           "ortho_nystagmus"),
        ("Ocular Motility",     "ortho_ocular_motility"),
        ("Convergence",         "ortho_convergence"),
        ("Remarks",             "ortho_remarks"),
        ("Binocular Balance",   "subj_binocular_balance"),
        ("Worth 4 Dot",         "subj_worth_4_dot"),
    ]
    any_ortho = False
    for label, key in ortho_fields:
        val = clinical.get(key)
        if val:
            any_ortho = True
            c1, c2 = st.columns([1, 2])
            with c1: st.write("**" + label + "**")
            with c2: st.write(str(val))
    if not any_ortho:
        st.caption("No orthoptic data recorded.")

    st.markdown("---")
    st.markdown("#### Order Items")
    c1, c2, c3, c4 = st.columns([1, 3, 1, 1])
    with c1: st.markdown("**Eye**")
    with c2: st.markdown("**Product**")
    with c3: st.markdown("**Qty**")
    with c4: st.markdown("**Amount**")
    total_amt = 0.0
    for line in order_lines:
        eye_label = "RIGHT" if line.get("eye_side") == "R" else "LEFT"
        amt = float(line.get("total_price", 0))
        total_amt += amt
        qty = line.get("display_qty") or line.get("requested_qty", "")
        product = (str(line.get("brand", "")) + " " + str(line.get("product_name", ""))).strip()
        c1, c2, c3, c4 = st.columns([1, 3, 1, 1])
        with c1: st.write(eye_label)
        with c2: st.write(product)
        with c3: st.write(str(qty))
        with c4: st.write("Rs." + f"{amt:,.2f}")
    st.markdown("**Total: Rs." + f"{total_amt:,.2f}**")
    st.caption("Generated: " + datetime.datetime.now().strftime("%d %b %Y %I:%M %p"))
