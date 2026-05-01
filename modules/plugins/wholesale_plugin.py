"""
modules/plugins/wholesale_plugin.py

Wholesale mode plugin.

Wraps the existing wholesale_punching.py render functions — zero logic is
duplicated here.  This file is intentionally thin: it is a wiring layer,
not a logic layer.

Architecture position:
    render_engine(WholesalePlugin())
        │
        ├─ init_session() → initialize_session_state() [wholesale_punching.py]
        │                   (crash recovery + cart restore handled by engine)
        ├─ header()       → render_order_header()      [ui_order_header.py]
        ├─ before_punch() → render_wholesale_controls() [wholesale_punching.py]
        ├─ punch_flow()   → power / product / alloc / cart [wholesale_punching.py]
        ├─ after_punch()  → (nothing extra for wholesale)
        └─ finalize()     → finalize_wholesale_order()  [wholesale_punching.py]
"""

import streamlit as st

from modules.core.plugin_base import PunchPlugin
from modules.ui_order_header import render_order_header


class WholesalePlugin(PunchPlugin):
    """Plugin that drives the wholesale desk order flow."""

    name = "WHOLESALE"

    # ── Hook 0 — Session bootstrap ────────────────────────────────────────────
    def init_session(self) -> None:
        """
        Initialise wholesale session state keys.

        Symmetric with RetailPlugin.init_session() — both modes bootstrap
        through this hook, keeping the engine's step-0 sequence identical
        for every plugin regardless of mode.
        """
        from modules.wholesale_punching import initialize_session_state
        initialize_session_state()

    # ── Hook 1 — Header ───────────────────────────────────────────────────────
    def header(self) -> dict | None:
        """
        Render the party / role-type selector via ui_order_header.
        Populates the shared session-state keys that the wholesale engine reads.
        Returns None if party is not yet selected → engine aborts render.
        """
        header = render_order_header()

        if not header or not header.get("party"):
            st.warning("⚠️ Please select Role Type and Party to continue.")
            return None

        # Populate shared session-state keys (wholesale_punching reads these)
        st.session_state.retail_patient_name = header["party"]
        st.session_state.retail_case_no      = header.get("customer_order_no") or ""
        st.session_state.wh_roletype         = header.get("roletype")
        st.session_state.wh_order_date       = header.get("order_date")

        return {
            "party":             header["party"],
            "roletype":          header.get("roletype"),
            "customer_order_no": header.get("customer_order_no"),
            "order_date":        header.get("order_date"),
        }

    # ── Hook 2 — Before punch ─────────────────────────────────────────────────
    def before_punch(self) -> None:
        """Render the wholesale control bar (reset buttons, new order, etc.)."""
        from modules.wholesale_punching import render_wholesale_controls
        render_wholesale_controls()

    # ── Hook 3 — Punch flow ───────────────────────────────────────────────────
    def punch_flow(self) -> None:
        """
        Run the standard wholesale punch sequence:
            power → product selection → batch allocation → cart
        All functions come from the existing wholesale_punching module.
        """
        from modules.wholesale_punching import (
            render_power_entry,
            render_product_selection,
            render_batch_allocation_editor,
            render_cart,
        )

        render_power_entry()
        render_product_selection()
        render_batch_allocation_editor()
        render_cart()

    # ── Hook 4 — After punch ──────────────────────────────────────────────────
    def after_punch(self) -> None:
        """Nothing extra for wholesale between cart and finalize."""
        pass

    # ── Hook 5 — Finalize ─────────────────────────────────────────────────────
    def finalize(self) -> None:
        """Delegate to the existing wholesale finalize + submit pipeline."""
        from modules.wholesale_punching import finalize_wholesale_order
        finalize_wholesale_order()

    # ── Hook 6 — End-of-render saves ──────────────────────────────────────────
    # Wholesale does not need snapshots / replay — base class no-op is correct.

    # ── Utility — validate ────────────────────────────────────────────────────
    def validate(self, cart: list) -> list[str]:
        """Wholesale cart validations (called by engine or finalize)."""
        errors = []
        if not cart:
            errors.append("Cart is empty — add at least one product.")
        if not st.session_state.get("retail_patient_name"):
            errors.append("No party selected.")
        return errors

    # ── Utility — enrich_order ────────────────────────────────────────────────
    def enrich_order(self, order_info: dict) -> dict:
        """Stamp wholesale-specific metadata onto the order before pipeline."""
        order_info["channel"]    = "Wholesale Desk"
        order_info["roletype"]   = st.session_state.get("wh_roletype")
        order_info["order_date"] = str(st.session_state.get("wh_order_date", ""))
        return order_info
