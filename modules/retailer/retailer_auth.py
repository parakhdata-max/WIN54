"""
modules/retailer/retailer_auth.py
===================================
OTP-based first login, then password for future logins.

Flow:
  First visit:
    Mobile → OTP → verified → Set password → logged in

  Return visit:
    Mobile → Password → logged in
    OR
    Mobile → Forgot? → OTP → Reset password → logged in

Dev mode: OTP shown on screen
"""

import random
import string
import uuid
import hashlib
import os
from datetime import datetime, timedelta
from typing import Optional

def _retailer_dev_mode() -> bool:
    explicit = os.getenv("RETAILER_DEV_MODE")
    if explicit is not None:
        return explicit.strip().lower() in ("1", "true", "yes", "on")
    return os.getenv("APP_ENV", "TEST").strip().upper() != "PROD"

DEV_MODE        = _retailer_dev_mode()
OTP_EXPIRY_MINS = 10
SESSION_HOURS   = 24
MSG91_AUTH_KEY  = ""
MSG91_TEMPLATE  = ""


def _hash_password(password: str) -> str:
    """Simple SHA256 hash — replace with bcrypt in production if desired."""
    return hashlib.sha256(password.strip().encode()).hexdigest()


def get_party_by_mobile(mobile: str) -> Optional[dict]:
    try:
        from modules.sql_adapter import run_query
        mobile_clean = _clean_mobile(mobile)
        rows = run_query(
            """SELECT id, party_name, mobile, city, is_active,
                      COALESCE(credit_limit, 0) AS credit_limit,
                      portal_password
               FROM parties
               WHERE REGEXP_REPLACE(COALESCE(mobile,''), '[^0-9]', '', 'g') = %s
               LIMIT 1""",
            (mobile_clean,)
        ) or []
        return rows[0] if rows else None
    except Exception:
        return None


def has_password(party: dict) -> bool:
    """Check if retailer has set a portal password."""
    return bool(party.get("portal_password"))


def verify_password(party: dict, password: str) -> bool:
    """Verify entered password against stored hash."""
    stored = party.get("portal_password", "")
    if not stored or not password:
        return False
    return stored == _hash_password(password)


def set_password(party_id: str, new_password: str) -> bool:
    """Save password hash to parties table."""
    try:
        from modules.sql_adapter import run_write
        run_write(
            "UPDATE parties SET portal_password = %s WHERE id = %s",
            (_hash_password(new_password), party_id)
        )
        return True
    except Exception:
        return False


def create_session(party: dict) -> str:
    """Create session token after successful login."""
    try:
        from modules.sql_adapter import run_write
        mobile  = _clean_mobile(str(party.get("mobile", "")))
        expires = datetime.utcnow() + timedelta(hours=SESSION_HOURS)

        # Delete any existing sessions for this party first
        run_write("DELETE FROM portal_sessions WHERE mobile = %s AND token IS NOT NULL",
                  (mobile,))

        # Insert fresh session
        token = _gen_token()
        run_write("""
            INSERT INTO portal_sessions (id, mobile, party_id, token, expires_at, channel)
            VALUES (%s, %s, %s, %s, %s, 'RETAILER')
        """, (str(uuid.uuid4()), mobile, str(party["id"]), token, expires))
        return token
    except Exception:
        return ""


def send_otp(mobile: str, party_name: str) -> dict:
    try:
        from modules.sql_adapter import run_write
        mobile_clean = _clean_mobile(mobile)
        otp    = _gen_otp()
        expiry = datetime.utcnow() + timedelta(minutes=OTP_EXPIRY_MINS)

        # Clear old OTP rows for this mobile, insert fresh
        run_write("DELETE FROM portal_sessions WHERE mobile = %s AND token IS NULL",
                  (mobile_clean,))
        run_write("""
            INSERT INTO portal_sessions (id, mobile, otp, otp_expiry, channel)
            VALUES (%s, %s, %s, %s, 'RETAILER')
        """, (str(uuid.uuid4()), mobile_clean, otp, expiry))

        if DEV_MODE:
            return {"success": True, "otp": otp,
                    "message": f"[DEV] OTP for {party_name}: {otp}"}
        else:
            _send_sms(mobile_clean, otp, party_name)
            return {"success": True, "message": f"OTP sent to {mobile[-4:].rjust(10,'*')}"}
    except Exception as e:
        return {"success": False, "message": f"Error sending OTP: {e}"}


