"""
fulfillment_layer.py
====================
BACKWARD COMPATIBILITY SHIM

This file re-exports from the new fulfillment/ package.
Any existing code importing from fulfillment_layer continues to work.

New code should import from the package directly:
    from modules.backoffice.fulfillment import render_fulfillment_panel

Old imports still work:
    from modules.backoffice.fulfillment_layer import render_assignment_block
    from modules.backoffice.fulfillment_layer import check_assignment_guard

NOTE: The old (order, all_lines) signatures are wrapped here so
      nothing breaks during the migration to ctx-first calling convention.
"""

from .fulfillment.ui import (
    render_fulfillment_panel,
    render_fulfillment_header,
    render_supplier_order_panel  as render_supplier_order_section,
    render_assignment_panel_block as render_assignment_block,
    check_assignment_guard,
)

# Old names → new names
render_fulfillment_section = render_fulfillment_panel


def _old_render_fulfillment_header(order, all_lines):
    """Compat wrapper for render_fulfillment_header(order, all_lines)."""
    from .kernel import BackofficeContext
    import streamlit as st
    ctx = BackofficeContext(order=order, session_state=st.session_state)
    render_fulfillment_header(ctx)


def _old_render_supplier_order_section(order):
    """Compat wrapper for render_supplier_order_section(order)."""
    from .kernel import BackofficeContext
    import streamlit as st
    ctx = BackofficeContext(order=order, session_state=st.session_state)
    render_supplier_order_section(ctx)


def _old_render_assignment_block(order, all_lines):
    """Compat wrapper for render_assignment_block(order, all_lines)."""
    from .kernel import BackofficeContext
    import streamlit as st
    ctx = BackofficeContext(order=order, session_state=st.session_state)
    render_assignment_block(ctx)


def _old_check_assignment_guard():
    """Compat wrapper for check_assignment_guard (no ctx)."""
    import streamlit as st

    class _FakeCtx:
        session = st.session_state

    return check_assignment_guard(_FakeCtx())


__all__ = [
    "render_fulfillment_panel",
    "render_fulfillment_section",
    "render_fulfillment_header",
    "render_supplier_order_section",
    "render_assignment_block",
    "check_assignment_guard",
]
