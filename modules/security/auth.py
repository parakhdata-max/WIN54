"""
FINAL AUTH MODULE — STABLE FOR WIN17
Supports psycopg2, SHA256 fallback, bcrypt optional
"""

import streamlit as st
import hashlib
import uuid

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
    except:
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
        except:
            return False

    # bcrypt
    if hashed.startswith("$2"):
        try:
            import bcrypt
            return bcrypt.checkpw(plain.encode(), hashed.encode())
        except:
            return False

    return False

# =====================================================
# SESSION
# =====================================================

def is_logged_in():
    return isinstance(st.session_state.get("user"), dict)

def logout():
    st.session_state.pop("user", None)
    st.rerun()

def _hydrate(user):
    st.session_state["user"] = {
        "id": str(user.get("id")),
        "username": user.get("username"),
        "name": user.get("display_name", user.get("username")),
        "role": (user.get("role") or "viewer").lower(),
        "is_active": user.get("is_active", True)
    }

# =====================================================
# LOGIN CORE
# =====================================================

def login(username: str, password: str):
    if not username or not password:
        return False, "Username and password required"

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
        except:
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

def create_user(username, display_name, password, role, created_by="admin"):
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
    hashed = hash_password(new_password)
    ok = _write("UPDATE erp_users SET password_hash=%s WHERE id=%s", (hashed, user_id))
    return (True, "Password reset") if ok else (False, "DB error")

def toggle_user_active(user_id, active, changed_by="admin"):
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