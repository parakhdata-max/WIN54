"""
modules/core/kb_helpers.py
===========================
Shared keyboard-first helpers for all billing screens.
Import: from modules.core.kb_helpers import autofocus_scan, enter_to_submit, kb_legend
"""

import streamlit.components.v1 as _stc


def autofocus_scan(placeholder_hint: str = "Scan"):
    """
    Auto-focus the first text input whose placeholder contains hint.
    Falls back to first visible input on page.
    """
    _stc.html(f"""<script>
    (function() {{
        function go() {{
            var inputs = window.parent.document.querySelectorAll(
                'input[type="text"], input[type="number"]'
            );
            for (var i = 0; i < inputs.length; i++) {{
                var ph = (inputs[i].placeholder || '').toLowerCase();
                if (ph.indexOf('{placeholder_hint.lower()}') >= 0) {{
                    inputs[i].focus(); inputs[i].select(); return;
                }}
            }}
            if (inputs.length > 0) {{ inputs[0].focus(); }}
        }}
        setTimeout(go, 180);
    }})();
    </script>""", height=0)


def enter_to_submit():
    """
    Wire Enter key → click the page's primary button.
    Skips textarea. Debounced 600ms to prevent double-fire.
    """
    _stc.html("""<script>
    (function() {
        if (window._enterWired) return;
        window._enterWired = true;
        var busy = false;
        window.parent.document.addEventListener('keydown', function(e) {
            if (busy || e.key !== 'Enter' || e.shiftKey) return;
            var t = e.target;
            if (t.tagName === 'TEXTAREA') return;
            if (t.tagName === 'SELECT') return;
            var btn = window.parent.document.querySelector(
                'button[data-testid="baseButton-primary"]:not([disabled])'
            );
            if (btn) {
                busy = true;
                e.preventDefault();
                btn.click();
                setTimeout(function() { busy = false; }, 600);
            }
        }, true);
    })();
    </script>""", height=0)


def kb_legend(extras: list = None):
    """
    Show a compact keyboard shortcut legend bar.
    extras: list of (key, action) tuples to add.
    """
    import streamlit as st
    hints = [
        ("Enter", "primary action"),
        ("Tab", "next field"),
        ("Scan", "auto-add to cart"),
    ]
    if extras:
        hints += extras
    parts = "".join(
        f"<span><b style='color:var(--color-text-info)'>{k}</b>"
        f"<span style='color:var(--color-text-tertiary)'> = {v}</span></span>"
        for k, v in hints
    )
    st.markdown(
        f"<div style='border:1px solid var(--color-border-tertiary);"
        f"border-radius:6px;padding:4px 12px;margin-bottom:8px;"
        f"font-size:.65rem;display:flex;gap:14px;flex-wrap:wrap'>"
        f"⌨️  {parts}</div>",
        unsafe_allow_html=True,
    )