def verify_otp(mobile: str, otp_entered: str) -> dict:
    try:
        from modules.sql_adapter import run_query, run_write
        mobile_clean = _clean_mobile(mobile)
        rows = run_query("""
            SELECT id, otp, otp_expiry
            FROM portal_sessions
            WHERE mobile = %s AND token IS NULL
            ORDER BY otp_expiry DESC LIMIT 1
        """, (mobile_clean,)) or []

        if not rows:
            return {"success": False, "message": "No OTP requested. Please request OTP first."}

        row    = rows[0]
        expiry = row["otp_expiry"]
        if expiry and datetime.utcnow() > expiry.replace(tzinfo=None):
            return {"success": False, "message": "OTP expired. Request a new one."}
        if str(row["otp"]).strip() != str(otp_entered).strip():
            return {"success": False, "message": "Incorrect OTP. Try again."}

        # OTP correct
        party = get_party_by_mobile(mobile_clean)
        if not party:
            return {"success": False, "message": "Party not found."}

        # Clean up used OTP row
        run_write("DELETE FROM portal_sessions WHERE id = %s", (str(row["id"]),))

        return {
            "success":      True,
            "party":        party,
            "needs_password": not has_password(party),  # ← first time: set password
            "message":      "OTP verified",
        }
    except Exception as e:
        return {"success": False, "message": f"Verification error: {e}"}


