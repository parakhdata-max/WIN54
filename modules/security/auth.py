"""
FINAL AUTH MODULE — STABLE FOR WIN17
Supports psycopg2, SHA256 fallback, bcrypt optional
"""

import streamlit as st
import hashlib
import uuid
import logging

log = logging.getLogger(__name__)

# =====================================================
# DB HELPERS
# =====================================================

def _query(sql, params=()):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params) or []
    except Exception:
        return []

def _write(sql, params):
    try:
        from modules.sql_adapter import run_write
        return run_write(sql, params)
    except Exception:
        return False

def get_session_user():
    """Return current logged-in user dict safely."""
    u = st.session_state.get("user")
    return u if isinstance(u, dict) else {}

# =====================================================
# PASSWORD HASHING
# =====================================================

def hash_password(plain: str) -> str:
    try:
        import bcrypt
        return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()
    except Exception as e:
        log.warning("bcrypt unavailable; falling back to salted sha256: %s", e)
        salt = uuid.uuid4().hex
        digest = hashlib.sha256((salt + plain).encode()).hexdigest()
        return f"sha256:{salt}:{digest}"

def verify_password(plain: str, hashed) -> bool:
    if not plain or not hashed:
        return False

    # normalize DB weird values
    if isinstance(hashed, (bytes, bytearray, memoryview)):
        hashed = bytes(hashed).decode()
    if isinstance(hashed, (list, tuple)):
        hashed = "".join(str(x) for x in hashed)

    hashed = str(hashed).strip()

    # SHA256 fallback
    if hashed.startswith("sha256:"):
        try:
            _, salt, digest = hashed.split(":", 2)
            return hashlib.sha256((salt + plain).encode()).hexdigest() == digest
        except Exception as e:
            log.warning("Invalid sha256 password hash format: %s", e)
            return False

    # bcrypt
    if hashed.startswith("$2"):
        try:
            import bcrypt
            return bcrypt.checkpw(plain.encode(), hashed.encode())
        except Exception as e:
            log.warning("bcrypt password verification failed: %s", e)
            return False

    return False

# =====================================================
# SESSION
# =====================================================

def is_logged_in():
    return isinstance(st.session_state.get("user"), dict)

def logout():
    st.session_state.pop("user", None)
    # clear Stage 1 bridge mirrors so no stale identity survives logout
    for _k in ("user_id", "user_name", "user_role"):
        st.session_state.pop(_k, None)
    st.rerun()

def _hydrate(user):
    st.session_state["user"] = {
        "id": str(user.get("id")),
        "username": user.get("username"),
        "name": user.get("display_name", user.get("username")),
        "role": (user.get("role") or "viewer").lower(),
        "is_active": user.get("is_active", True)
    }
    # Stage 1 bridge — mirror the SAME object into the flat keys that ~12
    # legacy audit/permission readers still use (challan_invoice_manager,
    # purchase_ui, hr_ui, year_end_ui, billing_status_ui, ...). These are
    # DERIVED from session_state['user'] at the single write point, so there
    # is still one source of truth. This makes created_by stamps record the
    # real user instead of the constant "System"/"Staff" literal, without
    # editing each billing/procurement file in this pass.
    # NOTE: safe because role does not change mid-session (re-login re-hydrates).
    _u = st.session_state["user"]
    st.session_state["user_id"]   = _u["id"]
    st.session_state["user_name"] = _u["name"]
    st.session_state["user_role"] = _u["role"]

# =====================================================
# LOGIN CORE
# =====================================================

def login(username: str, password: str):
    if not username or not password:
        return False, "Username and password required"

    try:
        from modules.core.environment import is_prod, test_login_password
        _test_pw = test_login_password()
        if not is_prod() and _test_pw:
            if password != _test_pw:
                return False, "Invalid TEST password"
            user = {
                "id": "00000000-0000-0000-0000-000000000001",
                "username": username.strip() or "test",
                "display_name": f"TEST - {username.strip() or 'User'}",
                "role": "admin",
                "is_active": True,
            }
            _hydrate(user)
            return True, ""
    except Exception:
        pass

    rows = _query(
        "SELECT * FROM erp_users WHERE LOWER(username)=LOWER(%s) AND is_active=TRUE LIMIT 1",
        (username.strip(),)
    )

    if not rows:
        return False, "Invalid username or password"

    user = rows[0]

    # ensure dict (adapter safety)
    if not isinstance(user, dict):
        try:
            user = dict(user)
        except Exception as e:
            log.error("Could not convert login row to dict: %s", e)
            return False, "Database adapter error"

    if not verify_password(password, user.get("password_hash")):
        return False, "Invalid username or password"

    _hydrate(user)
    return True, ""

# =====================================================
# DEFAULT ADMIN
# =====================================================

def ensure_default_admin():
    rows = _query("SELECT COUNT(*) as cnt FROM erp_users")
    if rows and int(rows[0].get("cnt") or 0) == 0:
        pwd = hash_password("admin123")
        _write("""
            INSERT INTO erp_users(username,display_name,password_hash,role,is_active)
            VALUES(%s,%s,%s,%s,TRUE)
        """, ("admin", "Administrator", pwd, "admin"))
        return True
    return False

# =====================================================
# LOGIN UI
# =====================================================

