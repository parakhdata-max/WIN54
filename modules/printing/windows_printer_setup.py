"""
Windows-only printer default setup for the shop LAN computer.

This does not replace HTML/browser printing. It only prepares local Windows
printer defaults so internal direct prints land on the expected hardware/media.
"""

from __future__ import annotations

from dataclasses import dataclass

from modules.printing.internal_print_config import (
    CANON_DOCUMENT_PRINTER,
    CANON_DEFAULT_PAPER,
    TSC_LABEL_H_MM,
    TSC_LABEL_PRINTER,
    TSC_LABEL_W_MM,
)


@dataclass(frozen=True)
class PrinterSetupResult:
    printer: str
    ok: bool
    message: str


def ensure_windows_print_defaults() -> list[PrinterSetupResult]:
    """Apply local Windows defaults for Canon A5 and TSC 75x50 where possible."""
    results: list[PrinterSetupResult] = []
    results.append(_set_canon_default_a5(CANON_DOCUMENT_PRINTER))
    results.append(_set_tsc_75x50_form(TSC_LABEL_PRINTER))
    return results


def _set_canon_default_a5(printer_name: str) -> PrinterSetupResult:
    try:
        import subprocess

        cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            (
                "Set-PrintConfiguration "
                f"-PrinterName '{printer_name}' "
                f"-PaperSize {CANON_DEFAULT_PAPER}"
            ),
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return PrinterSetupResult(printer_name, True, f"Default paper set to {CANON_DEFAULT_PAPER}")
    except Exception as exc:
        return PrinterSetupResult(printer_name, False, f"Could not set Canon paper: {exc}")


def _set_tsc_75x50_form(printer_name: str) -> PrinterSetupResult:
    """Register and apply a 75x50mm form to a TSC-style Windows printer."""
    form_name = f"DV_{TSC_LABEL_W_MM}x{TSC_LABEL_H_MM}"
    try:
        import win32print

        # FORM_INFO_1 units are thousandths of a millimetre.
        width = int(TSC_LABEL_W_MM * 1000)
        height = int(TSC_LABEL_H_MM * 1000)
        form_info = {
            "Flags": 0,
            "Name": form_name,
            "Size": {"cx": width, "cy": height},
            "ImageableArea": {"left": 0, "top": 0, "right": width, "bottom": height},
        }

        handle = win32print.OpenPrinter(printer_name)
        try:
            try:
                win32print.AddForm(handle, form_info)
            except Exception:
                try:
                    win32print.SetForm(handle, form_name, form_info)
                except Exception:
                    pass

            props = win32print.GetPrinter(handle, 2)
            devmode = props.get("pDevMode")
            if devmode is not None:
                # dmPaperWidth/Length are tenths of a millimetre.
                devmode.FormName = form_name
                devmode.PaperSize = 256  # DMPAPER_USER
                devmode.PaperWidth = int(TSC_LABEL_W_MM * 10)
                devmode.PaperLength = int(TSC_LABEL_H_MM * 10)
                devmode.Fields |= 0x10000 | 0x2 | 0x4 | 0x8
                props["pDevMode"] = devmode
                win32print.SetPrinter(handle, 2, props, 0)
        finally:
            win32print.ClosePrinter(handle)

        return PrinterSetupResult(printer_name, True, f"Registered/applied form {form_name}")
    except Exception as exc:
        return PrinterSetupResult(printer_name, False, f"Could not set TSC form {form_name}: {exc}")


if __name__ == "__main__":
    for result in ensure_windows_print_defaults():
        status = "OK" if result.ok else "WARN"
        print(f"{status}: {result.printer}: {result.message}")
