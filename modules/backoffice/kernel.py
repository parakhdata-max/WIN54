"""
kernel.py
=========
Backoffice Kernel — Context builder + lifecycle orchestration.

CONTEXT FIELDS (Issue 6 — stronger kernel)
-------------------------------------------
  ctx.order         : live order dict
  ctx.session       : st.session_state reference
  ctx.user          : user info dict (role, name, id)
  ctx.flags         : runtime boolean flags (debug_mode, etc.)
  ctx.audit         : append-only audit trail list for this render
  ctx.start_time    : float — render start timestamp (perf tracking)
  ctx.meta          : arbitrary metadata dict (extensible)

SYSTEM FLAGS (Issue 3 — debug feature flag)
--------------------------------------------
  SYSTEM_FLAGS["debug_mode"]     → guards debug overlay exposure
  SYSTEM_FLAGS["advisory_roles"] → roles allowed to see advisory panel

NAMING CONVENTION (Issue minor 1)
----------------------------------
  Type      Suffix    Example
  Router    shell     backoffice_shell.py
  Logic     layer     fulfillment_layer.py
  UI        panel     assignment_panel.py, advisory_panel.py

LIFECYCLE HOOKS
---------------
  _before_render(ctx)         → permission checks, telemetry start
  _after_render(ctx)          → perf logging, audit flush
  before_save_hook(ctx)       → billing guard, assignment guard, GST
  after_save_hook(ctx, id)    → notifications, WhatsApp trigger, audit log
"""

import time
import logging
from typing import Callable, Optional, Any, Dict, List

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# SYSTEM FLAGS  (Issue 3 — debug feature flag; Issue minor 2 — role gate)
# ═══════════════════════════════════════════════════════════════════════

SYSTEM_FLAGS: Dict[str, Any] = {
    # Set to True only in dev/staging — prevents debug overlay in prod
    "debug_mode": False,

    # Roles allowed to access Advisory Procurement panel
    # Empty list = no restriction (default until role system is wired)
    "advisory_roles": [],

    # Roles allowed to access Billing Gate tab
    "billing_gate_roles": [],

    # Feature flags — flip without code changes
    "enable_whatsapp_po":   False,
    "enable_audit_log":     True,
    "enable_perf_tracking": True,
}


def set_flag(name: str, value: Any) -> None:
    """Update a system flag at runtime (e.g. from app config or env vars)."""
    SYSTEM_FLAGS[name] = value


def flag(name: str, default: Any = False) -> Any:
    """Read a system flag safely."""
    return SYSTEM_FLAGS.get(name, default)


# ═══════════════════════════════════════════════════════════════════════
# CONTEXT OBJECT  (Issue 1 — ctx used everywhere; Issue 6 — richer ctx)
# ═══════════════════════════════════════════════════════════════════════

