"""
modules/crm/crm.py  —  DV ERP Professional CRM  (v3)
=====================================================
Tally/ERP-grade party management with:
  - GST, PAN, GSTIN, TAN, CIN, State Code
  - Credit limit & credit days
  - Contact person, email, alternate phone
  - Billing address vs shipping address
  - Party category tags
  - Lead pipeline & follow-up tracker

DB TRUTH (from db_schema_registry.py):
  parties.is_active  = BOOLEAN  (the real active flag)
  parties.status     = BOOLEAN  (legacy mirror — do NOT write varchar here)
  >>> CRM writes is_active only. NEVER writes to status column. <<<
"""

import streamlit as st
import uuid
import logging
from datetime import date, timedelta
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

PARTY_TYPES   = ["Supplier","Retail","Wholesale","Doctor","Optician","Fitter"]
LEAD_STAGES   = ["NEW","CONTACTED","QUALIFIED","PROPOSAL","WON","LOST"]
STAGE_COLORS  = {"NEW":"#6b7280","CONTACTED":"#3b82f6","QUALIFIED":"#8b5cf6",
                 "PROPOSAL":"#f59e0b","WON":"#10b981","LOST":"#ef4444"}
FOLLOWUP_TYPES = ["CALL","VISIT","WHATSAPP","EMAIL","DEMO","OTHER"]
LEAD_SOURCES   = ["Walk-in","Referral","Cold Call","WhatsApp","Exhibition",
                  "Online","Existing Customer","Other"]
FU_ICONS = {"CALL":"📞","VISIT":"🚗","WHATSAPP":"💬","EMAIL":"📧","DEMO":"🖥️","OTHER":"📌"}

INDIAN_STATES = [
    "Andhra Pradesh","Arunachal Pradesh","Assam","Bihar","Chhattisgarh","Goa","Gujarat",
    "Haryana","Himachal Pradesh","Jharkhand","Karnataka","Kerala","Madhya Pradesh",
    "Maharashtra","Manipur","Meghalaya","Mizoram","Nagaland","Odisha","Punjab","Rajasthan",
    "Sikkim","Tamil Nadu","Telangana","Tripura","Uttar Pradesh","Uttarakhand","West Bengal",
    "Andaman and Nicobar Islands","Chandigarh","Dadra and Nagar Haveli and Daman and Diu",
    "Delhi","Jammu and Kashmir","Ladakh","Lakshadweep","Puducherry","Other",
]

GST_RATES = ["0","5","12","18","28"]


# ─────────────────────────────────────────────────────────────────────────────
# RAW DB
# ─────────────────────────────────────────────────────────────────────────────

