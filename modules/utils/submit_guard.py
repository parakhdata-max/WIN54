"""
modules/utils/submit_guard.py  — Submit Guard v3
=================================================
Industrial double-submit prevention for Streamlit.

USAGE — Recommended:
    from modules.utils.submit_guard import guarded_submit, is_locked

    if st.button("Confirm", disabled=is_locked("retail_confirm")):
        with guarded_submit("retail_confirm") as allowed:
            if allowed:
                result = pipeline.submit()
                st.rerun()

USAGE — Legacy compatible:
    from modules.utils.submit_guard import submit_guard, clear_lock, is_locked

    if st.button("Save", disabled=is_locked("save_order")):
        if submit_guard("save_order"):
            try:
                save()
            finally:
                clear_lock("save_order")

USAGE — One-shot pipeline (retry-safe):
    result = run_once("retail_confirm", pipeline.submit_retail, cart_lines=lines)
"""

import time
import streamlit as st
from contextlib import contextmanager

LOCK_PREFIX    = "_lock_"
LOCK_TS_PREFIX = "_lock_ts_"
LOCK_TTL       = 45          # seconds — auto-unlock if app crashes mid-process


# ── Internal ──────────────────────────────────────────────────────────────────

def _lk(key):  return f"{LOCK_PREFIX}{key}"
def _ts(key):  return f"{LOCK_TS_PREFIX}{key}"


# ── Public API ────────────────────────────────────────────────────────────────

def is_locked(key: str) -> bool:
    """
    Returns True if this action is locked.
    Auto-unlocks stale locks after LOCK_TTL seconds (handles app crash).
    """
    if not st.session_state.get(_lk(key)):
        return False
    created = st.session_state.get(_ts(key), 0)
    if time.time() - created > LOCK_TTL:
        clear_lock(key)
        return False
    return True


def acquire_lock(key: str) -> bool:
    """
    Try to acquire the lock.
    Returns True if acquired. Returns False if already locked.
    NEVER calls st.stop() — caller controls flow.
    """
    if is_locked(key):
        st.warning("⏳ Already processing — please wait...")
        return False
    st.session_state[_lk(key)] = True
    st.session_state[_ts(key)] = time.time()
    return True


def clear_lock(key: str):
    """Release the lock. Call after success OR failure."""
    st.session_state.pop(_lk(key), None)
    st.session_state.pop(_ts(key), None)


def clear_all_locks():
    """Clear every active submit lock. Call inside industrial_reset(ALL)."""
    for k in list(st.session_state.keys()):
        if k.startswith(LOCK_PREFIX) or k.startswith(LOCK_TS_PREFIX):
            st.session_state.pop(k, None)


def submit_guard(key: str) -> bool:
    """Legacy-compatible one-call guard. Same as acquire_lock(key)."""
    return acquire_lock(key)


# ── Context manager — RECOMMENDED ─────────────────────────────────────────────

@contextmanager
def guarded_submit(key: str):
    """
    Context manager: acquires on enter, releases on exit ONLY if NOT rerunning.

    When st.rerun() is called inside the block, Streamlit raises a RerunException
    which propagates through finally. We catch it and intentionally do NOT clear
    the lock — the rerun itself will render the button as disabled (is_locked=True),
    preventing a double-submit. The lock auto-expires after LOCK_TTL seconds.

    Pattern:
        with guarded_submit("retail_confirm") as allowed:
            if allowed:
                result = pipeline.submit(...)
                if result["status"] == "CONFIRMED":
                    st.rerun()   # lock stays → button disabled on next render
    """
    acquired = acquire_lock(key)
    try:
        yield acquired
    except Exception as _exc:
        # Streamlit rerun/stop raises internal exceptions — let them propagate
        # but do NOT clear the lock (protects against double-submit on rerun)
        _exc_name = type(_exc).__name__
        if "Rerun" in _exc_name or "Stop" in _exc_name or "StopException" in _exc_name:
            raise  # propagate without clearing lock
        # For real errors: clear lock so button re-enables
        if acquired:
            clear_lock(key)
        raise
    else:
        # Normal completion (no rerun/stop) — clear lock
        if acquired:
            clear_lock(key)


# ── Retry-safe one-shot runner ─────────────────────────────────────────────────

def run_once(key: str, fn, *args, **kwargs):
    """
    Run fn exactly once even under rapid re-renders. Lock always releases.
    Returns fn's result, or None if already locked.

        result = run_once("confirm", pipeline.submit, cart=lines)
        if result is None: st.stop()
    """
    if not acquire_lock(key):
        return None
    try:
        return fn(*args, **kwargs)
    finally:
        clear_lock(key)


# ── Debug panel ───────────────────────────────────────────────────────────────

def debug_locks_panel():
    """
    Admin UI — shows active locks with force-unlock buttons.
    Add to Admin tab:
        from modules.utils.submit_guard import debug_locks_panel
        debug_locks_panel()
    """
    locks = {
        k[len(LOCK_PREFIX):]: st.session_state.get(_ts(k[len(LOCK_PREFIX):]))
        for k in st.session_state if k.startswith(LOCK_PREFIX)
    }
    if not locks:
        st.success("✅ No active submit locks — all clear")
        return

    st.warning(f"⚠️ {len(locks)} active lock(s)")
    for key, ts in sorted(locks.items()):
        age      = f"{time.time()-ts:.0f}s ago" if ts else "unknown"
        ttl_left = max(0, LOCK_TTL - (time.time()-ts)) if ts else 0
        c1, c2, c3 = st.columns([3, 3, 1])
        c1.write(f"🔒 `{key}`")
        c2.caption(f"Locked {age} · auto-clears in {ttl_left:.0f}s")
        with c3:
            if st.button("Unlock", key=f"_force_ul_{key}", type="secondary"):
                clear_lock(key)
                st.rerun()
