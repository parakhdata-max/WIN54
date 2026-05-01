"""
modules/core/plugin_base.py

Minimal plugin contract.
All mode-specific behaviour (retail, wholesale, lab, online) inherits from here.
The engine calls these hooks in order — plugins override only what they need.

Hook call order inside render_engine():
    0. init_session()       → mode-specific session defaults + integrity checks
                              (universal infra — crash recovery, cart restore —
                               is handled by the engine AFTER this hook)
    1. header()             → returns context dict, or None to abort render
    2. before_punch()       → optional UI before the punch flow
    3. punch_flow()         → REQUIRED: power → product → allocation → cart
    4. after_punch()        → optional UI after cart (printouts, barcode, etc.)
    5. finalize()           → submit button + order pipeline call
    6. on_render_complete() → end-of-render saves (snapshots, cart persist, replay)

Separation of concerns:
    Plugin  → business logic, mode-specific UI, session defaults
    Engine  → hook orchestration, universal infra (crash recovery, cart restore)
"""


class PunchPlugin:
    """Base plugin contract for the punch engine."""

    # ── Identity ──────────────────────────────────────────────────────────────
    name: str = "BASE"

    # ── Hook 0 — Session bootstrap ────────────────────────────────────────────
    def init_session(self) -> None:
        """
        Initialise mode-specific session state keys and run integrity checks.

        Called by the engine BEFORE universal infra (crash recovery, cart
        restore) so that all keys exist when restores run.

        Override in every plugin that has session state.
        Default is a safe no-op so abstract/test plugins don't crash.
        """
        pass

    # ── Hook 1 — Header ───────────────────────────────────────────────────────
    def header(self) -> dict | None:
        """
        Render the order header UI (party selector, patient picker, etc.).

        Returns:
            dict  → context stored in session_state["_plugin_context"]
            None  → abort render (e.g. party not yet selected)
        """
        return {}

    # ── Hook 2 — Before punch (optional) ─────────────────────────────────────
    def before_punch(self) -> None:
        """
        Optional UI rendered between header and the core punch flow.
        Examples: control bar, patient clinical panel, retail toggles.
        """
        pass

    # ── Hook 3 — Punch flow (REQUIRED) ───────────────────────────────────────
    def punch_flow(self) -> None:
        """
        Core punch body: power → product selection → batch allocation → cart.

        This hook is REQUIRED.  Subclasses MUST override it.
        Raising NotImplementedError here surfaces the problem immediately at
        runtime rather than silently rendering nothing.
        """
        raise NotImplementedError(
            f"Plugin '{self.name}' must implement punch_flow(). "
            "Wire in your render_power_entry / render_product_selection / "
            "render_batch_allocation_editor / render_cart calls here."
        )

    # ── Hook 4 — After punch (optional) ──────────────────────────────────────
    def after_punch(self) -> None:
        """
        Optional UI rendered after the cart and before finalize.
        Examples: printable summary, clinical printout, barcode label.
        """
        pass

    # ── Hook 5 — Finalize (optional) ─────────────────────────────────────────
    def finalize(self) -> None:
        """
        Submit button + order pipeline call.
        Optional: a plugin that has no submit step (e.g. a read-only view)
        can leave this as a no-op.
        """
        pass

    # ── Hook 6 — End-of-render saves (optional) ───────────────────────────────
    def on_render_complete(self) -> None:
        """
        Called by the engine at the very end of every successful render.
        Use for: runtime snapshots, cart persistence, session replay logging.
        Default is a safe no-op — only override when the mode needs saves.
        """
        pass

    # ── Utility hooks ─────────────────────────────────────────────────────────
    def validate(self, cart: list) -> list[str]:
        """
        Return a list of error strings (empty = valid).
        Called by finalize implementations before submitting.
        """
        return []

    def enrich_order(self, order_info: dict) -> dict:
        """
        Last chance to add/modify fields in order_info before pipeline submit.
        Must return the (possibly modified) order_info dict.
        """
        return order_info
