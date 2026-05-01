"""
modules/core/punch_engine.py

Single entry point for ALL punching modes.
The engine itself contains zero business logic — it only calls plugin hooks
in the correct order and handles the universal infrastructure that every
mode needs (crash recovery, cart restore, end-of-render saves).

Usage:
    from modules.core.punch_engine import render_engine
    from modules.plugins.wholesale_plugin import WholesalePlugin

    render_engine(WholesalePlugin())

Hook sequence (engine-owned steps marked ★):
    ★ 0a. plugin.init_session()    — mode-specific session defaults + integrity
    ★ 0b. restore_after_crash()    — universal crash recovery (engine-owned)
    ★ 0c. restore_cart()           — universal cart restore  (engine-owned)
      1.  plugin.header()          — returns context or None to abort
      2.  plugin.before_punch()    — optional pre-punch UI
      3.  plugin.punch_flow()      — REQUIRED core punch body
      4.  plugin.after_punch()     — optional post-cart UI
      5.  plugin.finalize()        — submit button + pipeline
    ★ 6.  plugin.on_render_complete() — end-of-render saves
"""

import streamlit as st


def render_engine(plugin) -> None:
    """
    Orchestrate a full punch session for the given plugin.

    Plugins provide business logic and mode-specific UI.
    The engine provides universal infrastructure scaffolding.
    """

    # ── Step 0a — Mode-specific session bootstrap ──────────────────────────────
    # Plugin initialises its own session keys and runs integrity checks.
    # Must run before any infra restore so all keys exist when restores write.
    plugin.init_session()

    # ── Step 0b/c — Universal infrastructure (engine-owned, NOT plugin concern) ─
    # These run for every mode. Plugins must NOT import or call these directly.
    try:
        from modules.core.crash_recovery import restore_after_crash
        restore_after_crash()
    except Exception:
        pass  # Graceful: crash recovery is best-effort

    try:
        from modules.core.persistent_cart import restore_cart
        restore_cart()
    except Exception:
        pass  # Graceful: cart restore is best-effort

    # ── Step 1 — Header ───────────────────────────────────────────────────────
    context = plugin.header()

    if context is None:
        # Plugin already rendered the gating UI (warning, selector, etc.)
        return

    # Stash context so finalize / enrich_order can read it downstream
    st.session_state["_plugin_context"] = context
    st.session_state["_active_plugin"]  = plugin.name

    # ── Step 2 — Pre-punch UI ─────────────────────────────────────────────────
    plugin.before_punch()

    # ── Step 3 — Core punch flow (REQUIRED) ───────────────────────────────────
    plugin.punch_flow()

    # ── Step 4 — Post-punch UI ────────────────────────────────────────────────
    plugin.after_punch()

    # ── Step 5 — Finalize + submit ────────────────────────────────────────────
    plugin.finalize()

    # ── Step 6 — End-of-render saves ──────────────────────────────────────────
    # Guard with hasattr: future plugins may not inherit from PunchPlugin
    # directly (e.g. a quick test stub). Base class provides a no-op default,
    # but the guard is a second safety net for any plugin that bypasses it.
    if hasattr(plugin, "on_render_complete"):
        plugin.on_render_complete()
