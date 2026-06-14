from __future__ import annotations

import datetime as _dt
from typing import Any


def coerce_date(value: Any) -> _dt.date | None:
    """Return a date for common UI/DB values, or None when blank/unparseable."""
    if value is None or value == "":
        return None
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    txt = str(value).strip()
    if not txt:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d"):
        try:
            return _dt.datetime.strptime(txt[:10], fmt).date()
        except Exception:
            pass
    try:
        return _dt.date.fromisoformat(txt[:10])
    except Exception:
        return None


def is_future_date(value: Any, today: _dt.date | None = None) -> bool:
    dt = coerce_date(value)
    return bool(dt and dt > (today or _dt.date.today()))


def future_date_message(label: str, value: Any) -> str:
    dt = coerce_date(value)
    shown = dt.isoformat() if dt else str(value or "")
    return f"{label} cannot be in the future ({shown})."


def validate_not_future(value: Any, label: str) -> tuple[bool, str]:
    if is_future_date(value):
        return False, future_date_message(label, value)
    return True, ""


def is_provisional_advance_cheque(
    *,
    payment_type: str = "",
    payment_mode: str = "",
    method: str = "",
    remarks: str = "",
    reference_no: str = "",
    explicit: bool = False,
) -> bool:
    """The only allowed future-dated payment is a marked provisional advance cheque."""
    mode = f"{payment_mode} {method}".upper()
    text = f"{remarks} {reference_no}".upper()
    return (
        explicit
        or (
            "ADVANCE" in str(payment_type or "").upper()
            and "CHEQUE" in mode
            and any(marker in text for marker in ("PROVISIONAL", "PDC", "POST DATED", "POST-DATED"))
        )
    )


def validate_payment_date(
    value: Any,
    *,
    payment_type: str = "",
    payment_mode: str = "",
    method: str = "",
    remarks: str = "",
    reference_no: str = "",
    allow_provisional_advance_cheque: bool = False,
) -> tuple[bool, str]:
    if not is_future_date(value):
        return True, ""
    if is_provisional_advance_cheque(
        payment_type=payment_type,
        payment_mode=payment_mode,
        method=method,
        remarks=remarks,
        reference_no=reference_no,
        explicit=allow_provisional_advance_cheque,
    ):
        return True, ""
    return False, future_date_message(
        "Payment date",
        value,
    ) + " Only provisional advance cheques may be post-dated."
