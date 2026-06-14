from __future__ import annotations

import datetime as _dt
import time
from contextlib import contextmanager
from collections import deque
from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class ObserverEvent:
    at: str
    page: str
    event_type: str
    elapsed_sec: float = 0.0
    message: str = ""
    relief_action: str = ""


_EVENTS: deque[ObserverEvent] = deque(maxlen=200)
_SLOW_COUNTS: dict[str, list[float]] = {}
_ERROR_COUNTS: dict[str, list[float]] = {}
_ACTIVE_TRACE: dict[str, Any] | None = None


DEFAULT_SLOW_THRESHOLD_SEC = 8.0
REPEATED_WINDOW_SEC = 300.0
SLOW_STEP_SEC = 0.75


def _now_iso() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _prune(values: list[float], window_sec: float = REPEATED_WINDOW_SEC) -> list[float]:
    cutoff = time.time() - window_sec
    return [v for v in values if v >= cutoff]


def _note(title: str, message: str, action: str, context: str, technical: str = "") -> str:
    try:
        from modules.core.operator_alerts import OperatorAlert, record_issue_comment
        return record_issue_comment(
            OperatorAlert(title=title, message=message, action=action, technical=technical),
            context=context,
        )
    except Exception:
        return ""


def start_perf_trace(page: str) -> None:
    """Begin a lightweight per-render timing trace."""
    global _ACTIVE_TRACE
    _ACTIVE_TRACE = {
        "page": str(page or "unknown"),
        "started_at": time.perf_counter(),
        "steps": [],
    }


def add_perf_step(label: str, elapsed_sec: float, category: str = "work", detail: str = "") -> None:
    """Record one timed bucket for the active page render."""
    try:
        if _ACTIVE_TRACE is None:
            return
        elapsed = float(elapsed_sec or 0)
        _ACTIVE_TRACE["steps"].append({
            "label": str(label or category or "step")[:120],
            "category": str(category or "work")[:40],
            "elapsed_sec": round(elapsed, 3),
            "detail": str(detail or "")[:240],
        })
    except Exception:
        pass


@contextmanager
def perf_step(label: str, category: str = "work", detail: str = ""):
    """Context manager for timing a named render step."""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        add_perf_step(label, time.perf_counter() - t0, category=category, detail=detail)


def finish_perf_trace(total_elapsed_sec: float | None = None) -> dict[str, Any]:
    """End the active render trace and return a compact performance summary."""
    global _ACTIVE_TRACE
    trace = _ACTIVE_TRACE or {}
    _ACTIVE_TRACE = None
    steps = list(trace.get("steps") or [])
    total = float(total_elapsed_sec or 0)
    if total <= 0 and trace.get("started_at"):
        total = time.perf_counter() - float(trace["started_at"])

    by_category: dict[str, float] = {}
    for s in steps:
        cat = str(s.get("category") or "work")
        by_category[cat] = by_category.get(cat, 0.0) + float(s.get("elapsed_sec") or 0)

    top_steps = sorted(
        steps,
        key=lambda s: float(s.get("elapsed_sec") or 0),
        reverse=True,
    )[:8]

    explained = sum(float(v or 0) for v in by_category.values())
    render_other = max(0.0, total - explained)
    if render_other >= 0.05:
        by_category["render/ui/other"] = by_category.get("render/ui/other", 0.0) + render_other

    probable = "render/ui/other"
    if by_category:
        probable = max(by_category.items(), key=lambda kv: kv[1])[0]

    return {
        "total_elapsed_sec": round(total, 3),
        "by_category": {k: round(v, 3) for k, v in sorted(by_category.items(), key=lambda kv: kv[1], reverse=True)},
        "top_steps": top_steps,
        "probable_cause": probable,
    }


def _format_perf_summary(perf: dict[str, Any] | None, threshold: float) -> tuple[str, str]:
    if not perf:
        return (
            "Likely cause: no detailed timing buckets were captured for this page.",
            "performance_trace=none",
        )
    total = float(perf.get("total_elapsed_sec") or 0)
    by_cat = perf.get("by_category") or {}
    top = perf.get("top_steps") or []
    probable = str(perf.get("probable_cause") or "unknown")

    cause_lines = [
        f"Probable cause: {probable} took the largest share of render time.",
        f"Timing: total {total:.2f}s, threshold {threshold:.2f}s.",
    ]
    if by_cat:
        cause_lines.append(
            "Inclusive buckets: "
            + ", ".join(f"{k} {float(v):.2f}s" for k, v in list(by_cat.items())[:6])
        )
    if top:
        cause_lines.append(
            "Top slow steps: "
            + "; ".join(
                f"{s.get('label')} [{s.get('category')}] {float(s.get('elapsed_sec') or 0):.2f}s"
                for s in top[:5]
            )
        )

    technical = [
        f"performance_trace_total={total:.3f}s",
        f"performance_trace_probable_cause={probable}",
        "performance_trace_inclusive_buckets="
        + ", ".join(f"{k}:{float(v):.3f}s" for k, v in by_cat.items()),
    ]
    if top:
        technical.append("performance_trace_top_steps=")
        technical.extend(
            f"- {s.get('label')} | {s.get('category')} | {float(s.get('elapsed_sec') or 0):.3f}s | {s.get('detail') or ''}"
            for s in top
        )
    return "\n".join(cause_lines), "\n".join(technical)


