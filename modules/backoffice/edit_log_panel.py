"""
edit_log_panel.py — backoffice edit log using JSONL file (zero DB load).
LOG FILE:  logs/edit_log.jsonl   (one JSON object per line, append-only)
"""
import json
import streamlit as st
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List


def _log_path() -> Path:
    here = Path(__file__).resolve()
    for parent in [here.parent, here.parent.parent, here.parent.parent.parent]:
        if (parent / "app.py").exists():
            d = parent / "logs"; d.mkdir(exist_ok=True)
            return d / "edit_log.jsonl"
    Path("logs").mkdir(exist_ok=True)
    return Path("logs") / "edit_log.jsonl"


def log_edit(event, order_no, party="", by="system",
             category="STATUS", detail=None, remarks=""):
    """Append one JSON line. Never raises."""
    try:
        entry = {
            "ts":       datetime.now().isoformat(timespec="seconds"),
            "event":    event, "order_no": order_no,
            "party":    party or "", "by": by or "system",
            "category": category, "detail": detail or {}, "remarks": remarks or "",
        }
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass


def _read_log(days=30, order_no="") -> List[Dict]:
    path = _log_path()
    if not path.exists():
        return []
    cutoff = datetime.now() - timedelta(days=days)
    results = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line == "PLACEHOLDER":
                    continue
                try:
                    e = json.loads(line)
                    try:    ts = datetime.fromisoformat(e.get("ts", "")[:19])
                    except: ts = datetime.min
                    if ts < cutoff:
                        continue
                    if order_no and e.get("order_no") != order_no:
                        continue
                    e["_ts"] = ts
                    results.append(e)
                except Exception:
                    continue
    except Exception:
        return []
    results.sort(key=lambda x: x["_ts"], reverse=True)
    return results


_SC = {
    "PENDING": "#3b82f6", "CONFIRMED": "#6366f1", "IN_PRODUCTION": "#8b5cf6",
    "READY": "#10b981", "BILLED": "#059669", "DISPATCHED": "#0891b2",
    "DELIVERED": "#10b981", "CLOSED": "#334155", "CANCELLED": "#ef4444",
}
_SI = {
    "PENDING": "📥", "CONFIRMED": "✅", "IN_PRODUCTION": "⚙️", "READY": "📦",
    "BILLED": "🧾", "DISPATCHED": "🚚", "DELIVERED": "✅", "CLOSED": "🔒", "CANCELLED": "❌",
}
_CAT = {
    "STATUS":   ("#1e293b",   "#94a3b8"),
    "SAVE":     ("#3b82f620", "#3b82f6"),
    "BILLING":  ("#05966920", "#059669"),
    "DISPATCH": ("#0891b220", "#0891b2"),
    "ASSIGN":   ("#8b5cf620", "#8b5cf6"),
}


def _visuals(e: Dict):
    cat = e.get("category", "STATUS")
    d   = e.get("detail", {})
    if cat == "BILLING":
        dt  = d.get("doc_type", "DOC")
        dn  = d.get("doc_no", "")
        amt = d.get("amount", "")
        icon = "🧾" if dt == "INVOICE" else "📋"
        col  = "#059669" if dt == "INVOICE" else "#0891b2"
        lbl  = dt + " " + dn + ("  ₹{:,.2f}".format(float(amt)) if amt else "")
        return icon, col, lbl
    if cat == "DISPATCH":
        carrier = d.get("carrier", "")
        dno     = d.get("dispatch_no", "")
        p       = " (partial)" if d.get("is_partial") else ""
        lbl     = "Dispatched" + p + ": " + dno + (" via " + carrier if carrier else "")
        return "🚚", "#0891b2", lbl
    if cat == "ASSIGN":
        return "🎯", "#8b5cf6", d.get("summary", "Assignment updated")
    if cat == "SAVE" or "SAVE" in e.get("event", ""):
        frm = d.get("from_status", "")
        to  = d.get("to_status", "")
        lbl = ("Saved: " + frm + " → " + to) if (frm and to and frm != to) else "Order saved"
        return "💾", "#3b82f6", lbl
    frm  = d.get("from_status", "")
    to   = d.get("to_status", e.get("event", ""))
    col  = _SC.get(to, "#64748b")
    icon = _SI.get(to, "🔄")
    lbl  = ((frm + " → " + to) if frm and frm != to else to) or e.get("event", "").replace("_", " ").title()
    return icon, col, lbl


def _fmt(e: Dict) -> str:
    ts = e.get("_ts") or e.get("ts")
    if not ts:
        return "—"
    try:
        if isinstance(ts, datetime):
            return ts.strftime("%d %b %Y  %I:%M %p")
        s = str(ts)[:19].replace("T", " ")
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").strftime("%d %b %Y  %I:%M %p")
    except Exception:
        return str(ts)[:16]


def _actor_html(name):
    if not name or name in ("system", "System"):
        return "<span style='color:#475569;font-size:0.68rem'>🤖 system</span>"
    return ("<span style='background:#1e40af;color:#fff;padding:1px 7px;"
            "border-radius:8px;font-size:0.68rem;font-weight:700'>👤 " + name + "</span>")


def _cat_html(cat):
    bg, fg = _CAT.get(cat, ("#1e293b", "#94a3b8"))
    return ("<span style='background:" + bg + ";color:" + fg + ";padding:1px 6px;"
            "border-radius:5px;font-size:0.6rem'>" + cat + "</span>")


