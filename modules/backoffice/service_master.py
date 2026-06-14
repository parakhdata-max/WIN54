"""Service master helpers for billing, production services and provider rates."""

from __future__ import annotations

from typing import Dict, List, Optional

_SERVICE_SCHEMA_READY = False
_SERVICE_DEFAULTS_READY = False

try:
    import streamlit as _st
    _cache_data = _st.cache_data
except Exception:
    def _cache_data(*dargs, **dkwargs):
        def _decorator(fn):
            return fn
        if dargs and callable(dargs[0]) and len(dargs) == 1 and not dkwargs:
            return dargs[0]
        return _decorator


def _q(sql: str, params: dict = None) -> List[Dict]:
    from modules.sql_adapter import run_query

    return run_query(sql, params or {}) or []


def _w(sql: str, params: dict = None):
    from modules.sql_adapter import run_write

    return run_write(sql, params or {})


def ensure_service_schema() -> None:
    """Best-effort schema availability for pages opened before migrations run."""
    global _SERVICE_SCHEMA_READY
    if _SERVICE_SCHEMA_READY:
        return

    _w(
        """
        CREATE TABLE IF NOT EXISTS service_types (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            service_code TEXT UNIQUE NOT NULL,
            service_group TEXT NOT NULL DEFAULT 'OTHER',
            service_name TEXT NOT NULL,
            retail_price NUMERIC(12,2) DEFAULT 0,
            wholesale_price NUMERIC(12,2) DEFAULT 0,
            gst_percent NUMERIC(5,2) DEFAULT 18,
            production_route TEXT DEFAULT '',
            sort_order INTEGER DEFAULT 100,
            is_active BOOLEAN DEFAULT TRUE,
            notes TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    _w(
        """
        CREATE TABLE IF NOT EXISTS service_providers (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            provider_name TEXT NOT NULL,
            provider_type TEXT NOT NULL DEFAULT 'FITTING',
            contact TEXT,
            address TEXT,
            gstin TEXT,
            gst_registered BOOLEAN DEFAULT FALSE,
            default_gst_percent NUMERIC(5,2) DEFAULT 0,
            is_active BOOLEAN DEFAULT TRUE,
            notes TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    for sql in [
        "ALTER TABLE service_providers ADD COLUMN IF NOT EXISTS contact TEXT",
        "ALTER TABLE service_providers ADD COLUMN IF NOT EXISTS address TEXT",
        "ALTER TABLE service_providers ADD COLUMN IF NOT EXISTS provider_type TEXT DEFAULT 'FITTING'",
        "ALTER TABLE service_providers ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
        "ALTER TABLE service_providers ADD COLUMN IF NOT EXISTS notes TEXT",
        "ALTER TABLE service_providers ADD COLUMN IF NOT EXISTS gstin TEXT",
        "ALTER TABLE service_providers ADD COLUMN IF NOT EXISTS gst_registered BOOLEAN DEFAULT FALSE",
        "ALTER TABLE service_providers ADD COLUMN IF NOT EXISTS default_gst_percent NUMERIC(5,2) DEFAULT 0",
        "ALTER TABLE parties ADD COLUMN IF NOT EXISTS preferred_courier_provider_id UUID",
        "ALTER TABLE parties ADD COLUMN IF NOT EXISTS preferred_courier_name TEXT",
    ]:
        try:
            _w(sql)
        except Exception:
            pass
    _w(
        """
        CREATE TABLE IF NOT EXISTS service_provider_rates (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            provider_id UUID NOT NULL REFERENCES service_providers(id) ON DELETE CASCADE,
            service_code TEXT NOT NULL REFERENCES service_types(service_code) ON DELETE CASCADE,
            purchase_rate NUMERIC(12,2) DEFAULT 0,
            effective_from DATE DEFAULT CURRENT_DATE,
            effective_to DATE,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(provider_id, service_code, effective_from)
        )
        """
    )
    _w(
        """
        CREATE TABLE IF NOT EXISTS party_service_rates (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            party_id UUID NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
            service_code TEXT NOT NULL REFERENCES service_types(service_code) ON DELETE CASCADE,
            price_mode TEXT NOT NULL DEFAULT 'FIXED',
            retail_price NUMERIC(12,2),
            wholesale_price NUMERIC(12,2),
            price_percent NUMERIC(7,2),
            is_active BOOLEAN DEFAULT TRUE,
            notes TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(party_id, service_code)
        )
        """
    )
    ensure_courier_rate_option_schema()
    try:
        _w(
            """
            INSERT INTO service_providers (
                id, provider_name, provider_type, contact, address, is_active, notes
            )
            SELECT f.id, f.fitter_name, 'COURIER', f.contact, f.address,
                   COALESCE(f.is_active, TRUE), f.notes
            FROM fitters f
            WHERE (
                    UPPER(COALESCE(f.fitter_type,'')) LIKE '%COURIER%'
                 OR UPPER(COALESCE(f.fitter_name,'')) LIKE '%COURIER%'
                 OR UPPER(COALESCE(f.fitter_name,'')) IN ('ANJANI','VIGHNESH')
            )
              AND NOT EXISTS (
                  SELECT 1 FROM service_providers sp WHERE sp.id = f.id
              )
            """
        )
    except Exception:
        pass
    _SERVICE_SCHEMA_READY = True


def ensure_courier_rate_option_schema() -> None:
    """Courier/provider parcel-size slabs used by dispatch."""
    _w(
        """
        CREATE TABLE IF NOT EXISTS courier_rate_options (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            provider_id UUID REFERENCES service_providers(id) ON DELETE CASCADE,
            option_label TEXT NOT NULL,
            parcel_size_code TEXT,
            charge_base NUMERIC(12,2) DEFAULT 0,
            gst_percent NUMERIC(5,2) DEFAULT 18,
            is_active BOOLEAN DEFAULT TRUE,
            sort_order INTEGER DEFAULT 100,
            notes TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    for sql in [
        "ALTER TABLE courier_rate_options ADD COLUMN IF NOT EXISTS provider_id UUID",
        "ALTER TABLE courier_rate_options ADD COLUMN IF NOT EXISTS option_label TEXT",
        "ALTER TABLE courier_rate_options ADD COLUMN IF NOT EXISTS parcel_size_code TEXT",
        "ALTER TABLE courier_rate_options ADD COLUMN IF NOT EXISTS charge_base NUMERIC(12,2) DEFAULT 0",
        "ALTER TABLE courier_rate_options ADD COLUMN IF NOT EXISTS gst_percent NUMERIC(5,2) DEFAULT 18",
        "ALTER TABLE courier_rate_options ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
        "ALTER TABLE courier_rate_options ADD COLUMN IF NOT EXISTS sort_order INTEGER DEFAULT 100",
        "ALTER TABLE courier_rate_options ADD COLUMN IF NOT EXISTS notes TEXT",
        "ALTER TABLE courier_rate_options ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()",
        "ALTER TABLE courier_rate_options ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()",
    ]:
        try:
            _w(sql)
        except Exception:
            pass


def seed_default_services() -> None:
    global _SERVICE_DEFAULTS_READY
    if _SERVICE_DEFAULTS_READY:
        return
    ensure_service_schema()
    _w(
        """
        INSERT INTO service_types
            (service_code, service_group, service_name, retail_price, wholesale_price, gst_percent, production_route, sort_order)
        VALUES
            ('COLOUR_LIGHT',   'COLOURING', 'Light Tint',          0, 0, 18, 'COLOURING', 10),
            ('COLOUR_MEDIUM',  'COLOURING', 'Medium Tint',         0, 0, 18, 'COLOURING', 20),
            ('COLOUR_DARK',    'COLOURING', 'Dark Tint',           0, 0, 18, 'COLOURING', 30),
            ('COLOUR_FASHION', 'COLOURING', 'Fashion Tint',        0, 0, 18, 'COLOURING', 40),
            ('FIT_STANDARD',   'FITTING',   'Standard Fitting',    0, 0, 18, 'FITTING',   110),
            ('FIT_SPECIAL',    'FITTING',   'Special Fitting',     0, 0, 18, 'FITTING',   120),
            ('FIT_DRILL',      'FITTING',   'Frame Drill Fitting', 0, 0, 18, 'FITTING',   130),
            ('FIT_RIMLESS',    'FITTING',   'Rimless Fitting',     0, 0, 18, 'FITTING',   140),
            ('COURIER_LOCAL',  'COURIER',   'Local Courier',       0, 0, 18, '',          210),
            ('COURIER_OUT',    'COURIER',   'Outstation Courier',  0, 0, 18, '',          220),
            ('COURIER_EXP',    'COURIER',   'Express Courier',     0, 0, 18, '',          230),
            ('HOME_CONSULT',   'OTHER',     'Home Consultation',   0, 0, 18, '',          310)
        ON CONFLICT (service_code) DO NOTHING
        """
    )
    _w(
        """
        UPDATE service_types
           SET gst_percent = 18,
               updated_at = NOW()
         WHERE UPPER(COALESCE(service_group,'')) = 'COURIER'
           AND COALESCE(gst_percent, 0) = 5
        """
    )
    _SERVICE_DEFAULTS_READY = True


def fetch_service_types(group: Optional[str] = None, active_only: bool = True) -> List[Dict]:
    seed_default_services()
    where = []
    params = {}
    if active_only:
        where.append("COALESCE(is_active,TRUE)=TRUE")
    if group:
        where.append("UPPER(service_group)=UPPER(%(g)s)")
        params["g"] = group
    sql = """
        SELECT id::text, service_code, service_group, service_name,
               COALESCE(retail_price,0)::numeric AS retail_price,
               COALESCE(wholesale_price,0)::numeric AS wholesale_price,
               COALESCE(gst_percent,0)::numeric AS gst_percent,
               COALESCE(production_route,'') AS production_route,
               COALESCE(sort_order,100) AS sort_order,
               COALESCE(is_active,TRUE) AS is_active,
               COALESCE(notes,'') AS notes
        FROM service_types
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY service_group, sort_order, service_name"
    return _q(sql, params)


def _master_service_price(service: Dict, order_type: str) -> float:
    if str(order_type or "").upper() == "WHOLESALE":
        return float(service.get("wholesale_price") or service.get("retail_price") or 0)
    return float(service.get("retail_price") or service.get("wholesale_price") or 0)


def service_price(service: Dict, order_type: str, party_id: Optional[str] = None) -> float:
    """Return master price, or party-specific override when configured."""
    base = _master_service_price(service, order_type)
    if not party_id:
        return base
    try:
        rows = _q(
            """
            SELECT price_mode,
                   COALESCE(retail_price, 0)::numeric AS retail_price,
                   COALESCE(wholesale_price, 0)::numeric AS wholesale_price,
                   COALESCE(price_percent, 0)::numeric AS price_percent
            FROM party_service_rates
            WHERE party_id=%(pid)s::uuid
              AND service_code=%(code)s
              AND COALESCE(is_active, TRUE)=TRUE
            LIMIT 1
            """,
            {"pid": party_id, "code": service.get("service_code")},
        )
        if not rows:
            return base
        row = rows[0]
        mode = str(row.get("price_mode") or "FIXED").upper()
        if mode == "PERCENT_OF_MASTER":
            return round(base * float(row.get("price_percent") or 0) / 100, 2)
        if str(order_type or "").upper() == "WHOLESALE":
            return float(row.get("wholesale_price") or row.get("retail_price") or base)
        return float(row.get("retail_price") or row.get("wholesale_price") or base)
    except Exception:
        return base


def fetch_party_service_rates(party_id: str) -> List[Dict]:
    ensure_service_schema()
    if not party_id:
        return []
    return _q(
        """
        SELECT psr.id::text, psr.party_id::text, psr.service_code,
               st.service_group, st.service_name,
               COALESCE(psr.price_mode, 'FIXED') AS price_mode,
               COALESCE(psr.retail_price, 0)::numeric AS retail_price,
               COALESCE(psr.wholesale_price, 0)::numeric AS wholesale_price,
               COALESCE(psr.price_percent, 0)::numeric AS price_percent,
               COALESCE(psr.is_active, TRUE) AS is_active,
               COALESCE(psr.notes, '') AS notes
        FROM party_service_rates psr
        JOIN service_types st ON st.service_code = psr.service_code
        WHERE psr.party_id=%(pid)s::uuid
        ORDER BY st.service_group, st.sort_order, st.service_name
        """,
        {"pid": party_id},
    )


def upsert_party_service_rate(
    party_id: str,
    service_code: str,
    price_mode: str,
    retail_price: float,
    wholesale_price: float,
    price_percent: float,
    is_active: bool,
    notes: str = "",
) -> None:
    ensure_service_schema()
    _w(
        """
        INSERT INTO party_service_rates
            (party_id, service_code, price_mode, retail_price, wholesale_price,
             price_percent, is_active, notes)
        VALUES
            (%(pid)s::uuid, %(code)s, %(mode)s, %(rp)s, %(wp)s,
             %(pct)s, %(active)s, %(notes)s)
        ON CONFLICT (party_id, service_code) DO UPDATE SET
            price_mode=%(mode)s,
            retail_price=%(rp)s,
            wholesale_price=%(wp)s,
            price_percent=%(pct)s,
            is_active=%(active)s,
            notes=%(notes)s,
            updated_at=NOW()
        """,
        {
            "pid": party_id,
            "code": service_code,
            "mode": price_mode,
            "rp": retail_price,
            "wp": wholesale_price,
            "pct": price_percent,
            "active": is_active,
            "notes": notes or "",
        },
    )


def update_service_type(
    service_code: str,
    service_name: str,
    retail_price: float,
    wholesale_price: float,
    gst_percent: float,
    production_route: str,
    is_active: bool,
    notes: str = "",
) -> None:
    """Update an existing service row and sync fitting labels for legacy production paths."""
    ensure_service_schema()
    code = str(service_code or "").upper().strip()
    name = str(service_name or "").strip()
    route = str(production_route or "").upper().strip()
    _w(
        """
        UPDATE service_types
        SET service_name=%(n)s,
            retail_price=%(rp)s,
            wholesale_price=%(wp)s,
            gst_percent=%(gst)s,
            production_route=%(route)s,
            is_active=%(active)s,
            notes=%(notes)s,
            updated_at=NOW()
        WHERE service_code=%(code)s
        """,
        {
            "code": code,
            "n": name,
            "rp": float(retail_price or 0),
            "wp": float(wholesale_price or 0),
            "gst": float(gst_percent or 0),
            "route": route,
            "active": bool(is_active),
            "notes": notes or "",
        },
    )
    # Legacy compatibility: production fitting assignments still store
    # fitting_type_code; keep the visible label in sync for old screens/reports.
    if route == "FITTING" or code.startswith("FIT"):
        try:
            _w(
                """
                INSERT INTO fitting_types (code, label, sort_order, is_active)
                VALUES (
                    %(code)s, %(n)s,
                    (SELECT COALESCE(MAX(sort_order),0)+10 FROM fitting_types),
                    %(active)s
                )
                ON CONFLICT (code) DO UPDATE SET
                    label=%(n)s,
                    is_active=%(active)s
                """,
                {"code": code, "n": name, "active": bool(is_active)},
            )
        except Exception:
            pass


@_cache_data(ttl=10, show_spinner=False)
def fetch_providers(provider_type: Optional[str] = None, active_only: bool = True) -> List[Dict]:
    ensure_service_schema()
    where = []
    params = {}
    if active_only:
        where.append("COALESCE(is_active,TRUE)=TRUE")
    if provider_type:
        if str(provider_type).upper() == "COURIER":
            where.append("""
                (
                    UPPER(COALESCE(provider_type,'')) = 'COURIER'
                    OR UPPER(COALESCE(provider_type,'')) LIKE '%COURIER%'
                    OR EXISTS (
                        SELECT 1
                        FROM service_provider_rates spr
                        JOIN service_types st ON st.service_code = spr.service_code
                        WHERE spr.provider_id = service_providers.id
                          AND UPPER(COALESCE(st.service_group,'')) = 'COURIER'
                          AND COALESCE(spr.is_active, TRUE) = TRUE
                    )
                )
            """)
        else:
            params["t"] = provider_type
            where.append("UPPER(provider_type)=UPPER(%(t)s)")
    sql = """
        SELECT id::text, provider_name, provider_type, contact, address,
               COALESCE(gstin,'') AS gstin,
               COALESCE(gst_registered,FALSE) AS gst_registered,
               COALESCE(default_gst_percent,0)::numeric AS default_gst_percent,
               COALESCE(is_active,TRUE) AS is_active, COALESCE(notes,'') AS notes
        FROM service_providers
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY provider_type, provider_name"
    return _q(sql, params)


def fetch_provider_rates(provider_id: str) -> List[Dict]:
    ensure_service_schema()
    return _q(
        """
        SELECT spr.id::text, spr.service_code, st.service_name, st.service_group,
               COALESCE(spr.purchase_rate,0)::numeric AS purchase_rate,
               spr.effective_from::text, spr.effective_to::text
        FROM service_provider_rates spr
        JOIN service_types st ON st.service_code = spr.service_code
        WHERE spr.provider_id=%(pid)s::uuid
          AND COALESCE(spr.is_active,TRUE)=TRUE
          AND (spr.effective_to IS NULL OR spr.effective_to >= CURRENT_DATE)
        ORDER BY st.service_group, st.sort_order, spr.effective_from DESC
        """,
        {"pid": provider_id},
    )


def fetch_courier_rate_options(provider_id: str, active_only: bool = True) -> List[Dict]:
    ensure_courier_rate_option_schema()
    if not provider_id:
        return []
    where = ["provider_id=%(pid)s::uuid"]
    if active_only:
        where.append("COALESCE(is_active, TRUE)=TRUE")
    return _q(
        f"""
        SELECT id::text,
               provider_id::text,
               COALESCE(option_label,'') AS option_label,
               COALESCE(parcel_size_code,'') AS parcel_size_code,
               COALESCE(charge_base,0)::numeric AS charge_base,
               COALESCE(gst_percent,0)::numeric AS gst_percent,
               COALESCE(is_active,TRUE) AS is_active,
               COALESCE(sort_order,100) AS sort_order,
               COALESCE(notes,'') AS notes
        FROM courier_rate_options
        WHERE {" AND ".join(where)}
        ORDER BY COALESCE(sort_order,100), option_label
        """,
        {"pid": provider_id},
    )


def save_courier_rate_option(
    *,
    provider_id: str,
    option_label: str,
    parcel_size_code: str = "",
    charge_base: float = 0.0,
    gst_percent: float = 18.0,
    is_active: bool = True,
    sort_order: int = 100,
    notes: str = "",
    option_id: str = "",
):
    ensure_courier_rate_option_schema()
    params = {
        "pid": provider_id or "",
        "label": str(option_label or "").strip(),
        "code": str(parcel_size_code or "").strip().upper() or None,
        "base": round(float(charge_base or 0), 2),
        "gst": round(float(gst_percent or 0), 2),
        "active": bool(is_active),
        "sort": int(sort_order or 100),
        "notes": str(notes or "").strip() or None,
        "id": option_id or "",
    }
    if not params["pid"] or not params["label"]:
        return False
    if option_id:
        return _w(
            """
            UPDATE courier_rate_options
               SET option_label=%(label)s,
                   parcel_size_code=%(code)s,
                   charge_base=%(base)s,
                   gst_percent=%(gst)s,
                   is_active=%(active)s,
                   sort_order=%(sort)s,
                   notes=%(notes)s,
                   updated_at=NOW()
             WHERE id=%(id)s::uuid
               AND provider_id=%(pid)s::uuid
            """,
            params,
        )
    return _w(
        """
        INSERT INTO courier_rate_options (
            provider_id, option_label, parcel_size_code, charge_base,
            gst_percent, is_active, sort_order, notes
        ) VALUES (
            %(pid)s::uuid, %(label)s, %(code)s, %(base)s,
            %(gst)s, %(active)s, %(sort)s, %(notes)s
        )
        """,
        params,
    )


def suggested_provider_for_service(service_code: str) -> Optional[Dict]:
    rows = _q(
        """
        SELECT sp.id::text, sp.provider_name, sp.provider_type, sp.contact,
               COALESCE(spr.purchase_rate,0)::numeric AS purchase_rate
        FROM service_provider_rates spr
        JOIN service_providers sp ON sp.id = spr.provider_id
        WHERE spr.service_code=%(c)s
          AND COALESCE(sp.is_active,TRUE)=TRUE
          AND COALESCE(spr.is_active,TRUE)=TRUE
          AND (spr.effective_to IS NULL OR spr.effective_to >= CURRENT_DATE)
        ORDER BY spr.effective_from DESC, spr.purchase_rate ASC
        LIMIT 1
        """,
        {"c": service_code},
    )
    return rows[0] if rows else None
