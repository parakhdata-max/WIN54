"""
Shared WhatsApp mobile lookup/save helpers.

Used by order punching, billing, registers, consultation, and backoffice panels
so a missing WhatsApp number can be entered once and reused from DB next time.
"""

from __future__ import annotations

import re
from typing import Optional

import streamlit as st


def clean_mobile(raw: str) -> str:
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if len(digits) > 12:
        digits = digits[-10:]
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def lookup_mobile(name: str = "", *, order_id: str = "", patient_id: str = "", fallback: str = "") -> str:
    name = str(name or "").strip()
    try:
        from modules.sql_adapter import run_query
    except Exception:
        return clean_mobile(fallback)

    if patient_id and len(str(patient_id)) > 10:
        try:
            rows = run_query(
                "SELECT COALESCE(mobile,'') AS mobile FROM patients WHERE id=%(id)s::uuid LIMIT 1",
                {"id": str(patient_id)},
            ) or []
            mob = clean_mobile(rows[0].get("mobile", "")) if rows else ""
            if mob:
                return mob
        except Exception:
            pass

    if order_id and len(str(order_id)) > 10:
        try:
            rows = run_query(
                """
                SELECT COALESCE(patient_mobile, '') AS mobile,
                       COALESCE(patient_name, party_name, '') AS name
                FROM orders WHERE id=%(id)s::uuid LIMIT 1
                """,
                {"id": str(order_id)},
            ) or []
            mob = clean_mobile(rows[0].get("mobile", "")) if rows else ""
            if mob:
                return mob
            if rows and not name:
                name = str(rows[0].get("name") or "").strip()
        except Exception:
            pass

    if name:
        try:
            rows = run_query(
                """
                SELECT mobile FROM (
                    SELECT COALESCE(mobile,'') AS mobile, 1 AS ord
                    FROM parties WHERE party_name = %(name)s
                    UNION ALL
                    SELECT COALESCE(mobile,'') AS mobile, 2 AS ord
                    FROM patients WHERE master_name = %(name)s
                    UNION ALL
                    SELECT COALESCE(patient_mobile, '') AS mobile, 3 AS ord
                    FROM orders WHERE COALESCE(patient_name, party_name) = %(name)s
                ) x
                WHERE COALESCE(mobile,'') <> ''
                ORDER BY ord
                LIMIT 1
                """,
                {"name": name},
            ) or []
            mob = clean_mobile(rows[0].get("mobile", "")) if rows else ""
            if mob:
                return mob
        except Exception:
            pass

    return clean_mobile(fallback)


def save_mobile(name: str = "", mobile: str = "", *, order_id: str = "", patient_id: str = "") -> tuple[bool, str]:
    name = str(name or "").strip()
    mob = clean_mobile(mobile)
    if not mob:
        return False, "Enter a valid 10-digit mobile number."

    try:
        from modules.sql_adapter import run_query, run_write
    except Exception as exc:
        return False, f"DB unavailable: {exc}"

    try:
        if patient_id and len(str(patient_id)) > 10:
            run_write("UPDATE patients SET mobile=%(m)s WHERE id=%(id)s::uuid", {"m": mob, "id": str(patient_id)})
            return True, "Mobile saved in patient master."

        if order_id and len(str(order_id)) > 10:
            rows = run_query(
                "SELECT COALESCE(patient_name, party_name, '') AS name FROM orders WHERE id=%(id)s::uuid LIMIT 1",
                {"id": str(order_id)},
            ) or []
            if rows and not name:
                name = str(rows[0].get("name") or "").strip()
            run_write(
                """
                UPDATE orders
                SET patient_mobile = %(m)s
                WHERE id=%(id)s::uuid
                """,
                {"m": mob, "id": str(order_id)},
            )

        if name:
            rows = run_query("SELECT id::text FROM parties WHERE party_name=%(name)s LIMIT 1", {"name": name}) or []
            if rows:
                run_write("UPDATE parties SET mobile=%(m)s WHERE id=%(id)s::uuid", {"m": mob, "id": rows[0]["id"]})
                return True, "Mobile saved in CRM party master."

            rows = run_query("SELECT id::text FROM patients WHERE master_name=%(name)s LIMIT 1", {"name": name}) or []
            if rows:
                run_write("UPDATE patients SET mobile=%(m)s WHERE id=%(id)s::uuid", {"m": mob, "id": rows[0]["id"]})
                return True, "Mobile saved in patient master."

            run_write(
                """
                UPDATE orders
                SET patient_mobile = %(m)s
                WHERE COALESCE(patient_name, party_name) = %(name)s
                  AND COALESCE(patient_mobile,'') = ''
                """,
                {"m": mob, "name": name},
            )
            return True, "Mobile saved against order/customer history."

        return True, "Mobile saved against this order."
    except Exception as exc:
        return False, f"Mobile save failed: {exc}"


def render_mobile_field(
    key: str,
    *,
    name: str = "",
    mobile: str = "",
    order_id: str = "",
    patient_id: str = "",
    label: str = "WhatsApp mobile",
    save: bool = True,
) -> str:
    resolved = lookup_mobile(name, order_id=order_id, patient_id=patient_id, fallback=mobile)
    mob_key = f"{key}_wa_mobile"
    if mob_key not in st.session_state:
        st.session_state[mob_key] = resolved or clean_mobile(mobile)
    entered = st.text_input(label, key=mob_key, placeholder="10-digit mobile")
    if save:
        if st.button("Save mobile to DB", key=f"{key}_wa_mobile_save", use_container_width=True):
            ok, msg = save_mobile(name, entered, order_id=order_id, patient_id=patient_id)
            if ok:
                st.success(msg)
            else:
                st.warning(msg)
    return clean_mobile(entered) or resolved