def _draw_row(e: Dict, show_order=False):
    icon, color, label = _visuals(e)
    ci, cm, cr = st.columns([0.4, 5, 2.5])
    with ci:
        st.markdown(
            "<div style='background:" + color + ";width:26px;height:26px;border-radius:50%;"
            "display:flex;align-items:center;justify-content:center;"
            "font-size:0.8rem;margin-top:6px'>" + icon + "</div>",
            unsafe_allow_html=True)
    with cm:
        order_line = (
            "<br><span style='color:#64748b;font-size:0.72rem'>"
            "📁 <b style='color:#94a3b8'>" + e.get("order_no", "") + "</b>"
            "  ·  " + e.get("party", "") + "</span>"
        ) if show_order else ""
        remark_line = (
            "<br><span style='color:#64748b;font-size:0.7rem;font-style:italic'>"
            "📝 " + str(e.get("remarks", ""))[:80] + "</span>"
        ) if e.get("remarks") else ""
        st.markdown(
            "<div style='padding:4px 0'>"
            "<span style='color:#e2e8f0;font-size:0.82rem;font-weight:600'>" + label + "</span>"
            " " + _cat_html(e.get("category", "STATUS")) + order_line + remark_line + "</div>",
            unsafe_allow_html=True)
    with cr:
        st.markdown(
            "<div style='text-align:right;padding:4px 0'>"
            "<div style='color:#475569;font-size:0.68rem'>🕐 " + _fmt(e) + "</div>"
            "<div style='margin-top:3px'>" + _actor_html(str(e.get("by") or "system")) + "</div></div>",
            unsafe_allow_html=True)
    st.markdown("<div style='border-bottom:1px solid #1e293b;margin:0'></div>", unsafe_allow_html=True)


def _stat_bar(entries):
    cats = [e.get("category", "") for e in entries]
    for col, (val, label, color) in zip(
        st.columns(4),
        [(len(entries),                                     "Total",    "#3b82f6"),
         (cats.count("STATUS") + cats.count("SAVE"),        "Saves",    "#8b5cf6"),
         (cats.count("BILLING"),                            "Billing",  "#059669"),
         (cats.count("DISPATCH"),                           "Dispatch", "#0891b2")]
    ):
        col.markdown(
            "<div style='background:#1e293b;border-radius:8px;padding:7px 10px;"
            "text-align:center;border-top:3px solid " + color + "'>"
            "<div style='color:" + color + ";font-size:1rem;font-weight:800'>" + str(val) + "</div>"
            "<div style='color:#64748b;font-size:0.62rem'>" + label + "</div></div>",
            unsafe_allow_html=True)


def render_edit_log(order: Dict) -> None:
    """Per-order tab — shows full trail for one order."""
    order_no = str(order.get("order_no") or "")
    st.markdown("### 📋 Edit Log")
    st.caption("Every save, status change, billing document, and dispatch for this order.")
    if not order_no:
        st.warning("Cannot load log — order_no missing.")
        return
    entries = _read_log(days=365, order_no=order_no)
    if not entries:
        st.info("No log entries yet. Events will appear here after the first save.")
        return
    _stat_bar(entries)
    actors = {e.get("by") for e in entries if e.get("by") not in ("system", "System", "", None)}
    if actors:
        st.markdown(
            "<div style='margin:8px 0 10px;font-size:0.72rem;color:#94a3b8'>👤 Edited by: "
            + "  ".join("<b style='color:#e2e8f0'>" + a + "</b>" for a in sorted(actors))
            + "</div>", unsafe_allow_html=True)
    st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)
    for e in entries:
        _draw_row(e, show_order=False)
    st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)
    with st.expander("📤 Export"):
        export = [{k: v for k, v in e.items() if k != "_ts"} for e in entries]
        st.download_button("⬇ Download JSON",
            data=json.dumps(export, indent=2, default=str),
            file_name="edit_log_" + order_no + ".json",
            mime="application/json", use_container_width=True)


def render_edit_log_page() -> None:
    """Full-page sidebar view — system-wide, filterable."""
    st.markdown("# 📋 Edit Log")
    st.caption("System-wide audit trail — every save, status change, billing document, and dispatch.")
    col_s, col_cat, col_who, col_days = st.columns([3, 2, 2, 1])
    with col_s:
        search   = st.text_input("🔍", placeholder="Order no / party name", label_visibility="collapsed")
    with col_cat:
        cat_filt = st.selectbox("Cat", ["All", "STATUS", "SAVE", "BILLING", "DISPATCH", "ASSIGN"], label_visibility="collapsed")
    with col_who:
        who_filt = st.text_input("👤", placeholder="Filter by user", label_visibility="collapsed")
    with col_days:
        days     = st.selectbox("Days", [7, 14, 30, 90], label_visibility="collapsed")
    st.markdown("---")
    entries = _read_log(days=days)
    if search:
        s = search.lower()
        entries = [e for e in entries if s in e.get("order_no", "").lower() or s in e.get("party", "").lower()]
    if cat_filt != "All":
        entries = [e for e in entries if e.get("category") == cat_filt]
    if who_filt:
        w = who_filt.lower()
        entries = [e for e in entries if w in (e.get("by") or "").lower()]
    _stat_bar(entries)
    st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)
    if not entries:
        st.info("No events found for the selected filters.")
        return
    for e in entries:
        _draw_row(e, show_order=True)
    st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)
    export = [{k: v for k, v in e.items() if k != "_ts"} for e in entries]
    st.download_button(
        "⬇ Export " + str(len(export)) + " events as JSON",
        data=json.dumps(export, indent=2, default=str),
        file_name="edit_log_" + str(days) + "d.json",
        mime="application/json", use_container_width=True)