class BackofficeContext:
    """
    Single carrier object passed through ALL backoffice layers.

    RULE: Every layer function must accept ctx as first argument.
    Never pass (order, st) separately when ctx is available.

    Example — correct:
        def render_fulfillment_panel(ctx: BackofficeContext) -> None:
            order = ctx.order
            session = ctx.session

    Example — wrong (old pattern):
        def render_fulfillment_panel(order: Dict, st) -> None:
            ...

    FIELDS
    ------
    order        : live order dict
    session      : st.session_state (Streamlit)
    user         : {id, name, role, email} — from auth layer
    flags        : runtime flags for this render cycle
    audit        : list of audit events recorded this render
    start_time   : perf tracking — time.time() at context creation
    meta         : extensible dict for one-off data threading
    """

    def __init__(
        self,
        order: Dict,
        session_state: Any,
        user: Optional[Dict] = None,
        initial_flags: Optional[Dict] = None,
    ):
        self.order      = order
        self.session    = session_state
        self.user       = user or {}
        self.flags      = dict(SYSTEM_FLAGS)           # copy — per-render overrides
        self.audit: List[Dict] = []                    # append-only audit trail
        self.start_time = time.time()                  # perf tracking
        self.meta: Dict = {}                           # extensible bag

        # Apply any per-render flag overrides (e.g. from query params)
        if initial_flags:
            self.flags.update(initial_flags)

    # ── Core accessors ───────────────────────────────────────────────

    @property
    def order_id(self) -> Optional[str]:
        """Display order ID — order_no preferred, falls back to id."""
        return self.order.get("order_no") or self.order.get("id")

    @property
    def all_lines(self) -> List[Dict]:
        """Flat list: stock + inhouse + lab lines."""
        lines: List[Dict] = []
        lines.extend(self.order.get("stock_lines", []))
        lines.extend(self.order.get("inhouse_lines", []))
        lines.extend(self.order.get("lab_order_lines", []))
        return lines

    # ── Session state shortcuts ──────────────────────────────────────

    @property
    def is_assignments_locked(self) -> bool:
        return bool(self.session.get("bo_assignments_locked", False))

    @property
    def is_debug_pricing(self) -> bool:
        """True only if debug_mode system flag AND session toggle are both on."""
        return (
            bool(self.flags.get("debug_mode", False))
            and bool(self.session.get("debug_pricing", False))
        )

    # ── Named lock helpers ───────────────────────────────────────────

    def lock(self, name: str) -> None:
        """Set a named boolean lock in session state."""
        self.session[f"bo_lock_{name}"] = True

    def unlock(self, name: str) -> None:
        self.session[f"bo_lock_{name}"] = False

    def is_locked(self, name: str) -> bool:
        return bool(self.session.get(f"bo_lock_{name}", False))

    # ── Role / permission helpers ────────────────────────────────────

    @property
    def user_role(self) -> str:
        return str(self.user.get("role", "")).lower()

    def has_role(self, *roles: str) -> bool:
        """True if user has any of the given roles (case-insensitive)."""
        return self.user_role in [r.lower() for r in roles]

    def can_access_advisory(self) -> bool:
        """
        True if user is allowed to view Advisory panel.
        Empty advisory_roles list = no restriction.
        """
        allowed = self.flags.get("advisory_roles", [])
        if not allowed:
            return True
        return self.has_role(*allowed)

    def can_access_billing_gate(self) -> bool:
        allowed = self.flags.get("billing_gate_roles", [])
        if not allowed:
            return True
        return self.has_role(*allowed)

    # ── Audit trail ──────────────────────────────────────────────────

    def record(self, event: str, payload: Optional[Dict] = None) -> None:
        """
        Append an audit event to this render's audit trail.
        Flushed by after_save_hook or _after_render.

        Usage:
            ctx.record("product_changed", {"old": "A", "new": "B"})
        """
        entry = {
            "event":    event,
            "order_id": self.order_id,
            "user":     self.user.get("id", "unknown"),
            "ts":       time.time(),
        }
        if payload:
            entry["payload"] = payload
        self.audit.append(entry)

    def flush_audit(self) -> None:
        """
        Write audit trail to persistent logger.
        Called by after_save_hook — no-op if audit log disabled.
        """
        if not self.flags.get("enable_audit_log", True):
            return
        if not self.audit:
            return
        try:
            from modules.backoffice.audit_logger import audit_bulk
            audit_bulk(self.audit)
        except Exception as e:
            log.warning(f"[Kernel] Audit flush failed: {e}")
        finally:
            self.audit.clear()

    # ── Perf tracking ─────────────────────────────────────────────────

    @property
    def elapsed_ms(self) -> int:
        """Milliseconds since context was created."""
        return int((time.time() - self.start_time) * 1000)

    def log_perf(self, label: str) -> None:
        if self.flags.get("enable_perf_tracking", True):
            log.debug(f"[Perf] {label} | order={self.order_id} | {self.elapsed_ms}ms")

    # ── Repr ─────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return (
            f"<BackofficeContext order={self.order_id} "
            f"user={self.user.get('id','?')} "
            f"lines={len(self.all_lines)} "
            f"elapsed={self.elapsed_ms}ms>"
        )


# ═══════════════════════════════════════════════════════════════════════
# KERNEL — CONTEXT BUILDER + ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════

def build_context(
    order: Dict,
    st_module: Any,
    user: Optional[Dict] = None,
    flags: Optional[Dict] = None,
) -> BackofficeContext:
    """
    Build a BackofficeContext for the given order and Streamlit session.

    Args:
        order:      The live order dict
        st_module:  The streamlit module (pass `st` from caller)
        user:       Optional user dict {id, name, role} from auth layer
        flags:      Optional per-render flag overrides

    Returns:
        BackofficeContext — ready to thread through all layers
    """
    if user is None:
        user = st_module.session_state.get("current_user") or {}

    ctx = BackofficeContext(
        order=order,
        session_state=st_module.session_state,
        user=user,
        initial_flags=flags,
    )
    ctx.log_perf("context_built")
    return ctx


def run_backoffice(
    order: Dict,
    st_module: Any,
    renderer: Callable[["BackofficeContext"], None],
    user: Optional[Dict] = None,
) -> None:
    """
    Orchestration entry point for the backoffice shell.

    Builds context → lifecycle hooks → renderer → cleanup.
    """
    ctx = build_context(order, st_module, user=user)
    _before_render(ctx)
    renderer(ctx)
    _after_render(ctx)


# ═══════════════════════════════════════════════════════════════════════
# LIFECYCLE HOOKS
# ═══════════════════════════════════════════════════════════════════════

def _before_render(ctx: BackofficeContext) -> None:
    ctx.log_perf("before_render")


def _after_render(ctx: BackofficeContext) -> None:
    ctx.log_perf("after_render")


def before_save_hook(ctx: BackofficeContext) -> bool:
    """
    Pre-save validation gate.
    Returns True to allow save, False to block.
    Individual guards (billing, assignment, GST) still run inline in shell
    — this is the future consolidation point.
    """
    ctx.log_perf("before_save_hook")
    ctx.record("save_attempted")
    return True


def after_save_hook(ctx: BackofficeContext, saved_id: str) -> None:
    """
    Post-save actions: audit flush, WhatsApp trigger stub.
    """
    ctx.record("save_completed", {"saved_id": saved_id})
    ctx.log_perf("after_save_hook")

    if ctx.flags.get("enable_audit_log", True):
        ctx.flush_audit()

    if ctx.flags.get("enable_whatsapp_po", False):
        _trigger_whatsapp_notification(ctx, saved_id)


def _trigger_whatsapp_notification(ctx: BackofficeContext, saved_id: str) -> None:
    """Stub — wire to WhatsApp business API when ready."""
    log.info(f"[Kernel] WhatsApp notification stub — order {saved_id}")