def render_login_page():
    _, col, _ = st.columns([1, 1.4, 1])
    with col:
        st.title("DV ERP 👓")
        st.caption("Optical Business Management")
        try:
            from modules.core.environment import db_label, is_prod, test_login_password
            if not is_prod():
                st.markdown(
                    f"""
                    <div style="background:#facc15;color:#111827;border:3px solid #dc2626;
                                padding:14px;border-radius:8px;text-align:center;
                                font-weight:1000;font-size:1.6rem;margin:10px 0">
                        BIG TEST LOGIN<br>
                        <span style="font-size:.85rem;font-weight:800">Database: {db_label()}</span>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                if test_login_password():
                    st.caption("TEST mode uses the separate TEST_LOGIN_PASSWORD from .env.")
        except Exception:
            pass

        with st.form("login_form"):
            u = st.text_input("Username")
            p = st.text_input("Password", type="password")

            if st.form_submit_button("Sign In"):
                ok, err = login(u, p)
                if ok:
                    st.rerun()
                else:
                    st.error(err)
# =====================================================
# USER MANAGEMENT (RBAC UI SUPPORT)
# =====================================================

def get_all_users():
    return _query("""
        SELECT id, username, display_name, role, is_active,
               created_at, last_login_at
        FROM erp_users
        ORDER BY role, display_name
    """)

def _can_manage_users() -> tuple:
    """Internal authority check for user-management mutations (Stage 6).

    Defence in depth: these functions previously trusted the caller, so any
    code path reachable by a non-admin (e.g. a manager who can see the Admin
    section) could create users or change roles. Now every mutation verifies
    the acting principal here as well.

    Returns (True, "") if allowed, else (False, message).
    Bootstrap-safe: when there is NO authenticated session (first-run /
    programmatic context) the call is allowed, because ensure_default_admin()
    and migrations legitimately run without a logged-in user. A logged-in
    NON-admin is always denied.
    """
    sess = get_session_user()
    if not sess:                       # no authenticated user → system context
        return True, ""
    try:
        from modules.security.roles import normalize_role, ADMIN
        role = normalize_role(sess.get("role"))
    except Exception:
        role = str(sess.get("role", "")).strip().lower()
        ADMIN = "admin"
    if role == ADMIN:
        return True, ""
    return False, "⛔ Not authorized — user management is admin-only"


def create_user(username, display_name, password, role, created_by="admin"):
    _ok, _msg = _can_manage_users()
    if not _ok:
        return False, _msg

    # normalize + strictly validate role before it touches the DB, so no
    # uppercase / alias / junk role text is ever persisted (and no silent
    # downgrade to viewer for an unrecognised value).
    try:
        from modules.security.roles import canonical_role_or_none
        _crole = canonical_role_or_none(role)
    except Exception:
        _crole = (str(role).strip().lower() or None)
    if not _crole:
        return False, f"⛔ Unknown role: {role!r}"
    role = _crole

    username = username.lower().strip()

    existing = _query("SELECT id FROM erp_users WHERE username=%s", (username,))
    if existing:
        return False, "Username already exists"

    hashed = hash_password(password)
    ok = _write("""
        INSERT INTO erp_users(username,display_name,password_hash,role,is_active)
        VALUES(%s,%s,%s,%s,TRUE)
    """, (username, display_name, hashed, role))

    return (True, "User created") if ok else (False, "DB error")

def update_user_role(user_id, new_role, changed_by="admin", reason=""):
    _ok, _msg = _can_manage_users()
    if not _ok:
        return False, _msg
    # normalize + strictly validate before persisting (see create_user)
    try:
        from modules.security.roles import canonical_role_or_none
        _crole = canonical_role_or_none(new_role)
    except Exception:
        _crole = (str(new_role).strip().lower() or None)
    if not _crole:
        return False, f"⛔ Unknown role: {new_role!r}"
    new_role = _crole
    ok = _write("UPDATE erp_users SET role=%s WHERE id=%s", (new_role, user_id))
    if ok:
        # Log role change if table exists
        try:
            _write("""
                INSERT INTO user_role_changes(target_user, new_role, changed_by, reason)
                SELECT username, %s, %s, %s FROM erp_users WHERE id=%s
            """, (new_role, changed_by, reason or "", user_id))
        except Exception:
            pass
    return (True, "Role updated") if ok else (False, "DB error")

def reset_user_password(user_id, new_password, changed_by="admin"):
    _ok, _msg = _can_manage_users()
    if not _ok:
        return False, _msg
    hashed = hash_password(new_password)
    ok = _write("UPDATE erp_users SET password_hash=%s WHERE id=%s", (hashed, user_id))
    return (True, "Password reset") if ok else (False, "DB error")

def toggle_user_active(user_id, active, changed_by="admin"):
    _ok, _msg = _can_manage_users()
    if not _ok:
        return False, _msg
    ok = _write("UPDATE erp_users SET is_active=%s WHERE id=%s", (active, user_id))
    return (True, "User updated") if ok else (False, "DB error")
def change_own_password(user_id: str, current_password: str, new_password: str):
    rows = _query("SELECT password_hash FROM erp_users WHERE id=%s", (user_id,))
    if not rows:
        return False, "User not found"

    if not verify_password(current_password, rows[0].get("password_hash")):
        return False, "Current password incorrect"

    hashed = hash_password(new_password)
    ok = _write("UPDATE erp_users SET password_hash=%s WHERE id=%s", (hashed, user_id))
    return (True, "Password changed") if ok else (False, "DB error")
