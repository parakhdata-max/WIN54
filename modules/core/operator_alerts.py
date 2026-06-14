from __future__ import annotations

import re
import traceback
import datetime as _dt
import hashlib
import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class OperatorAlert:
    title: str
    message: str
    action: str
    severity: str = "error"
    technical: str = ""

    def as_text(self) -> str:
        return f"{self.title}: {self.message} What to do: {self.action}"


_RECENT_ISSUES: dict[str, _dt.datetime] = {}


def _issue_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "issue_notes"


def collect_system_snapshot() -> str:
    """Best-effort CMD/process/system snapshot for issue notes."""
    lines = [
        f"Timestamp: {_dt.datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Machine: {platform.node()}",
        f"OS: {platform.platform()}",
        f"Python: {sys.version.split()[0]}",
        f"PID: {os.getpid()}",
        f"CWD: {os.getcwd()}",
        f"Executable: {sys.executable}",
        f"Command: {' '.join(sys.argv)}",
    ]
    try:
        import psutil  # type: ignore
        proc = psutil.Process(os.getpid())
        mem = proc.memory_info()
        lines.extend([
            f"Process status: {proc.status()}",
            f"Process CPU%: {proc.cpu_percent(interval=0.05)}",
            f"Process memory RSS MB: {mem.rss / (1024 * 1024):.1f}",
            f"System CPU%: {psutil.cpu_percent(interval=0.05)}",
            f"System memory%: {psutil.virtual_memory().percent}",
        ])
        py_procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info", "cmdline"]):
            try:
                name = str(p.info.get("name") or "")
                cmd = " ".join(p.info.get("cmdline") or [])
                if "python" in name.lower() or "streamlit" in cmd.lower():
                    rss = p.info.get("memory_info")
                    rss_mb = (rss.rss / (1024 * 1024)) if rss else 0
                    py_procs.append(
                        f"- PID {p.info.get('pid')} {name} RSS {rss_mb:.1f} MB CMD {cmd[:220]}"
                    )
            except Exception:
                pass
        if py_procs:
            lines.append("Python/Streamlit processes:")
            lines.extend(py_procs[:12])
    except Exception as exc:
        lines.append(f"psutil snapshot unavailable: {exc}")
    return "\n".join(lines)


def record_issue_comment(alert: OperatorAlert, context: str = "") -> str:
    """
    Append a human-readable issue note into WIN54/issue_notes.
    This is intentionally file-based so field problems discovered during live
    work become a simple coding punch-list later.
    """
    now = _dt.datetime.now()
    tech = alert.technical or ""
    fp_src = f"{context}|{alert.title}|{alert.message}|{tech[:500]}"
    fp = hashlib.sha1(fp_src.encode("utf-8", "ignore")).hexdigest()
    last = _RECENT_ISSUES.get(fp)
    if last and (now - last).total_seconds() < 5:
        return ""
    _RECENT_ISSUES[fp] = now

    folder = _issue_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{now:%Y-%m-%d}_issues.md"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    next_no = len(re.findall(r"^## Issue \d+", existing, flags=re.M)) + 1
    block = (
        f"\n## Issue {next_no} - {now:%H:%M:%S}\n"
        f"- Context: {context or 'unknown'}\n"
        f"- Status: BLOCKED_BY_SYSTEM\n"
        f"- What happened: {alert.title} - {alert.message}\n"
        f"- What to do: {alert.action}\n"
    )
    if tech:
        block += "\nTechnical detail:\n\n```text\n" + tech[:4000] + "\n```\n"
    block += "\nSystem snapshot:\n\n```text\n" + collect_system_snapshot()[:5000] + "\n```\n"
    path.write_text(existing + block, encoding="utf-8")

    readme = folder / "README.md"
    if not readme.exists():
        readme.write_text(
            "# WIN54 Issue Notes\n\n"
            "Auto-recorded operator/DB blocks. Use these notes to find repeated "
            "field, schema, GST, date, duplicate and transaction issues that "
            "need a proper UI validation or code fix.\n",
            encoding="utf-8",
        )
    return str(path)


def _text(exc: Any) -> str:
    if exc is None:
        return ""
    parts = [str(exc)]
    original = getattr(exc, "original", None)
    if original is not None:
        parts.append(str(original))
    cause = getattr(exc, "__cause__", None)
    if cause is not None:
        parts.append(str(cause))
    return "\n".join(p for p in parts if p)


def _constraint_name(msg: str) -> str:
    m = re.search(r'constraint "([^"]+)"', msg, flags=re.I)
    return m.group(1) if m else ""


