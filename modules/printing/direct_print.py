"""
Direct/local print helpers.

Direct print is best-effort for local LAN machines. HTML remains the standby
because browser-generated documents cannot always be spooled silently by every
Windows handler.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from modules.printing.internal_print_config import CANON_DOCUMENT_PRINTER
from modules.printing.printer_config import get_printer, printer_status


def printer_exists(printer_name: str) -> tuple[bool, str]:
    """Return whether a Windows printer is visible and ready on this workstation."""
    status = printer_status(printer_name)
    return bool(status.get("exists") and status.get("ready")), str(status.get("message") or status.get("status"))


def spool_html_to_printer(
    html: str,
    printer_name: str | None = None,
    *,
    job_name: str = "DV ERP Print",
) -> tuple[bool, str]:
    """
    Try to send HTML to the local document printer without showing print dialog.

    Uses Windows ShellExecute 'printto'. This depends on the workstation's HTML
    file association supporting printto. If it fails, caller should open the
    HTML fallback window.
    """
    printer_name = printer_name or get_printer("document") or CANON_DOCUMENT_PRINTER
    ok, msg = printer_exists(printer_name)
    if not ok:
        return False, msg

    try:
        import win32api
        spool_dir = Path(tempfile.gettempdir()) / "dv_erp_print_spool"
        spool_dir.mkdir(parents=True, exist_ok=True)
        path = spool_dir / (safe_filename(job_name) + ".html")
        path.write_text(html, encoding="utf-8")
        win32api.ShellExecute(0, "printto", str(path), f'"{printer_name}"', str(spool_dir), 0)
        return True, f"Sent to {printer_name}"
    except Exception as exc:
        return False, f"Direct HTML print failed: {exc}"


def safe_filename(value: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(value or "print"))
    text = text.strip("_") or "print"
    return text[:80]
