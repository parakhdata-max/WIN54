"""
modules/online_store/store_auth.py
===================================
OTP-based auth for online customers.
Sessions stored in online_sessions table.
"""
from __future__ import annotations
import uuid, hashlib, random, string
from datetime import datetime, timedelta
from typing import Optional


def _rq(sql, params=None):
    from modules.sql_adapter import run_query
    return run_query(sql, params) or []

def _rw(sql, params=None):
    from modules.sql_adapter import run_write
    return run_write(sql, params)


# ── OTP ───────────────────────────────────────────────────────────────────

def send_otp(mobile: str) -> str:
    """Generate OTP, store in DB, send via MSG91 when configured, and return it for test fallback."""
    otp = "".join(random.choices(string.digits, k=6))
    _rw("""
        INSERT INTO online_otps (mobile, otp, expires_at)
        VALUES (%(m)s, %(otp)s, NOW() + INTERVAL '10 minutes')
        ON CONFLICT DO NOTHING
    """, {"m": mobile, "otp": otp})
    # Cleanup old OTPs
    _rw("DELETE FROM online_otps WHERE mobile=%(m)s AND id != (SELECT id FROM online_otps WHERE mobile=%(m)s ORDER BY created_at DESC LIMIT 1)",
        {"m": mobile})

    # Production SMS: configure MSG91_AUTH_KEY and MSG91_OTP_TEMPLATE_ID in system_settings.
    # If not configured or gateway fails, OTP remains saved and caller can show it in TEST/dev UI.
    try:
        import requests
        cfg = _rq("""
            SELECT key, value
            FROM system_settings
            WHERE key IN ('MSG91_AUTH_KEY', 'MSG91_OTP_TEMPLATE_ID')
        """)
        settings = {r["key"]: r["value"] for r in (cfg or [])}
        authkey = settings.get("MSG91_AUTH_KEY")
        template_id = settings.get("MSG91_OTP_TEMPLATE_ID")
        clean_mobile = "".join(ch for ch in str(mobile) if ch.isdigit())
        if authkey and template_id and len(clean_mobile) >= 10:
            requests.post(
                "https://api.msg91.com/api/v5/otp",
                params={
                    "authkey": authkey,
                    "template_id": template_id,
                    "mobile": f"91{clean_mobile[-10:]}",
                    "otp": otp,
                },
                timeout=5,
            )
    except Exception:
        pass
    return otp


def verify_otp(mobile: str, otp: str) -> bool:
    rows = _rq("""
        SELECT id FROM online_otps
        WHERE mobile=%(m)s AND otp=%(otp)s
          AND expires_at > NOW() AND used=FALSE
        LIMIT 1
    """, {"m": mobile, "otp": otp})
    if not rows:
        return False
    _rw("UPDATE online_otps SET used=TRUE WHERE id=%(id)s::uuid",
        {"id": rows[0]["id"]})
    return True


# ── Customer ──────────────────────────────────────────────────────────────

def get_or_create_customer(mobile: str, name: str = "") -> dict:
    rows = _rq("SELECT * FROM online_customers WHERE mobile=%(m)s LIMIT 1", {"m": mobile})
    if rows:
        _rw("UPDATE online_customers SET last_login=NOW() WHERE id=%(id)s::uuid", {"id": rows[0]["id"]})
        return dict(rows[0])
    cid = str(uuid.uuid4())
    _rw("""
        INSERT INTO online_customers (id, name, mobile, created_at, last_login)
        VALUES (%(id)s::uuid, %(name)s, %(m)s, NOW(), NOW())
    """, {"id": cid, "name": name or mobile, "m": mobile})
    return {"id": cid, "name": name or mobile, "mobile": mobile}


def create_session(customer_id: str) -> str:
    token = str(uuid.uuid4()).replace("-", "") + str(uuid.uuid4()).replace("-", "")
    _rw("""
        INSERT INTO online_sessions (customer_id, token, expires_at)
        VALUES (%(cid)s::uuid, %(tok)s, NOW() + INTERVAL '30 days')
    """, {"cid": customer_id, "tok": token})
    return token


def validate_token(token: str) -> Optional[dict]:
    if not token:
        return None
    rows = _rq("""
        SELECT c.*, s.token
        FROM online_sessions s
        JOIN online_customers c ON c.id = s.customer_id
        WHERE s.token=%(tok)s AND s.expires_at > NOW() AND c.is_active=TRUE
        LIMIT 1
    """, {"tok": token})
    return dict(rows[0]) if rows else None


def logout(token: str) -> None:
    _rw("DELETE FROM online_sessions WHERE token=%(tok)s", {"tok": token})


# ── Address ───────────────────────────────────────────────────────────────

def get_addresses(customer_id: str) -> list:
    return _rq("""
        SELECT * FROM customer_addresses
        WHERE customer_id=%(cid)s::uuid
        ORDER BY is_default DESC, created_at DESC
    """, {"cid": customer_id})


def save_address(customer_id: str, data: dict, address_id: str = None) -> str:
    if data.get("is_default"):
        _rw("UPDATE customer_addresses SET is_default=FALSE WHERE customer_id=%(cid)s::uuid",
            {"cid": customer_id})
    if address_id:
        _rw("""
            UPDATE customer_addresses
            SET label=%(label)s, recipient=%(recipient)s, line1=%(line1)s,
                line2=%(line2)s, city=%(city)s, state=%(state)s,
                pincode=%(pincode)s, phone=%(phone)s, is_default=%(is_default)s
            WHERE id=%(id)s::uuid AND customer_id=%(cid)s::uuid
        """, {**data, "id": address_id, "cid": customer_id})
        return address_id
    aid = str(uuid.uuid4())
    _rw("""
        INSERT INTO customer_addresses
            (id, customer_id, label, recipient, line1, line2, city, state, pincode, phone, is_default)
        VALUES
            (%(id)s::uuid, %(cid)s::uuid, %(label)s, %(recipient)s, %(line1)s,
             %(line2)s, %(city)s, %(state)s, %(pincode)s, %(phone)s, %(is_default)s)
    """, {**data, "id": aid, "cid": customer_id})
    return aid