def _q(sql, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        logger.error(f"[CRM:q] {e}")
        return []

def _w(sql, params=None):
    """Returns None on success, error string on failure."""
    try:
        from modules.sql_adapter import run_write
        run_write(sql, params or {})
        return None
    except Exception as e:
        logger.error(f"[CRM:w] {e}")
        return str(e)


# ─────────────────────────────────────────────────────────────────────────────
# BOOTSTRAP — once per session
# ─────────────────────────────────────────────────────────────────────────────

def _bootstrap():
    # Re-run if tables are missing even if session says we already booted
    if st.session_state.get("_crm_boot3"):
        # Quick sanity check — if crm_leads is missing, clear the boot flag so we re-run
        try:
            from modules.sql_adapter import run_query as _rqb
            _rqb("SELECT 1 FROM crm_leads LIMIT 0", {})
        except Exception:
            st.session_state.pop("_crm_boot3", None)
            _bust()
    if st.session_state.get("_crm_boot3"):
        return
    ddls = [
        # Extended parties columns (ALTER — safe if column already exists)
        "ALTER TABLE parties ADD COLUMN IF NOT EXISTS gstin          VARCHAR(15)",
        "ALTER TABLE parties ADD COLUMN IF NOT EXISTS pan_no         VARCHAR(10)",
        "ALTER TABLE parties ADD COLUMN IF NOT EXISTS tan_no         VARCHAR(10)",
        "ALTER TABLE parties ADD COLUMN IF NOT EXISTS cin_no         VARCHAR(21)",
        "ALTER TABLE parties ADD COLUMN IF NOT EXISTS gst_rate       NUMERIC(5,2) DEFAULT 0",
        "ALTER TABLE parties ADD COLUMN IF NOT EXISTS state_code     VARCHAR(2)",
        "ALTER TABLE parties ADD COLUMN IF NOT EXISTS state_name     VARCHAR(80)",
        "ALTER TABLE parties ADD COLUMN IF NOT EXISTS pincode        VARCHAR(6)",
        "ALTER TABLE parties ADD COLUMN IF NOT EXISTS email          VARCHAR(120)",
        "ALTER TABLE parties ADD COLUMN IF NOT EXISTS alt_mobile     VARCHAR(15)",
        "ALTER TABLE parties ADD COLUMN IF NOT EXISTS contact_person VARCHAR(100)",
        "ALTER TABLE parties ADD COLUMN IF NOT EXISTS credit_limit   NUMERIC(12,2) DEFAULT 0",
        "ALTER TABLE parties ADD COLUMN IF NOT EXISTS credit_days    INTEGER DEFAULT 0",
        "ALTER TABLE parties ADD COLUMN IF NOT EXISTS opening_balance NUMERIC(12,2) DEFAULT 0",
        "ALTER TABLE parties ADD COLUMN IF NOT EXISTS balance_type   VARCHAR(10) DEFAULT 'Dr'",
        "ALTER TABLE parties ADD COLUMN IF NOT EXISTS tally_group    VARCHAR(80)",
        "ALTER TABLE parties ADD COLUMN IF NOT EXISTS notes          TEXT",
        # CRM tables
        """CREATE TABLE IF NOT EXISTS crm_leads (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            party_id UUID, lead_source VARCHAR(80), stage VARCHAR(40) DEFAULT 'NEW',
            notes TEXT, assigned_to VARCHAR(80), potential_value NUMERIC(12,2) DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW(), updated_at TIMESTAMP DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS crm_followups (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            party_id UUID, lead_id UUID, followup_type VARCHAR(40) DEFAULT 'CALL',
            due_date DATE, done BOOLEAN DEFAULT FALSE, done_at TIMESTAMP,
            notes TEXT, created_by VARCHAR(80), created_at TIMESTAMP DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS crm_touchpoints (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            party_id UUID, type VARCHAR(40) DEFAULT 'NOTE',
            summary TEXT, created_by VARCHAR(80), created_at TIMESTAMP DEFAULT NOW())""",
    ]
    for ddl in ddls:
        err = _w(ddl)
        if err:
            logger.warning(f"[CRM:DDL] {err}")
    st.session_state["_crm_boot3"] = True


# ─────────────────────────────────────────────────────────────────────────────
# CACHE
# ─────────────────────────────────────────────────────────────────────────────

def _cached(key, fn):
    """Cache result in session_state. Never stores None so failed DB calls retry."""
    k = f"_crm_{key}"
    if k not in st.session_state or st.session_state[k] is None:
        result = fn()
        if result is not None:          # only cache successful results
            st.session_state[k] = result
        return result if result is not None else []
    return st.session_state[k]

def _bust():
    for k in list(st.session_state.keys()):
        if k.startswith("_crm_") and k not in ("_crm_boot3",):
            del st.session_state[k]


# ─────────────────────────────────────────────────────────────────────────────
# FETCH (all cached)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_parties(party_type=None, search=""):
    key = f"pl_{party_type}_{search}"
    def _load():
        s = f"%{(search or '').lower()}%"
        base = ("SELECT id, party_name, party_type, mobile, city, area, "
                "is_active, gstin, credit_limit, credit_days, contact_person, email "
                "FROM parties ")
        if party_type and party_type != "All":
            return _q(base + "WHERE party_type=%(t)s AND "
                      "(LOWER(party_name) LIKE %(s)s OR COALESCE(mobile,'') LIKE %(s)s) "
                      "ORDER BY party_name LIMIT 300",
                      {"t": party_type, "s": s})
        return _q(base + "WHERE (LOWER(party_name) LIKE %(s)s OR COALESCE(mobile,'') LIKE %(s)s) "
                  "ORDER BY party_name LIMIT 300", {"s": s})
    return _cached(key, _load)

def fetch_party(pid):
    rows = _q("SELECT * FROM parties WHERE id=%(id)s", {"id": pid})
    return rows[0] if rows else None

def fetch_suppliers():
    return _cached("sup", lambda: _q(
        "SELECT id, party_name, mobile, city, area, is_active, gstin, "
        "credit_limit, credit_days, contact_person "
        "FROM parties WHERE party_type='Supplier' ORDER BY party_name", {}))

def fetch_metrics():
    def _load():
        # parties count — always safe
        base = _q("SELECT COUNT(*) AS tp FROM parties", {})
        tp   = (base[0].get("tp", 0) if base else 0)
        sup  = _q("SELECT COUNT(*) AS ts FROM parties WHERE party_type='Supplier'", {})
        ts   = (sup[0].get("ts", 0) if sup else 0)
        # CRM tables may not exist yet — query separately so one failure doesn't kill all
        try:
            from modules.sql_adapter import run_query as _rqm
            fup = _rqm("SELECT COUNT(*) AS of FROM crm_followups WHERE done=FALSE AND due_date<=CURRENT_DATE", {})
            of_ = fup[0].get("of", 0) if fup else 0
        except Exception:
            of_ = 0
        try:
            from modules.sql_adapter import run_query as _rqm2
            lds = _rqm2("SELECT COUNT(*) AS ol FROM crm_leads WHERE stage NOT IN ('WON','LOST')", {})
            ol  = lds[0].get("ol", 0) if lds else 0
        except Exception:
            ol  = 0
        return {"tp": tp, "ts": ts, "of": of_, "ol": ol}
    return _cached("metrics", _load)

def fetch_leads(stage=None):
    key = f"leads_{stage}"
    def _load():
        where  = "" if not stage or stage == "All" else "WHERE cl.stage=%(stage)s"
        params = {} if not stage or stage == "All" else {"stage": stage}
        return _q(f"SELECT cl.id, cl.stage, cl.lead_source, cl.notes, cl.assigned_to, "
                  f"cl.potential_value, cl.updated_at, p.party_name, p.mobile "
                  f"FROM crm_leads cl LEFT JOIN parties p ON p.id=cl.party_id "
                  f"{where} ORDER BY cl.updated_at DESC LIMIT 200", params)
    return _cached(key, _load)

def fetch_followups_all():
    return _cached("fups", lambda: _q(
        "SELECT cf.id, cf.followup_type, cf.due_date, cf.notes, "
        "p.party_name, p.mobile FROM crm_followups cf "
        "LEFT JOIN parties p ON p.id=cf.party_id "
        "WHERE cf.done=FALSE ORDER BY cf.due_date ASC LIMIT 200", {}))

def fetch_touchpoints(pid):
    # NOT cached — lazy, called only inside open expander
    return _q("SELECT type, summary, created_by, created_at FROM crm_touchpoints "
              "WHERE party_id=%(pid)s ORDER BY created_at DESC LIMIT 20", {"pid": pid})


# ─────────────────────────────────────────────────────────────────────────────
# SAVE PARTY — writes is_active (boolean), NEVER status
# ─────────────────────────────────────────────────────────────────────────────

def save_party(data: Dict):
    """
    Upsert party via INSERT ... ON CONFLICT.
    Returns (party_id, None) on success, (None, error_str) on failure.

    CRITICAL: is_active is BOOLEAN in DB. status is also BOOLEAN (legacy).
    This function ONLY writes is_active. status is never touched.
    """
    pid      = data.get("id") or str(uuid.uuid4())
    is_new   = not data.get("id")
    is_active = bool(data.get("is_active", True))

    p = {
        "pid":       pid,
        "name":      (data.get("party_name") or "").strip(),
        "ptype":     data.get("party_type", "Retail"),
        "mobile":    (data.get("mobile") or "").strip() or None,
        "alt_mob":   (data.get("alt_mobile") or "").strip() or None,
        "email":     (data.get("email") or "").strip() or None,
        "contact":   (data.get("contact_person") or "").strip() or None,
        "address":   (data.get("address") or "").strip() or None,
        "city":      (data.get("city") or "").strip() or None,
        "area":      (data.get("area") or "").strip() or None,
        "pincode":   (data.get("pincode") or "").strip() or None,
        "state_name":(data.get("state_name") or "").strip() or None,
        "state_code":(data.get("state_code") or "").strip() or None,
        "gstin":     (data.get("gstin") or "").strip().upper() or None,
        "pan_no":    (data.get("pan_no") or "").strip().upper() or None,
        "tan_no":    (data.get("tan_no") or "").strip().upper() or None,
        "cin_no":    (data.get("cin_no") or "").strip().upper() or None,
        "gst_rate":  float(data.get("gst_rate") or 0),
        "cl":        float(data.get("credit_limit") or 0),
        "cd":        int(data.get("credit_days") or 0),
        "ob":        float(data.get("opening_balance") or 0),
        "bt":        data.get("balance_type", "Dr"),
        "tally_grp": (data.get("tally_group") or "").strip() or None,
        "notes":     (data.get("notes") or "").strip() or None,
        "is_active": is_active,
        "bp":        (data.get("billing_preference") or "CHALLAN").upper(),
    }

    sql = """
        INSERT INTO parties (
            id, party_name, party_type, mobile, alt_mobile, email,
            contact_person, address, city, area, pincode,
            state_name, state_code,
            gstin, pan_no, tan_no, cin_no, gst_rate,
            credit_limit, credit_days, opening_balance, balance_type,
            tally_group, notes, is_active, billing_preference, created_at
        ) VALUES (
            %(pid)s, %(name)s, %(ptype)s, %(mobile)s, %(alt_mob)s, %(email)s,
            %(contact)s, %(address)s, %(city)s, %(area)s, %(pincode)s,
            %(state_name)s, %(state_code)s,
            %(gstin)s, %(pan_no)s, %(tan_no)s, %(cin_no)s, %(gst_rate)s,
            %(cl)s, %(cd)s, %(ob)s, %(bt)s,
            %(tally_grp)s, %(notes)s, %(is_active)s, %(bp)s, NOW()
        )
        ON CONFLICT (id) DO UPDATE SET
            party_name=%(name)s, party_type=%(ptype)s, mobile=%(mobile)s,
            alt_mobile=%(alt_mob)s, email=%(email)s,
            contact_person=%(contact)s, address=%(address)s,
            city=%(city)s, area=%(area)s, pincode=%(pincode)s,
            state_name=%(state_name)s, state_code=%(state_code)s,
            gstin=%(gstin)s, pan_no=%(pan_no)s, tan_no=%(tan_no)s,
            cin_no=%(cin_no)s, gst_rate=%(gst_rate)s,
            credit_limit=%(cl)s, credit_days=%(cd)s,
            opening_balance=%(ob)s, balance_type=%(bt)s,
            tally_group=%(tally_grp)s, notes=%(notes)s, is_active=%(is_active)s,
            billing_preference=%(bp)s
    """
    err = _w(sql, p)
    if err:
        # If new column doesn't exist yet (ALTER TABLE might not have run), retry minimal
        if "column" in err.lower() and "does not exist" in err.lower():
            sql_min = """
                INSERT INTO parties(id, party_name, party_type, mobile, address,
                    city, area, is_active, created_at)
                VALUES(%(pid)s,%(name)s,%(ptype)s,%(mobile)s,%(address)s,
                    %(city)s,%(area)s,%(is_active)s,NOW())
                ON CONFLICT (id) DO UPDATE SET
                    party_name=%(name)s, party_type=%(ptype)s, mobile=%(mobile)s,
                    address=%(address)s, city=%(city)s, area=%(area)s,
                    is_active=%(is_active)s
            """
            err = _w(sql_min, p)
        if err:
            return None, err
    _bust()
    return pid, None


def save_lead(data):
    lid = data.get("id") or str(uuid.uuid4())
    err = _w(
        "INSERT INTO crm_leads(id,party_id,lead_source,stage,notes,assigned_to,"
        "potential_value,created_at,updated_at) "
        "VALUES(%(id)s,%(pid)s,%(src)s,%(stage)s,%(notes)s,%(asgn)s,%(val)s,NOW(),NOW()) "
        "ON CONFLICT (id) DO UPDATE SET stage=%(stage)s,notes=%(notes)s,"
        "lead_source=%(src)s,assigned_to=%(asgn)s,potential_value=%(val)s,updated_at=NOW()",
        {"id": lid, "pid": data.get("party_id"), "src": data.get("lead_source",""),
         "stage": data.get("stage","NEW"), "notes": data.get("notes",""),
         "asgn": data.get("assigned_to",""), "val": float(data.get("potential_value") or 0)})
    if err is None:
        _bust(); return lid, None
    return None, err


def save_followup(data):
    fid = str(uuid.uuid4())
    err = _w(
        "INSERT INTO crm_followups(id,party_id,lead_id,followup_type,due_date,"
        "notes,created_by,created_at) "
        "VALUES(%(id)s,%(pid)s,%(lid)s,%(type)s,%(due)s,%(notes)s,%(by)s,NOW())",
        {"id": fid, "pid": data.get("party_id"), "lid": data.get("lead_id"),
         "type": data.get("followup_type","CALL"), "due": data.get("due_date"),
         "notes": data.get("notes",""), "by": data.get("created_by","system")})
    if err is None:
        _bust(); return fid, None
    return None, err


def mark_followup_done(fid):
    err = _w("UPDATE crm_followups SET done=TRUE,done_at=NOW() WHERE id=%(id)s", {"id": fid})
    if err is None: _bust()
    return err


def log_touchpoint(pid, tp_type, summary, user):
    return _w("INSERT INTO crm_touchpoints(id,party_id,type,summary,created_by,created_at) "
              "VALUES(%(id)s,%(pid)s,%(type)s,%(sum)s,%(by)s,NOW())",
              {"id": str(uuid.uuid4()), "pid": pid, "type": tp_type,
               "sum": summary, "by": user})


def _user():
    try:
        from modules.security.roles import current_user_name
        return current_user_name()
    except Exception:
        return "system"


# ─────────────────────────────────────────────────────────────────────────────
# PARTY FORM — full Tally/ERP grade
# ─────────────────────────────────────────────────────────────────────────────

def _party_form(key: str, existing: Dict = None, compact: bool = False):
    """
    Renders the party form. Returns (submitted:bool, data:dict).
    compact=True for the quick-add supplier widget.
    All internal widget keys are scoped to `key` to prevent
    DuplicateWidgetID when multiple forms render simultaneously
    (Streamlit renders ALL tabs on every run, not just the active one).
    """
    ex  = existing or {}
    _k  = key.replace(" ", "_")   # safe prefix for widget keys

    with st.form(key):
        # ── Basic Info ───────────────────────────────────────────────────────
        st.markdown("**Basic Information**")
        b1, b2, b3 = st.columns(3)
        with b1:
            pname  = st.text_input("Party Name *", value=ex.get("party_name",""))
            mobile = st.text_input("Mobile",        value=ex.get("mobile","") or "")
            email  = st.text_input("Email",         value=ex.get("email","") or "")
        with b2:
            idx    = PARTY_TYPES.index(ex["party_type"]) if ex.get("party_type") in PARTY_TYPES else 0
            ptype  = st.selectbox("Party Type", PARTY_TYPES, index=idx)
            alt_mob = st.text_input("Alt Mobile",  value=ex.get("alt_mobile","") or "")
            contact = st.text_input("Contact Person", value=ex.get("contact_person","") or "")
        with b3:
            is_active = st.checkbox("Active", value=bool(ex.get("is_active", True)))
            tally_grp = st.text_input("Tally Ledger Group",
                                       value=ex.get("tally_group","") or "",
                                       placeholder="e.g. Sundry Debtors")
            if not compact:
                notes = st.text_area("Internal Notes", value=ex.get("notes","") or "", height=68)
            else:
                notes = ""

        if not compact:
            st.markdown("**Address**")
            a1, a2 = st.columns(2)
            with a1:
                address = st.text_area("Billing Address", value=ex.get("address","") or "", height=70)
                city    = st.text_input("City",    value=ex.get("city","") or "", key=f"party_city_{_k}")
                area    = st.text_input("Area",    value=ex.get("area","") or "", key=f"party_area_{_k}")
            with a2:
                pincode = st.text_input("Pincode", value=ex.get("pincode","") or "")
                state_names = [""] + INDIAN_STATES
                cur_state   = ex.get("state_name","") or ""
                sidx = state_names.index(cur_state) if cur_state in state_names else 0
                state_name  = st.selectbox("State", state_names, index=sidx)
                state_code  = st.text_input("State Code (GST)", value=ex.get("state_code","") or "",
                                             placeholder="e.g. 27 for Maharashtra",
                                             max_chars=2)
        else:
            address = st.text_area("Address", value=ex.get("address","") or "", height=60)
            city    = st.text_input("City",    value=ex.get("city","") or "", key=f"party_city_{_k}")
            area    = st.text_input("Area",    value=ex.get("area","") or "", key=f"party_area_{_k}")
            pincode = state_name = state_code = ""

        # ── Tax / Compliance ─────────────────────────────────────────────────
        st.markdown("**GST & Compliance**")
        t1, t2, t3, t4 = st.columns(4)
        with t1:
            gstin   = st.text_input("GSTIN (15 digits)", value=ex.get("gstin","") or "",
                                     max_chars=15, placeholder="27AABCU9603R1ZX")
        with t2:
            pan_no  = st.text_input("PAN (10 chars)",    value=ex.get("pan_no","") or "",
                                     max_chars=10, placeholder="AABCU9603R")
        with t3:
            tan_no  = st.text_input("TAN",               value=ex.get("tan_no","") or "",
                                     max_chars=10)
        with t4:
            gst_idx  = GST_RATES.index(str(int(float(ex.get("gst_rate",0) or 0)))) \
                       if str(int(float(ex.get("gst_rate",0) or 0))) in GST_RATES else 0
            gst_rate = st.selectbox("GST Rate %", GST_RATES, index=gst_idx)

        if not compact:
            cin_no = st.text_input("CIN (Companies)", value=ex.get("cin_no","") or "",
                                    max_chars=21, placeholder="U12345MH2000PTC123456")
        else:
            cin_no = ""

        # ── Credit Terms ─────────────────────────────────────────────────────
        st.markdown("**Credit Terms**")
        cr1, cr2, cr3, cr4 = st.columns(4)
        with cr1:
            credit_limit = st.number_input("Credit Limit (₹)",
                                            value=float(ex.get("credit_limit",0) or 0),
                                            min_value=0.0, step=1000.0)
        with cr2:
            credit_days  = st.number_input("Credit Days",
                                            value=int(ex.get("credit_days",0) or 0),
                                            min_value=0, step=5)
        with cr3:
            opening_bal  = st.number_input("Opening Balance (₹)",
                                            value=float(ex.get("opening_balance",0) or 0),
                                            step=100.0)
        with cr4:
            bt_idx = 0 if ex.get("balance_type","Dr") == "Dr" else 1
            bal_type = st.selectbox("Balance Type", ["Dr","Cr"], index=bt_idx)

        # ── Billing & Document Settings ──────────────────────────────────────
        if not compact:
            st.markdown("**Billing Settings**")
            bp1, bp2 = st.columns(2)
            with bp1:
                _bp_opts   = ["CHALLAN", "DIRECT_INVOICE"]
                _bp_labels = {
                    "CHALLAN":        "📋 Challan first, invoice later (default)",
                    "DIRECT_INVOICE": "🧾 Direct Invoice — wholesale only",
                }
                _bp_cur = (ex.get("billing_preference") or "CHALLAN").upper()
                if _bp_cur not in _bp_opts:
                    _bp_cur = "CHALLAN"
                billing_pref = st.selectbox(
                    "Billing Preference",
                    _bp_opts,
                    index=_bp_opts.index(_bp_cur),
                    format_func=lambda x: _bp_labels.get(x, x),
                    help="CHALLAN: create challan from order, convert to invoice later.\n"
                         "DIRECT_INVOICE: wholesale parties only — challan + invoice created in one step.",
                    key=f"billing_pref_{_k}",
                )
            with bp2:
                st.markdown(
                    "<div style='background:#0f172a;border:1px solid #1e293b;"
                    "border-radius:6px;padding:8px 12px;margin-top:26px'>"
                    "<div style='color:#94a3b8;font-size:0.72rem'>"
                    "Retail parties always use Challan regardless of this setting. "
                    "Set DIRECT_INVOICE only for wholesale parties who want "
                    "immediate invoicing without a separate challan step."
                    "</div></div>",
                    unsafe_allow_html=True,
                )
        else:
            billing_pref = ex.get("billing_preference") or "CHALLAN"

        submitted = st.form_submit_button("💾 Save", type="primary", use_container_width=True)

    if submitted:
        return True, {
            "id":             ex.get("id"),
            "party_name":     pname,
            "party_type":     ptype,
            "mobile":         mobile,
            "alt_mobile":     alt_mob,
            "email":          email,
            "contact_person": contact,
            "address":        address,
            "city":           city,
            "area":           area,
            "pincode":        pincode,
            "state_name":     state_name,
            "state_code":     state_code,
            "gstin":          gstin,
            "pan_no":         pan_no,
            "tan_no":         tan_no,
            "cin_no":         cin_no,
            "gst_rate":       gst_rate,
            "credit_limit":   credit_limit,
            "credit_days":    credit_days,
            "opening_balance":opening_bal,
            "balance_type":   bal_type,
            "tally_group":    tally_grp,
            "notes":          notes,
            "is_active":      is_active,
            "billing_preference": billing_pref,
        }
    return False, {}


def _show_save_error(err):
    st.error(f"❌ {err}")
    if "does not exist" in err:
        st.info("💡 Column not found — ALTER TABLE may still be running. Try again in a moment.")
    elif "duplicate" in err.lower() or "unique" in err.lower():
        st.warning("A party with this name/mobile already exists.")
    elif "boolean" in err.lower():
        st.error("🔴 Boolean type error — please report this to admin. is_active must be True/False.")


# ─────────────────────────────────────────────────────────────────────────────
# INLINE SUPPLIER QUICK-ADD
# ─────────────────────────────────────────────────────────────────────────────

def render_supplier_quick_add(on_success=None):
    """Compact supplier form for embedding in Backoffice. Returns new party_id."""
    st.markdown("#### ➕ Add New Supplier")
    submitted, data = _party_form("crm_sup_quick", compact=True)
    if submitted:
        if not data.get("party_name","").strip():
            st.error("Supplier name is required.")
            return None
        data["party_type"] = "Supplier"
        pid, err = save_party(data)
        if pid:
            st.success(f"✅ **{data['party_name']}** added as Supplier")
            log_touchpoint(pid, "NOTE", "Created via Quick Add", _user())
            if on_success:
                on_success(pid, data["party_name"])
            return pid
        else:
            _show_save_error(err)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# TAB: SUPPLIERS
# ─────────────────────────────────────────────────────────────────────────────

def _tab_suppliers():
    st.markdown("### 🏭 Supplier Manager")
    st.caption("All Supplier-type parties — required for **Order to Supplier** in Backoffice.")

    # ── Inline edit form (shown above list when editing) ─────────────────────
    editing = st.session_state.get("sup_edit")
    if editing:
        ex = {} if editing == "NEW" else (fetch_party(editing) or {})
        label = "➕ New Supplier" if editing == "NEW" else f"✏️ Edit: {ex.get('party_name','')}"
        with st.container(border=True):
            st.markdown(f"**{label}**")
            submitted, data = _party_form(f"sup_form_{editing}", existing=ex)
            if st.button("✖ Cancel", key=f"sup_cancel_{editing}"):
                st.session_state.pop("sup_edit", None)
                st.rerun()
            if submitted:
                if not data.get("party_name","").strip():
                    st.error("Supplier name is required.")
                else:
                    data["party_type"] = "Supplier"
                    pid, err = save_party(data)
                    if pid:
                        st.success(f"✅ Saved: **{data['party_name']}**")
                        st.session_state.pop("sup_edit", None)
                        st.rerun()
                    else:
                        _show_save_error(err)
        st.markdown("---")

    if not editing:
        with st.expander("➕ Add New Supplier",
                         expanded=st.session_state.get("crm_open_sup_add", False)):
            render_supplier_quick_add()
        st.markdown("---")

    suppliers = fetch_suppliers()
    if not suppliers:
        st.warning("No suppliers yet. Add one above.")
        return

    st.success(f"✅ {len(suppliers)} supplier(s) ready for Order to Supplier")
    for s in suppliers:
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([4, 2, 2, 1])
            with c1:
                st.markdown(f"**{s.get('party_name','')}**")
                st.caption(f"📍 {s.get('city','') or '—'}  ·  {s.get('area','') or '—'}")
                if s.get("contact_person"):
                    st.caption(f"👤 {s['contact_person']}")
            with c2:
                if s.get("mobile"):   st.caption(f"📱 {s['mobile']}")
                if s.get("gstin"):    st.caption(f"GST: `{s['gstin']}`")
            with c3:
                if s.get("credit_limit"):
                    st.caption(f"💳 Limit: ₹{float(s['credit_limit']):,.0f}")
                if s.get("credit_days"):
                    st.caption(f"📅 {s['credit_days']} days")
            with c4:
                color = "#10b981" if s.get("is_active") else "#ef4444"
                label = "Active" if s.get("is_active") else "Inactive"
                st.markdown(
                    f"<span style='background:{color};color:#fff;padding:2px 8px;"
                    f"border-radius:8px;font-size:0.72rem'>{label}</span>",
                    unsafe_allow_html=True)
                if st.button("✏️ Edit", key=f"supedit_{s['id']}", use_container_width=True):
                    st.session_state["sup_edit"] = str(s["id"])
                    st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# TAB: PARTY MASTER
# ─────────────────────────────────────────────────────────────────────────────

def _tab_party_master():
    st.markdown("### 🗂️ Party Master")

    # Handle redirect from Supplier tab edit button
    if st.session_state.pop("pm_edit_from_sup", False):
        pass  # pm_edit already set

    col1, col2, col3 = st.columns([2, 3, 1])
    with col1:
        type_filter = st.selectbox("Type", ["All"] + PARTY_TYPES, key="pm_type")
    with col2:
        search = st.text_input("Search name / mobile / GSTIN", key="pm_search",
                               placeholder="Type to search…")
    with col3:
        st.markdown("<div style='margin-top:1.75rem'>", unsafe_allow_html=True)
        if st.button("➕ New Party", key="pm_new", use_container_width=True):
            st.session_state["pm_edit"] = "NEW"
        st.markdown("</div>", unsafe_allow_html=True)

    # ── Edit form ────────────────────────────────────────────────────────────
    editing = st.session_state.get("pm_edit")
    if editing:
        ex = {} if editing == "NEW" else (fetch_party(editing) or {})
        label = "➕ New Party" if editing == "NEW" else f"✏️ Edit: {ex.get('party_name','')}"
        with st.container(border=True):
            st.markdown(f"**{label}**")
            submitted, data = _party_form(f"pm_form_{editing}", existing=ex)
            if st.button("✖ Cancel", key=f"pm_cancel_{editing}"):
                st.session_state.pop("pm_edit", None)
                st.rerun()
            if submitted:
                if not data.get("party_name","").strip():
                    st.error("Party name is required.")
                else:
                    pid, err = save_party(data)
                    if pid:
                        st.success(f"✅ Saved: **{data['party_name']}**")
                        st.session_state.pop("pm_edit", None)
                        st.rerun()
                    else:
                        _show_save_error(err)

    # ── List ─────────────────────────────────────────────────────────────────
    parties = fetch_parties(type_filter if type_filter != "All" else None, search)
    if not parties:
        st.info("No parties found.")
        return

    counts = {}
    for p in parties:
        counts[p.get("party_type","?")] = counts.get(p.get("party_type","?"), 0) + 1
    pcols = st.columns(min(len(counts), 6))
    for i, (t, c) in enumerate(sorted(counts.items())):
        pcols[i % len(pcols)].metric(t, c)
    st.markdown("---")

    for p in parties:
        with st.container(border=True):
            c1, c2, c3, c4, c5 = st.columns([4, 2, 2, 2, 1])
            dot = "🟢" if p.get("is_active") else "🔴"
            with c1:
                st.markdown(f"{dot} **{p.get('party_name','')}**  "
                            f"<span style='color:#6b7280;font-size:0.78rem'>"
                            f"{p.get('party_type','')} | {p.get('city','') or '—'}</span>",
                            unsafe_allow_html=True)
                if p.get("contact_person"):
                    st.caption(f"👤 {p['contact_person']}")
            with c2:
                if p.get("mobile"):  st.caption(f"📱 {p['mobile']}")
                if p.get("email"):   st.caption(f"📧 {p['email']}")
            with c3:
                if p.get("gstin"):
                    st.caption(f"GSTIN: `{p['gstin']}`")
                if p.get("area"):
                    st.caption(f"📍 {p['area']}")
            with c4:
                if p.get("credit_limit"):
                    st.caption(f"💳 ₹{float(p['credit_limit']):,.0f} / {p.get('credit_days',0)}d")
            with c5:
                if st.button("✏️", key=f"pme_{p['id']}"):
                    st.session_state["pm_edit"] = str(p["id"])
                    st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# TAB: LEADS
# ─────────────────────────────────────────────────────────────────────────────

def _tab_leads():
    st.markdown("### 🎯 Lead Pipeline")
    stage_filter = st.selectbox("Stage", ["All"] + LEAD_STAGES, key="lead_f")

    with st.expander("➕ New Lead"):
        with st.form("lead_form"):
            lc1, lc2 = st.columns(2)
            with lc1:
                psearch     = st.text_input("Party search (name/mobile)")
                lead_source = st.selectbox("Source", LEAD_SOURCES)
                stage       = st.selectbox("Stage", LEAD_STAGES)
            with lc2:
                assigned = st.text_input("Assigned To")
                pot_val  = st.number_input("Potential Value (₹)", min_value=0.0, step=500.0)
                notes    = st.text_area("Notes", height=68)
            sub = st.form_submit_button("💾 Save Lead", type="primary")
        if sub:
            results = fetch_parties(search=psearch) if psearch else []
            if not results:
                st.error("Party not found — add via Party Master first.")
            else:
                lid, err = save_lead({"party_id": str(results[0]["id"]),
                                       "lead_source": lead_source, "stage": stage,
                                       "notes": notes, "assigned_to": assigned,
                                       "potential_value": pot_val})
                if lid: st.success(f"✅ Lead saved"); st.rerun()
                else:   st.error(f"❌ {err}")

    leads = fetch_leads(stage_filter)
    if not leads:
        st.info("No leads found."); return

    by_stage = {}
    for ld in leads:
        by_stage.setdefault(ld.get("stage","NEW"), []).append(ld)

    show = LEAD_STAGES if stage_filter == "All" else [stage_filter]
    for i in range(0, len(show), 3):
        cols = st.columns(len(show[i:i+3]))
        for col, stage in zip(cols, show[i:i+3]):
            color = STAGE_COLORS.get(stage, "#6b7280")
            sl    = by_stage.get(stage, [])
            with col:
                st.markdown(
                    f"<div style='background:{color};color:#fff;text-align:center;"
                    f"border-radius:6px;padding:4px;font-weight:700;margin-bottom:6px'>"
                    f"{stage} ({len(sl)})</div>", unsafe_allow_html=True)
                for ld in sl:
                    with st.container(border=True):
                        st.markdown(f"**{ld.get('party_name','?')}**")
                        if ld.get("potential_value"):
                            st.caption(f"₹{float(ld['potential_value']):,.0f}")
                        if ld.get("assigned_to"):
                            st.caption(f"👤 {ld['assigned_to']}")
                        ci = LEAD_STAGES.index(stage)
                        for ns in LEAD_STAGES[ci+1:ci+3]:
                            if st.button(f"→ {ns}", key=f"la_{ld['id']}_{ns}",
                                         use_container_width=True):
                                _, err = save_lead({**dict(ld), "id": str(ld["id"]),
                                                    "stage": ns})
                                if err: st.error(err)
                                else:   st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# TAB: FOLLOW-UPS
# ─────────────────────────────────────────────────────────────────────────────

def _tab_followups():
    st.markdown("### 📅 Follow-Up Tracker")
    all_p   = fetch_followups_all()
    overdue  = [f for f in all_p if f.get("due_date") and f["due_date"] <= date.today()]
    upcoming = [f for f in all_p if f not in overdue]

    if overdue:
        st.error(f"🔴 **{len(overdue)} overdue** follow-up(s)")
        for f in overdue:
            c1, c2, c3 = st.columns([5, 2, 1])
            with c1:
                icon = FU_ICONS.get(f.get("followup_type",""), "📌")
                st.markdown(f"{icon} **{f.get('party_name','?')}** — {f.get('followup_type','')}")
                st.caption(f"Due: {f.get('due_date','')}  ·  {(f.get('notes') or '')[:60]}")
            with c2: st.caption(f"📱 {f.get('mobile','') or '—'}")
            with c3:
                if st.button("✅", key=f"fod_{f['id']}"):
                    mark_followup_done(str(f["id"])); st.rerun()
        st.markdown("---")

    with st.expander("➕ Schedule Follow-Up"):
        with st.form("fu_form"):
            fc1, fc2 = st.columns(2)
            with fc1:
                fu_search = st.text_input("Party name / mobile")
                fu_type   = st.selectbox("Type", FOLLOWUP_TYPES)
            with fc2:
                fu_due   = st.date_input("Due Date", value=date.today() + timedelta(days=1))
                fu_notes = st.text_area("Notes", height=68)
            sub = st.form_submit_button("📅 Schedule", type="primary")
        if sub:
            results = fetch_parties(search=fu_search) if fu_search else []
            if not results:
                st.error("Party not found.")
            else:
                fid, err = save_followup({"party_id": str(results[0]["id"]),
                                          "followup_type": fu_type, "due_date": fu_due,
                                          "notes": fu_notes, "created_by": _user()})
                if fid: st.success(f"✅ Scheduled"); st.rerun()
                else:   st.error(f"❌ {err}")

    if not upcoming:
        st.info("🎉 No pending follow-ups."); return

    st.markdown("#### Upcoming")
    for f in upcoming:
        due  = f.get("due_date")
        late = due and due <= date.today()
        bg   = "#fffbeb" if not late else "#fef2f2"
        bdr  = "#f59e0b" if not late else "#dc2626"
        st.markdown(f"<div style='background:{bg};border-left:4px solid {bdr};"
                    f"padding:8px 12px;border-radius:4px;margin:3px 0'>",
                    unsafe_allow_html=True)
        uc1, uc2, uc3 = st.columns([5, 2, 1])
        with uc1:
            icon = FU_ICONS.get(f.get("followup_type",""), "📌")
            st.markdown(f"{icon} **{f.get('party_name','?')}**")
            st.caption(f"{(f.get('notes') or '')[:80]}")
        with uc2:
            st.caption(f"Due: **{due}**")
            st.caption(f"📱 {f.get('mobile','') or '—'}")
        with uc3:
            if st.button("✅", key=f"fup_{f['id']}", help="Mark Done"):
                mark_followup_done(str(f["id"])); st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# TAB: CONTACTS
# ─────────────────────────────────────────────────────────────────────────────

def _tab_contacts():
    st.markdown("### 📋 Contact Book")
    cc1, cc2 = st.columns([3, 1])
    with cc1:
        csearch = st.text_input("Search", key="cb_s", placeholder="Name, mobile or GSTIN…")
    with cc2:
        ctype = st.selectbox("Type", ["All"] + PARTY_TYPES, key="cb_t")

    parties = fetch_parties(ctype if ctype != "All" else None, csearch)
    if not parties:
        st.info("No contacts found."); return
    st.caption(f"{len(parties)} contact(s)")

    for p in parties:
        mob   = p.get("mobile") or "—"
        label = f"{p.get('party_name','')}  |  {p.get('party_type','')}  |  📱 {mob}"
        with st.expander(label):
            dc1, dc2 = st.columns([2, 3])
            with dc1:
                for field, val in [
                    ("City",    p.get("city")), ("Area",  p.get("area")),
                    ("Email",   p.get("email")), ("GSTIN", p.get("gstin")),
                    ("Credit Limit", f"₹{float(p['credit_limit']):,.0f}" if p.get("credit_limit") else None),
                    ("Credit Days",  str(p.get("credit_days","")) if p.get("credit_days") else None),
                ]:
                    if val: st.markdown(f"**{field}:** {val}")
            with dc2:
                tps = fetch_touchpoints(str(p["id"]))
                st.markdown("**Touchpoints**")
                if tps:
                    for tp in tps[:5]:
                        icon = {"NOTE":"📝","CALL":"📞","VISIT":"🚗",
                                "EMAIL":"📧","WHATSAPP":"💬"}.get(tp.get("type",""), "•")
                        ts   = str(tp.get("created_at",""))[:16]
                        st.caption(f"{icon} {ts} — {(tp.get('summary') or '')[:80]}")
                else:
                    st.caption("No touchpoints yet.")
                with st.form(f"tp_{p['id']}"):
                    t1, t2 = st.columns([1, 3])
                    with t1:
                        tp_type = st.selectbox("",
                            ["NOTE","CALL","VISIT","EMAIL","WHATSAPP"],
                            key=f"tpt_{p['id']}", label_visibility="collapsed")
                    with t2:
                        tp_text = st.text_input("", key=f"tps_{p['id']}",
                            label_visibility="collapsed", placeholder="Enter note…")
                    if st.form_submit_button("📝 Log"):
                        log_touchpoint(str(p["id"]), tp_type, tp_text, _user())
                        st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# METRICS BAR
# ─────────────────────────────────────────────────────────────────────────────

def _render_metrics():
    m = fetch_metrics()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("👥 Parties",           m["tp"])
    c2.metric("🏭 Suppliers",          m["ts"], help="Ready for Order to Supplier")
    c3.metric("🎯 Open Leads",         m["ol"])
    c4.metric("🔴 Overdue Follow-ups", m["of"],
              delta="Action needed" if m["of"] else "All clear",
              delta_color="inverse")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENTRY
# ─────────────────────────────────────────────────────────────────────────────

def render_crm_module():
    _bootstrap()
    st.subheader("🤝 CRM — Customer & Supplier Relationship Manager")

    with st.expander("🔌 Diagnostics", expanded=False):
        ca, cb = st.columns(2)
        with ca:
            try:
                from modules.sql_adapter import get_connection
                c = get_connection(); c.close()
                st.success("✅ DB connected")
            except Exception as e:
                st.error(f"❌ DB: {e}")
        with cb:
            cols = _q("SELECT column_name FROM information_schema.columns "
                      "WHERE table_name='parties' AND table_schema='public'", {})
            col_names = sorted(r.get("column_name","") for r in cols)
            st.caption("parties cols: " + ", ".join(col_names))
        if st.button("🔄 Clear Cache"):
            _bust(); st.rerun()

    _render_metrics()
    st.markdown("---")

    tabs = st.tabs(["🏭 Suppliers","🗂️ Party Master","🎯 Leads","📅 Follow-Ups","📋 Contacts"])
    with tabs[0]: _tab_suppliers()
    with tabs[1]: _tab_party_master()
    with tabs[2]: _tab_leads()
    with tabs[3]: _tab_followups()
    with tabs[4]: _tab_contacts()