def record_page_render(page: str, elapsed_sec: float, threshold_sec: float | None = None, perf: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Observe a completed page render. If slow/repeated-slow, write issue notes.
    Returns metadata for UI warnings.
    """
    threshold = float(threshold_sec or DEFAULT_SLOW_THRESHOLD_SEC)
    page = str(page or "unknown")
    event = ObserverEvent(
        at=_now_iso(),
        page=page,
        event_type="render",
        elapsed_sec=round(float(elapsed_sec or 0), 3),
    )
    _EVENTS.append(event)

    result = {"slow": False, "repeated": False, "note_path": "", "relief_action": "", "probable_cause": ""}
    if elapsed_sec <= threshold:
        return result

    result["slow"] = True
    now = time.time()
    hits = _prune(_SLOW_COUNTS.get(page, []))
    hits.append(now)
    _SLOW_COUNTS[page] = hits
    result["repeated"] = len(hits) >= 2

    perf_message, perf_technical = _format_perf_summary(perf, threshold)
    result["probable_cause"] = str((perf or {}).get("probable_cause") or "")
    action = (
        "The observer recorded the probable slow bucket. First optimise the largest bucket "
        "(usually DB query, order hydration, product master load, or render/UI work). "
        "Use narrower filters or recent/active view while patching."
    )
    technical = (
        f"page={page} elapsed_sec={elapsed_sec:.3f} threshold_sec={threshold:.3f} "
        f"repeated_count_5min={len(hits)}\n{perf_technical}"
    )
    note_path = _note(
        "Screen became slow / possible hang",
        f"{page} took {elapsed_sec:.1f}s to render.\n{perf_message}",
        action,
        context=f"observer:slow:{page}",
        technical=technical,
    )
    result["note_path"] = note_path

    if len(hits) >= 3:
        # Safe runtime relief only: clear Streamlit cache. No data/code changes.
        try:
            import streamlit as st
            st.cache_data.clear()
            result["relief_action"] = "Cleared Streamlit data cache after repeated slow renders."
            _EVENTS.append(ObserverEvent(
                at=_now_iso(),
                page=page,
                event_type="relief",
                elapsed_sec=0.0,
                message="repeated slow render",
                relief_action=result["relief_action"],
            ))
        except Exception:
            pass
    return result


def record_page_error(page: str, exc: Exception) -> dict[str, Any]:
    """
    Observe a page error. Repeated errors trigger safe cache clearing.
    """
    page = str(page or "unknown")
    msg = str(exc or "")
    _EVENTS.append(ObserverEvent(
        at=_now_iso(),
        page=page,
        event_type="error",
        message=msg[:300],
    ))
    now = time.time()
    hits = _prune(_ERROR_COUNTS.get(page, []))
    hits.append(now)
    _ERROR_COUNTS[page] = hits

    result = {"repeated": len(hits) >= 2, "relief_action": ""}
    if len(hits) >= 2:
        try:
            import streamlit as st
            st.cache_data.clear()
            result["relief_action"] = "Cleared Streamlit data cache after repeated page errors."
            _EVENTS.append(ObserverEvent(
                at=_now_iso(),
                page=page,
                event_type="relief",
                message="repeated page error",
                relief_action=result["relief_action"],
            ))
        except Exception:
            pass
    return result


def get_observer_summary() -> dict[str, Any]:
    events = list(_EVENTS)
    slow_pages = {}
    error_pages = {}
    for e in events:
        if e.event_type == "render" and e.elapsed_sec > DEFAULT_SLOW_THRESHOLD_SEC:
            slow_pages[e.page] = slow_pages.get(e.page, 0) + 1
        if e.event_type == "error":
            error_pages[e.page] = error_pages.get(e.page, 0) + 1
    return {
        "events": [asdict(e) for e in events[-50:]],
        "slow_pages": slow_pages,
        "error_pages": error_pages,
        "recent_relief": [asdict(e) for e in events if e.event_type == "relief"][-10:],
    }
