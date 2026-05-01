"""
fy_service.py
Financial year helpers.

FIX 1: All FY lookups use record_date, not system date.
       get_current_fy() is only used for UI display — never for doc numbering.
"""
import datetime
from modules.sql_adapter import run_query, run_write


def get_fy_label(d: datetime.date = None) -> str:
    """Full label: '2025-26'. Uses today if d is None."""
    d = d or datetime.date.today()
    if d.month >= 4:
        return f"{d.year}-{str(d.year + 1)[-2:]}"
    return f"{d.year - 1}-{str(d.year)[-2:]}"


def get_fy_short(d: datetime.date = None) -> str:
    """Short code: '2526'. FIX 1: pass the record date, not today."""
    label = get_fy_label(d)
    parts = label.split("-")
    return parts[0][-2:] + parts[1]


def get_fy_for_date(d: datetime.date) -> dict:
    """
    FIX 1: Canonical FY lookup — always by date.
    Use this for invoice numbering, doc creation, lock checks.
    Never use get_current_fy() for these purposes.
    """
    rows = run_query("""
        SELECT id, fy, fy_short, start_date, end_date, is_closed,
               closed_at, closed_by
        FROM financial_years
        WHERE %s BETWEEN start_date AND end_date
        LIMIT 1
    """, (d,)) or []

    if rows:
        return rows[0]

    # Synthesise if not yet seeded
    label = get_fy_label(d)
    short = get_fy_short(d)
    yr    = d.year if d.month >= 4 else d.year - 1
    return {
        "id": None, "fy": label, "fy_short": short,
        "start_date": datetime.date(yr, 4, 1),
        "end_date":   datetime.date(yr + 1, 3, 31),
        "is_closed":  False,
        "closed_at":  None, "closed_by": None,
    }


def get_current_fy() -> dict:
    """
    FY for TODAY — for UI display / dashboard only.
    Do NOT use this for doc number generation or lock checks.
    Use get_fy_for_date(record_date) for those.
    """
    return get_fy_for_date(datetime.date.today())


def is_fy_closed(record_date: datetime.date) -> bool:
    """True if the FY containing record_date is closed."""
    return bool(get_fy_for_date(record_date).get("is_closed"))


def all_financial_years() -> list:
    """All FY rows, newest first."""
    return run_query("""
        SELECT id, fy, fy_short, start_date, end_date,
               is_closed, closed_at, closed_by
        FROM financial_years
        ORDER BY start_date DESC
    """) or []


def ensure_fy_seeded():
    """Ensure current FY exists in financial_years. Safe to call on startup."""
    today = datetime.date.today()
    label = get_fy_label(today)
    short = get_fy_short(today)
    yr    = today.year if today.month >= 4 else today.year - 1
    try:
        run_write("""
            INSERT INTO financial_years (fy, fy_short, start_date, end_date)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (fy) DO NOTHING
        """, (label, short,
              datetime.date(yr, 4, 1),
              datetime.date(yr + 1, 3, 31)))
    except Exception:
        pass
