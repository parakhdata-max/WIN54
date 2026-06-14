"""
Local workstation printer configuration.

This is intentionally local-file based, not DB based. Different LAN counters can
use different physical printers while sharing the same ERP database.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from datetime import datetime

from modules.printing.internal_print_config import (
    CANON_DOCUMENT_PRINTER,
    EVOLIS_CARD_PRINTER,
    TSC_LABEL_PRINTER,
)


CONFIG_DIR = Path(os.getenv("DV_LOCAL_CONFIG_DIR", Path.home() / ".dv_erp"))
CONFIG_FILE = CONFIG_DIR / "printer_settings.json"
AUDIT_FILE = CONFIG_DIR / "printer_settings_audit.log"

DEFAULTS = {
    "tsc_label_printer": TSC_LABEL_PRINTER,
    "document_printer": CANON_DOCUMENT_PRINTER,
    "card_printer": EVOLIS_CARD_PRINTER,
    "html_fallback": True,
}


def list_windows_printers() -> list[str]:
    try:
        import win32print
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        return sorted({p[2] for p in win32print.EnumPrinters(flags) if p and p[2]})
    except Exception:
        return []


_STATUS_FLAGS = {
    0x00000001: "PAUSED",
    0x00000002: "ERROR",
    0x00000004: "PENDING_DELETION",
    0x00000008: "PAPER_JAM",
    0x00000010: "PAPER_OUT",
    0x00000020: "MANUAL_FEED",
    0x00000040: "PAPER_PROBLEM",
    0x00000080: "OFFLINE",
    0x00000100: "IO_ACTIVE",
    0x00000200: "BUSY",
    0x00000400: "PRINTING",
    0x00000800: "OUTPUT_BIN_FULL",
    0x00001000: "NOT_AVAILABLE",
    0x00002000: "WAITING",
    0x00004000: "PROCESSING",
    0x00008000: "INITIALIZING",
    0x00010000: "WARMING_UP",
    0x00020000: "TONER_LOW",
    0x00040000: "NO_TONER",
    0x00080000: "PAGE_PUNT",
    0x00100000: "USER_INTERVENTION",
    0x00200000: "OUT_OF_MEMORY",
    0x00400000: "DOOR_OPEN",
    0x00800000: "SERVER_UNKNOWN",
    0x01000000: "POWER_SAVE",
}

_BAD_STATUS = {
    "PAUSED",
    "ERROR",
    "PAPER_JAM",
    "PAPER_OUT",
    "PAPER_PROBLEM",
    "OFFLINE",
    "NOT_AVAILABLE",
    "NO_TONER",
    "USER_INTERVENTION",
    "DOOR_OPEN",
    "SERVER_UNKNOWN",
}


def printer_status(printer_name: str) -> dict:
    """Return Windows queue readiness for a printer."""
    if not printer_name:
        return {"name": "", "exists": False, "ready": False, "status": "NO_SELECTION", "message": "No printer selected"}
    try:
        import win32print
        names = list_windows_printers()
        if printer_name not in names:
            return {
                "name": printer_name,
                "exists": False,
                "ready": False,
                "status": "NOT_FOUND",
                "message": f"Printer not found on this computer: {printer_name}",
            }
        handle = win32print.OpenPrinter(printer_name)
        try:
            info = win32print.GetPrinter(handle, 2)
        finally:
            win32print.ClosePrinter(handle)

        raw_status = int(info.get("Status") or 0)
        attrs = int(info.get("Attributes") or 0)
        jobs = int(info.get("cJobs") or 0)
        flags = [label for bit, label in _STATUS_FLAGS.items() if raw_status & bit]
        if attrs & getattr(win32print, "PRINTER_ATTRIBUTE_WORK_OFFLINE", 0x00000400):
            flags.append("WORK_OFFLINE")
        bad = bool(set(flags) & (_BAD_STATUS | {"WORK_OFFLINE"}))
        status = "READY" if not bad else ", ".join(flags)
        if raw_status == 0 and not bad:
            status = "READY"
        message = f"{status}" + (f" · {jobs} queued" if jobs else "")
        return {
            "name": printer_name,
            "exists": True,
            "ready": not bad,
            "status": status,
            "flags": flags,
            "jobs": jobs,
            "message": message,
        }
    except Exception as exc:
        return {
            "name": printer_name,
            "exists": False,
            "ready": False,
            "status": "CHECK_FAILED",
            "message": f"Printer status check failed: {exc}",
        }


def load_printer_settings() -> dict:
    data = dict(DEFAULTS)
    try:
        if CONFIG_FILE.exists():
            saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                data.update({k: v for k, v in saved.items() if v is not None})
    except Exception:
        pass

    data["tsc_label_printer"] = os.getenv("DV_TSC_PRINTER") or data.get("tsc_label_printer") or TSC_LABEL_PRINTER
    data["document_printer"] = os.getenv("DV_CANON_PRINTER") or data.get("document_printer") or CANON_DOCUMENT_PRINTER
    data["card_printer"] = os.getenv("DV_EVOLIS_PRINTER") or data.get("card_printer") or EVOLIS_CARD_PRINTER
    return data


def save_printer_settings(settings: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    old = dict(load_printer_settings())
    data = dict(old)
    data.update(settings or {})
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    _append_audit("SAVE", old, data)


def save_printer_profile(profile_name: str, settings: dict) -> None:
    profile_name = str(profile_name or "").strip()
    if not profile_name:
        return
    data = dict(load_printer_settings())
    profiles = data.get("profiles") if isinstance(data.get("profiles"), dict) else {}
    profiles[profile_name] = {
        "tsc_label_printer": settings.get("tsc_label_printer", ""),
        "document_printer": settings.get("document_printer", ""),
        "card_printer": settings.get("card_printer", ""),
    }
    data["profiles"] = profiles
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    _append_audit("SAVE_PROFILE", {}, {"profile": profile_name, **profiles[profile_name]})


def apply_printer_profile(profile_name: str) -> bool:
    data = dict(load_printer_settings())
    profiles = data.get("profiles") if isinstance(data.get("profiles"), dict) else {}
    profile = profiles.get(str(profile_name or "").strip())
    if not isinstance(profile, dict):
        return False
    save_printer_settings({
        "tsc_label_printer": profile.get("tsc_label_printer", ""),
        "document_printer": profile.get("document_printer", ""),
        "card_printer": profile.get("card_printer", ""),
    })
    return True


def _append_audit(action: str, old: dict, new: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        rec = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "host": socket.gethostname(),
            "user": os.getenv("USERNAME") or os.getenv("USER") or "",
            "action": action,
            "old": old,
            "new": new,
        }
        with AUDIT_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=True) + "\n")
    except Exception:
        pass


def get_printer(role: str) -> str:
    settings = load_printer_settings()
    key = {
        "tsc": "tsc_label_printer",
        "label": "tsc_label_printer",
        "document": "document_printer",
        "canon": "document_printer",
        "card": "card_printer",
        "cr80": "card_printer",
    }.get(str(role or "").lower(), str(role or ""))
    return str(settings.get(key) or DEFAULTS.get(key) or "").strip()


def printer_exists(printer_name: str) -> tuple[bool, str]:
    status = printer_status(printer_name)
    return bool(status.get("exists") and status.get("ready")), str(status.get("message") or status.get("status"))