def build_operator_alert(exc: Any, context: str = "") -> OperatorAlert:
    msg = _text(exc)
    low = msg.lower()
    constraint = _constraint_name(msg)

    if "current transaction is aborted" in low:
        return OperatorAlert(
            "Save blocked by an earlier database error",
            "One field failed first, so Postgres rejected the remaining save steps.",
            "Do not retry blindly. Scroll up or check the terminal for the first error above this one, correct that field, then save again. If the page is stuck, refresh once.",
            technical=msg,
        )

    if "future" in low and ("date" in low or "post-dated" in low):
        return OperatorAlert(
            "Future date not allowed",
            msg.splitlines()[0],
            "Use today or an earlier date. Only provisional advance cheques can be post-dated.",
            technical=msg,
        )

    if "duplicate key value" in low or "already exists" in low or "unique constraint" in low:
        return OperatorAlert(
            "Duplicate document/value",
            "The same number or unique value is already saved in the database.",
            "Search the existing order/invoice/payment first. If this is an edit, use the edit/rollback correction path instead of creating a new record.",
            technical=msg,
        )

    if "not-null constraint" in low or "null value in column" in low:
        col = ""
        m = re.search(r'column "([^"]+)"', msg, flags=re.I)
        if m:
            col = m.group(1)
        if "gst" in low:
            return OperatorAlert(
                "GST detail missing",
                f"Required GST field is blank{f' ({col})' if col else ''}.",
                "Go back to the product/line edit and fill GST percent/rate. If the product master has no GST, update Product & Inventory first, then reopen this order.",
                technical=msg,
            )
        return OperatorAlert(
            "Required field missing",
            f"A required field was blank{f' ({col})' if col else ''}.",
            "Fill the highlighted/required field on the form, then save again. If this came from a loader, update the missing column in the source file.",
            technical=msg,
        )

    if "check constraint" in low:
        return OperatorAlert(
            "Value rejected by business rule",
            f"The database rejected a value that does not satisfy rule {constraint or 'a check constraint'}.",
            "Review quantity, GST, date, payment amount and status fields. Correct the invalid value before saving.",
            technical=msg,
        )

    if "foreign key constraint" in low or "violates foreign key" in low:
        return OperatorAlert(
            "Linked record missing",
            "This save refers to a party/product/order/invoice that is not available or was removed.",
            "Re-select the party/product/order from the dropdown, then save again. If it was deleted, restore or create the master record first.",
            technical=msg,
        )

    if "invalid input syntax" in low:
        if "uuid" in low:
            action = "Re-select the record from the dropdown instead of typing/copying an internal id."
        elif "date" in low:
            action = "Enter the date in the form date picker, or use DD/MM/YYYY."
        elif "numeric" in low or "integer" in low:
            action = "Enter numbers only; remove symbols, spaces or text."
        else:
            action = "Correct the typed value and save again."
        return OperatorAlert(
            "Invalid form value",
            "A typed value could not be converted to the expected database type.",
            action,
            technical=msg,
        )

    if "column" in low and "does not exist" in low:
        return OperatorAlert(
            "Database/schema mismatch",
            "The code is asking for a column that is not present in this database.",
            "Do not continue posting new records from this screen. Run the pending migration or deploy the matching file set, then restart Streamlit.",
            technical=msg,
        )

    if "dict is not a sequence" in low or "argument formats can't be mixed" in low or "syntax error at or near" in low:
        return OperatorAlert(
            "Internal query format issue",
            "A report/query was built incorrectly, so the database refused to run it.",
            "No data was saved. Share this alert with the developer and avoid this button until patched.",
            technical=msg,
        )

    if "permission denied" in low or "readonly" in low or "read-only" in low:
        return OperatorAlert(
            "Permission blocked",
            "This action is not allowed for the current user or database mode.",
            "Use an authorised login or switch out of read-only/test mode before saving.",
            technical=msg,
        )

    if "gst" in low:
        return OperatorAlert(
            "GST validation failed",
            "GST values are incomplete or inconsistent.",
            "Return to the line/product edit, fill GST percent and re-run pricing before challan/invoice/register posting.",
            technical=msg,
        )

    return OperatorAlert(
        f"{context} blocked" if context else "Action blocked",
        msg.splitlines()[0] if msg else "The system rejected this action.",
        "Review the form fields and try again. If this repeats, open the technical details and share them for patching.",
        technical=msg,
    )


def friendly_error_text(exc: Any, context: str = "") -> str:
    return build_operator_alert(exc, context=context).as_text()


def render_operator_alert(exc: Any, context: str = "", show_traceback: bool = False) -> None:
    try:
        import streamlit as st
    except Exception:
        return
    alert = build_operator_alert(exc, context=context)
    note_path = ""
    try:
        note_path = record_issue_comment(alert, context=context)
    except Exception:
        note_path = ""
    box = st.error if alert.severity == "error" else st.warning
    box(
        f"**{alert.title}**\n\n"
        f"{alert.message}\n\n"
        f"**What to do:** {alert.action}"
    )
    if note_path:
        st.caption(f"Issue note saved: {note_path}")
    tech = alert.technical or str(exc or "")
    if show_traceback:
        tb = traceback.format_exc()
        if tb and tb.strip() != "NoneType: None":
            tech = f"{tech}\n\n{tb}"
    if tech:
        with st.expander("Technical details for support", expanded=False):
            st.code(tech[:8000])


def block_with_message(title: str, message: str, action: str, technical: str = "") -> None:
    """Use from forms when a known validation rule blocks save."""
    render_operator_alert(
        OperatorAlert(title=title, message=message, action=action, technical=technical),
        context=title,
        show_traceback=False,
    )


def record_slow_issue(context: str, elapsed_seconds: float, threshold_seconds: float) -> str:
    alert = OperatorAlert(
        title="Screen became slow / possible hang",
        message=(
            f"{context or 'Screen'} took {elapsed_seconds:.1f}s to render "
            f"(threshold {threshold_seconds:.1f}s)."
        ),
        action=(
            "Check the saved system snapshot, recent DB query, and this page's filters. "
            "If repeated, optimise the loader/query or add pagination/cache."
        ),
        technical=f"slow_render context={context} elapsed={elapsed_seconds:.3f}s threshold={threshold_seconds:.3f}s",
    )
    return record_issue_comment(alert, context=f"slow_render:{context}")
