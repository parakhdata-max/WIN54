"""
modules/plugins/retail_plugin.py

Retail mode plugin.

Wraps the existing retail_punching.py render functions — zero logic is
duplicated here.  Retail has a richer flow than wholesale (patient search,
clinical exam, lens/boxing params, barcode), all of which live in hooks.

Architecture position:
    render_engine(RetailPlugin())
        │
        ├─ init_session()   → session defaults + integrity check [retail_punching.py]
        │                     (crash recovery + cart restore handled by engine)
        ├─ header()         → business-only: returns mode tag, no infra here
        ├─ before_punch()   → controls + patient selection + clinical exam
        ├─ punch_flow()     → power / product / lens+boxing params / alloc / cart
        ├─ after_punch()    → printable summary + clinical printout + barcode
        ├─ finalize()       → finalize_retail_order_to_backoffice()
        └─ on_render_complete() → runtime snapshots + cart persist + replay log
"""

import streamlit as st

from modules.core.plugin_base import PunchPlugin


class RetailPlugin(PunchPlugin):
    """Plugin that drives the retail (counter / OPD) order flow."""

    name = "RETAIL"

    # ── Hook 0 — Session bootstrap ────────────────────────────────────────────
    def init_session(self) -> None:
        """
        Initialise retail session state and run integrity checks.

        Runs BEFORE the engine calls restore_after_crash() and restore_cart(),
        guaranteeing all keys exist when restores attempt to overwrite them.

        No infra imports here — crash recovery and cart restore belong
        to the engine (step 0b/0c in punch_engine.py).
        """
        from modules.retail_punching import initialize_session_state, assert_session_integrity
        initialize_session_state()   # ALWAYS FIRST — creates all session keys
        assert_session_integrity()   # ERP isolation layer

        # Hard key guarantee after integrity check
        # (restores may overwrite or skip keys — re-guarantee before render)
        mandatory = {
            "retail_selected_product":   None,
            "retail_order_lines":        [],
            "retail_current_allocation": None,
            "retail_show_batch_editor":  False,
        }
        for k, v in mandatory.items():
            if k not in st.session_state:
                st.session_state[k] = v

    # ── Hook 1 — Header ───────────────────────────────────────────────────────
    def header(self) -> dict | None:
        """
        Retail does not use the party/order-header widget.
        Patient selection happens inside before_punch() below.

        Returns a non-None dict so the engine proceeds.
        Contains NO infrastructure calls — those belong in init_session().
        """
        return {"mode": "retail"}

    # ── Hook 2 — Before punch ─────────────────────────────────────────────────
    def before_punch(self) -> None:
        """
        Retail-specific pre-punch UI:
            - Page title + description
            - Global control bar (reset buttons, undo, etc.)
            - Patient selection (name / phone / case-ID search)
            - Clinical examination expander
        """
        from modules.retail_punching import render_retail_controls, render_patient_selection
        from modules.clinical_exam import render_clinical_examination

        st.title("🛍️ Retail Order Punching")
        st.markdown(
            "Complete retail order management with Case ID search, "
            "patient history, and stock allocation"
        )

        render_retail_controls()
        st.markdown("---")
        render_patient_selection()
        render_clinical_examination()

    # ── Hook 3 — Punch flow ───────────────────────────────────────────────────
    def punch_flow(self) -> None:
        """
        Retail punch sequence (mirrors render_retail_punching order):
            power → product → lens params → boxing params → batch allocation → cart
        Lens/boxing params sit between product and allocation in retail flow.
        """
        from modules.retail_punching import (
            render_power_entry,
            render_product_selection,
            render_lens_params,
            render_boxing_params,
            render_batch_allocation_editor,
            render_order_lines,
        )

        render_power_entry()
        render_product_selection()
        render_lens_params()
        render_boxing_params()
        render_batch_allocation_editor()
        render_order_lines()

    # ── Hook 4 — After punch ──────────────────────────────────────────────────
    def after_punch(self) -> None:
        """
        Retail post-cart UI (rendered before finalize submit button):
            - Printable provisional summary
            - Clinical printout
            - Patient barcode label
        """
        try:
            from modules.core.print_summary import render_printable_summary
            render_printable_summary(
                st.session_state.retail_order_lines,
                st.session_state.retail_patient_name,
                st.session_state.retail_patient_mobile,
                st.session_state.retail_provisional_order_id,
            )
        except Exception as err:
            import traceback
            st.error("ERROR in print_summary: " + str(err))
            st.code(traceback.format_exc())

        from modules.core.print_clinical import render_printable_clinical
        rx_r = st.session_state.get("retail_new_rx_r") or st.session_state.get("retail_old_rx_r") or {}
        rx_l = st.session_state.get("retail_new_rx_l") or st.session_state.get("retail_old_rx_l") or {}
        render_printable_clinical(rx_r, rx_l, st.session_state.retail_patient_name)

        from modules.core.barcode_label import render_patient_label
        render_patient_label(
            {
                "id":     str(st.session_state.get("retail_patient_id", "")),
                "name":   st.session_state.retail_patient_name,
                "mobile": st.session_state.retail_patient_mobile,
            },
            rx_r,
            rx_l,
        )

    # ── Hook 5 — Finalize ─────────────────────────────────────────────────────
    def finalize(self) -> None:
        """Delegate to the existing retail finalize + submit pipeline."""
        from modules.retail_punching import finalize_retail_order_to_backoffice
        finalize_retail_order_to_backoffice()

    # ── Hook 6 — End-of-render saves ──────────────────────────────────────────
    def on_render_complete(self) -> None:
        """
        Auto-save state snapshots at the end of every render.
        Matches the tail of the original render_retail_punching() function.

        Lives here — not in finalize() — because these saves must run even
        if the submit button was not pressed this cycle.
        """
        from modules.core.crash_recovery import save_runtime_snapshot
        from modules.core.persistent_cart import persist_cart
        from modules.core.session_replay import record_step
        save_runtime_snapshot()
        persist_cart()
        record_step("render_complete")

    # ── Utility — validate ────────────────────────────────────────────────────
    def validate(self, cart: list) -> list[str]:
        """Retail-specific cart validations."""
        errors = []
        if not st.session_state.get("retail_patient_id"):
            errors.append("Patient not selected.")
        if not cart:
            errors.append("Cart is empty — add at least one product.")
        return errors

    # ── Utility — enrich_order ────────────────────────────────────────────────
    def enrich_order(self, order_info: dict) -> dict:
        """Stamp retail-specific metadata onto the order before pipeline."""
        order_info["retail_flag"]    = True
        order_info["channel"]        = "Retail Counter"
        order_info["patient_id"]     = st.session_state.get("retail_patient_id")
        order_info["patient_mobile"] = st.session_state.get("retail_patient_mobile", "")
        order_info["lens_params"]    = dict(st.session_state.get("retail_lens_params") or {})
        order_info["boxing_params"]  = dict(st.session_state.get("retail_boxing_params") or {})
        return order_info
