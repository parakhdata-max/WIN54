"""Sidebar page for local LAN printer selection."""

from __future__ import annotations

import streamlit as st

from modules.printing.printer_config import (
    AUDIT_FILE,
    CONFIG_FILE,
    apply_printer_profile,
    get_printer,
    load_printer_settings,
    list_windows_printers,
    printer_exists,
    printer_status,
    save_printer_profile,
    save_printer_settings,
)


def _printer_pick(label: str, current: str, printers: list[str], status_map: dict, key: str) -> str:
    label_by_name = {}
    options = [""]
    for p in printers:
        stt = status_map.get(p) or {}
        badge = "READY" if stt.get("ready") else str(stt.get("status") or "NOT_READY")
        shown = f"{p}  [{badge}]"
        label_by_name[shown] = p
        options.append(shown)
    options.append("Manual / network path")

    current_label = next((shown for shown, name in label_by_name.items() if name == current), "")
    idx = options.index(current_label) if current_label in options else 0
    pick = st.selectbox(label, options, index=idx, key=f"{key}_pick")
    if pick == "Manual / network path":
        return st.text_input(
            f"{label} manual name/path",
            value=current if current not in printers else "",
            key=f"{key}_manual",
            placeholder=r"e.g. TSC-2 or \\COUNTER2\TSC",
        ).strip()
    return label_by_name.get(pick, "").strip()


def _status_line(name: str) -> None:
    status = printer_status(name)
    if status.get("ready"):
        st.success(f"{name}: READY")
    elif status.get("exists"):
        st.warning(f"{name}: {status.get('message')}")
    else:
        st.error(status.get("message"))


def render_printer_settings() -> None:
    st.markdown("### Local Printer Settings")
    st.caption(
        "These settings are saved on this computer only. "
        "Each LAN counter can choose its own TSC, document printer and CR80 printer."
    )

    all_printers = list_windows_printers()
    status_map = {p: printer_status(p) for p in all_printers}
    ready_only = st.checkbox("Show only READY printers in dropdown", value=False, key="lp_ready_only")
    printers = [p for p in all_printers if status_map.get(p, {}).get("ready")] if ready_only else all_printers
    if printers:
        ready_count = sum(1 for p in all_printers if status_map.get(p, {}).get("ready"))
        st.info(f"{len(all_printers)} Windows printer queue(s) detected · {ready_count} ready by Windows status.")
    else:
        st.warning("No READY printers detected. You can still enter a manual network printer path or disable the ready-only filter.")

    current_tsc = get_printer("tsc")
    current_doc = get_printer("document")
    current_card = get_printer("card")

    c1, c2, c3 = st.columns(3)
    with c1:
        tsc = _printer_pick("TSC / Label Printer", current_tsc, printers, status_map, "lp_tsc")
        if tsc:
            _status_line(tsc)
    with c2:
        doc = _printer_pick("Document Printer", current_doc, printers, status_map, "lp_doc")
        if doc:
            _status_line(doc)
    with c3:
        card = _printer_pick("CR80 Card Printer", current_card, printers, status_map, "lp_card")
        if card:
            _status_line(card)

    html_fallback = st.checkbox(
        "Use HTML/Windows print fallback if direct print fails",
        value=True,
        key="lp_html_fallback",
    )

    b1, b2, b3 = st.columns(3)
    with b1:
        if st.button("Save Printer Settings", type="primary", use_container_width=True):
            save_printer_settings({
                "tsc_label_printer": tsc,
                "document_printer": doc,
                "card_printer": card,
                "html_fallback": html_fallback,
            })
            st.success("Saved local printer settings")
            st.rerun()
    with b2:
        if st.button("Test TSC Label", use_container_width=True):
            try:
                from modules.printing.label_printer import build_tspl, _send_tspl
                ok, msg = _send_tspl(build_tspl("TEST123", "Parakh", "Rs.1", 1))
                st.success(msg) if ok else st.error(msg)
            except Exception as exc:
                st.error(str(exc))

    st.markdown("#### Printer Profiles")
    settings = load_printer_settings()
    profiles = settings.get("profiles") if isinstance(settings.get("profiles"), dict) else {}
    p1, p2, p3 = st.columns([2, 2, 1])
    with p1:
        profile_name = st.text_input("Profile name", placeholder="Counter 1 / Counter 2 / Backoffice", key="lp_profile_name")
    with p2:
        chosen_profile = st.selectbox("Apply profile", [""] + sorted(profiles.keys()), key="lp_profile_apply")
    with p3:
        if st.button("Save Profile", use_container_width=True):
            save_printer_profile(profile_name, {
                "tsc_label_printer": tsc,
                "document_printer": doc,
                "card_printer": card,
            })
            st.success("Profile saved")
            st.rerun()
        if chosen_profile and st.button("Apply", use_container_width=True):
            if apply_printer_profile(chosen_profile):
                st.success("Profile applied")
                st.rerun()
            else:
                st.error("Profile not found")
    with b3:
        if st.button("Test Document Printer", use_container_width=True):
            try:
                from modules.printing.direct_print import spool_html_to_printer
                ok, msg = spool_html_to_printer(
                    "<html><body><h2>DV ERP Printer Test</h2><p>Document printer OK.</p></body></html>",
                    doc,
                    job_name="DV_ERP_Printer_Test",
                )
                st.success(msg) if ok else st.error(msg)
            except Exception as exc:
                st.error(str(exc))

    with st.expander("Detected Windows printer names", expanded=False):
        if all_printers:
            for p in all_printers:
                stt = status_map.get(p) or {}
                line = f"{p} — {'READY' if stt.get('ready') else stt.get('message')}"
                if stt.get("ready"):
                    st.success(line)
                else:
                    st.warning(line)
        else:
            st.caption("No printers detected.")
        st.caption(f"Saved at: {CONFIG_FILE}")

    with st.expander("Printer Settings Audit Log", expanded=False):
        if AUDIT_FILE.exists():
            lines = AUDIT_FILE.read_text(encoding="utf-8").splitlines()[-20:]
            for line in reversed(lines):
                st.code(line)
        else:
            st.caption("No printer setting changes recorded yet.")