def validate_token(token: str) -> Optional[dict]:
    if not token:
        return None
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT ps.party_id, ps.mobile, ps.expires_at,
                   p.party_name, p.city, p.credit_limit
            FROM portal_sessions ps
            LEFT JOIN parties p ON p.id = ps.party_id
            WHERE ps.token = %s AND ps.expires_at > NOW()
            LIMIT 1
        """, (token,)) or []
        if not rows:
            return None
        r = rows[0]
        return {
            "id":           str(r["party_id"]) if r["party_id"] else None,
            "party_name":   r["party_name"] or r["mobile"],
            "mobile":       r["mobile"],
            "city":         r.get("city", ""),
            "credit_limit": float(r.get("credit_limit") or 0),
        }
    except Exception:
        return None


def logout(token: str):
    try:
        from modules.sql_adapter import run_write
        run_write("UPDATE portal_sessions SET expires_at = NOW() WHERE token = %s", (token,))
    except Exception:
        pass


def _clean_mobile(mobile: str) -> str:
    m = mobile.strip().replace(" ", "").replace("-", "")
    if m.startswith("+91"): m = m[3:]
    if m.startswith("91") and len(m) == 12: m = m[2:]
    return m

def _gen_otp() -> str:
    return str(random.randint(100000, 999999))

def _gen_token() -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=48))

def _send_sms(mobile: str, otp: str, name: str):
    import urllib.request, json
    payload = json.dumps({
        "template_id": MSG91_TEMPLATE,
        "mobile":      f"91{mobile}",
        "authkey":     MSG91_AUTH_KEY,
        "otp":         otp,
    }).encode()
    req = urllib.request.Request(
        "https://api.msg91.com/api/v5/otp", data=payload,
        headers={"Content-Type": "application/JSON"}, method="POST",
    )
    urllib.request.urlopen(req, timeout=5)

import random
import string
import uuid
from datetime import datetime, timedelta
from typing import Optional

# ── CONFIG ────────────────────────────────────────────────────────────────────
DEV_MODE        = _retailer_dev_mode()
OTP_EXPIRY_MINS = 10
SESSION_HOURS   = 24
MSG91_AUTH_KEY  = ""      # ← Set your MSG91 API key here
MSG91_TEMPLATE  = ""      # ← Set your approved SMS template ID


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════════════════════

def get_party_by_mobile(mobile: str) -> Optional[dict]:
    """Check if mobile exists in parties table. Returns party dict or None."""
    try:
        from modules.sql_adapter import run_query
        mobile_clean = mobile.strip().replace(" ", "").replace("-", "")
        # Remove country code if present
        if mobile_clean.startswith("+91"):
            mobile_clean = mobile_clean[3:]
        if mobile_clean.startswith("91") and len(mobile_clean) == 12:
            mobile_clean = mobile_clean[2:]

        rows = run_query(
            """SELECT id, party_name, mobile, city, is_active,
                      COALESCE(credit_limit, 0) AS credit_limit
               FROM parties
               WHERE REGEXP_REPLACE(COALESCE(mobile,''), '[^0-9]', '', 'g')
                     = %s
               LIMIT 1""",
            (mobile_clean,)
        ) or []
        return rows[0] if rows else None
    except Exception as e:
        return None


def send_otp(mobile: str, party_name: str) -> dict:
    """
    Generate OTP, store in DB, send via SMS.
    Returns {success, otp (dev only), message}
    """
    try:
        from modules.sql_adapter import run_write, run_query

        mobile_clean = _clean_mobile(mobile)
        otp          = _gen_otp()
        expiry       = datetime.utcnow() + timedelta(minutes=OTP_EXPIRY_MINS)

        # Upsert session row
        run_write("""
            INSERT INTO portal_sessions (id, mobile, otp, otp_expiry, channel)
            VALUES (%s, %s, %s, %s, 'RETAILER')
            ON CONFLICT (token) DO NOTHING
        """, (str(uuid.uuid4()), mobile_clean, otp, expiry))

        # Also update any existing row for this mobile
        run_write("""
            UPDATE portal_sessions
            SET otp = %s, otp_expiry = %s, token = NULL, party_id = NULL
            WHERE mobile = %s AND token IS NULL
        """, (otp, expiry, mobile_clean))

        # Send SMS
        if DEV_MODE:
            return {"success": True, "otp": otp,
                    "message": f"[DEV] OTP for {party_name}: {otp}"}
        else:
            _send_sms(mobile_clean, otp, party_name)
            return {"success": True, "message": f"OTP sent to {mobile[-4:].rjust(10,'*')}"}

    except Exception as e:
        return {"success": False, "message": f"Error sending OTP: {e}"}


def verify_otp(mobile: str, otp_entered: str) -> dict:
    """
    Verify OTP. On success, create session token.
    Returns {success, token, party, message}
    """
    try:
        from modules.sql_adapter import run_query, run_write

        mobile_clean = _clean_mobile(mobile)

        rows = run_query("""
            SELECT id, otp, otp_expiry, party_id
            FROM portal_sessions
            WHERE mobile = %s AND token IS NULL
            ORDER BY otp_expiry DESC
            LIMIT 1
        """, (mobile_clean,)) or []

        if not rows:
            return {"success": False, "message": "No OTP requested. Please request OTP first."}

        row    = rows[0]
        expiry = row["otp_expiry"]

        if expiry and datetime.utcnow() > expiry.replace(tzinfo=None):
            return {"success": False, "message": "OTP expired. Request a new one."}

        if str(row["otp"]).strip() != str(otp_entered).strip():
            return {"success": False, "message": "Incorrect OTP. Try again."}

        # ── OTP correct — create session token ───────────────────────────────
        party   = get_party_by_mobile(mobile_clean)
        token   = _gen_token()
        expires = datetime.utcnow() + timedelta(hours=SESSION_HOURS)

        run_write("""
            UPDATE portal_sessions
            SET token = %s, party_id = %s, expires_at = %s
            WHERE mobile = %s AND token IS NULL
        """, (token, str(party["id"]) if party else None, expires, mobile_clean))

        return {
            "success": True,
            "token":   token,
            "party":   party,
            "message": "Login successful",
        }

    except Exception as e:
        return {"success": False, "message": f"Verification error: {e}"}


def validate_token(token: str) -> Optional[dict]:
    """Validate session token. Returns party dict or None if invalid/expired."""
    if not token:
        return None
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT ps.party_id, ps.mobile, ps.expires_at,
                   p.party_name, p.city, p.credit_limit
            FROM portal_sessions ps
            LEFT JOIN parties p ON p.id = ps.party_id
            WHERE ps.token = %s
              AND ps.expires_at > NOW()
            LIMIT 1
        """, (token,)) or []

        if not rows:
            return None
        r = rows[0]
        return {
            "id":         str(r["party_id"]) if r["party_id"] else None,
            "party_name": r["party_name"] or r["mobile"],
            "mobile":     r["mobile"],
            "city":       r.get("city", ""),
            "credit_limit": float(r.get("credit_limit") or 0),
        }
    except Exception:
        return None


def logout(token: str):
    """Invalidate token."""
    try:
        from modules.sql_adapter import run_write
        run_write("UPDATE portal_sessions SET expires_at = NOW() WHERE token = %s", (token,))
    except Exception:
        pass


# ── INTERNAL ──────────────────────────────────────────────────────────────────

def _clean_mobile(mobile: str) -> str:
    m = mobile.strip().replace(" ", "").replace("-", "")
    if m.startswith("+91"): m = m[3:]
    if m.startswith("91") and len(m) == 12: m = m[2:]
    return m


def _gen_otp() -> str:
    return str(random.randint(100000, 999999))


def _gen_token() -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=48))


def _send_sms(mobile: str, otp: str, name: str):
    """Send OTP via MSG91. Configure MSG91_AUTH_KEY and MSG91_TEMPLATE."""
    import urllib.request, json
    url     = "https://api.msg91.com/api/v5/otp"
    payload = json.dumps({
        "template_id": MSG91_TEMPLATE,
        "mobile":      f"91{mobile}",
        "authkey":     MSG91_AUTH_KEY,
        "otp":         otp,
    }).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/JSON"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=5)
