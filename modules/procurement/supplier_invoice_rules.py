"""
modules/procurement/supplier_invoice_rules.py
=============================================
Supplier Invoice Matching Engine — optical-domain intelligence.

MATCHING PRIORITY (correct order):
  1. Exact alias memory    (deterministic, fastest)
  2. Historical map        (relational, from product_supplier_map)
  3. Normalise + fuzzy     (token cleaning, abbreviation expansion)
  4. AI semantic fallback  (Claude, only when key available)
  5. difflib fallback      (last resort)

AI is the LAST intelligent fallback, not the primary engine.
"""
from __future__ import annotations
import json, re, logging, os
from typing import Any, Dict, List, Optional, Tuple

_log = logging.getLogger(__name__)

# ── Pack-size / abbreviation normalisation dictionary ─────────────────────────
_ABBREV_EXPAND = {
    r"\bAIROPTIX\b": "AIR OPTIX",
    r"\bAIROPT\b":   "AIR OPTIX",
    r"\bA/O\b":      "AIR OPTIX",
    r"\bAO\b":       "AIR OPTIX",
    r"\bHG\b":       "HYDRAGLYDE",
    r"\bASTG\b":     "TORIC",
    r"\bSPH\b":      "SPHERE",
    r"\bPROG\b":     "PROGRESSIVE",
    r"\bFREEFORM\b": "FREEFORM",
    r"\bCLEAR ARC\b":"CLEARARC",
    r"\bRX\b":       "RX",
    r"\bHC\b":       "HARDCOAT",
    r"\bARC\b":      "ANTIREFLECTION",
    r"\bBLUE\b":     "BLUECUT",
    r"\bBCUT\b":     "BLUECUT",
    r"\b3P\b":       "3PACK",
    r"\b6P\b":       "6PACK",
    r"\b6PK\b":      "6PACK",
    r"\b6 PK\b":     "6PACK",
}
# Tokens that describe pack size / quantity — remove before matching
_PACK_SIZE_RE = re.compile(
    r"\b(\d+\s*PK|\d+PACK|\d+\s*P\b|"
    r"\d+\s*(PAIR|PCS|PIECES|BOX|BOXES|PC)\b|"
    r"\b(PAIR|PCS|PIECES|BOX)\b)",
    re.I,
)
# Numeric power patterns — strip before name matching
_POWER_STRIP_RE = re.compile(
    r"[+-]?\d{1,3}\.\d{2}|"        # decimal powers  (+1.50, -03.00)
    r"\b\d{3}\b|"                   # 3-digit codes   (870, 145, 180)
    r"\b\d{2}\.\d{2}\b",            # 2dp codes       (8.70, 14.50)
    re.I,
)

# ── Built-in per-supplier configs ─────────────────────────────────────────────
_BUILTIN: Dict[str, Dict[str, Any]] = {
    "alcon": {
        "power_format":    "ALCON_TORIC",
        "cyl_sign":        "ALWAYS_NEGATIVE",
        "qty_unit":        "PCS",
        "product_aliases": {
            "AIROPT ASTG HG":     "Air Optix Toric",
            "AIROPTIX ASTG HG":   "Air Optix Toric",
            "AIROPT AQ HG SPH":   "Air Optix Hydraglyde SPH 6PK",
            "AIROPTIX AQ HG SPH": "Air Optix Hydraglyde SPH 6PK",
            "FRESHLOOK 1-DAY":    "FreshLook 1-Day",
        },
        "field_patterns": {
            "invoice_no": r"\b(9\d{9})\b",
            "date":       r"Due date\s*\|?\s*(\d{2}\.\d{2}\.\d{4})",
        },
        "notes": (
            "Alcon: CYL always negative (125 = -1.25). "
            "Batch: 8-digit / DD.MM.YYYY / CIBA VISION JOHOR. HSN 90013000. "
            "AIROPTIX AQ HG SPH = Air Optix Hydraglyde, ASTG = Toric."
        ),
    },
    "bonzer": {
        "power_format":    "BONZER_RL",
        "cyl_sign":        "AS_WRITTEN",
        "qty_unit":        "PAIRS",
        "product_aliases": {},
        "field_patterns": {
            "invoice_no": r"Invoice No\s*\|?\s*([A-Z0-9\-/]+)",
            "date":       r"Date\s*\|?\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
        },
        "notes": (
            "Bonzer: power [R] sph cyl axis add [L] sph cyl axis add. "
            "Qty in pairs (1 pair = 2 pcs). Custom hex PDF font."
        ),
    },
    "shamir": {
        "power_format":    "MERGED_RL",
        "cyl_sign":        "AS_WRITTEN",
        "qty_unit":        "PCS",
        "product_aliases": {
            "ALFA RX FREEFORM PROG": "Autograph Spectrum+™",
            "ALFA RX FREEFORM":      "Autograph Spectrum+™",
        },
        "field_patterns": {
            "invoice_no": r"Invoice\s*(?:No\.?)?\s*:?\s*([A-Z0-9\-/]+)",
            "date":       r"Date\s*:?\s*(\d{2}[/-]\d{2}[/-]\d{4})",
        },
        "notes": (
            "Shamir: product and power merged. Split at [R]. "
            "ALFA RX FREEFORM PROG 1.50 CLEAR ARC → product name before [R]."
        ),
    },
    "default": {
        "power_format": "GENERIC", "cyl_sign": "AS_WRITTEN", "qty_unit": "PCS",
        "product_aliases": {}, "field_patterns": {}, "notes": "",
    },
}

# ── DB helpers ────────────────────────────────────────────────────────────────
def _q(sql, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        _log.warning("[sir] q: %s", e); return []

def _w(sql, params=None):
    try:
        from modules.sql_adapter import run_write
        run_write(sql, params or {}); return True
    except Exception as e:
        _log.warning("[sir] w: %s", e); return False

def ensure_table():
    _w("""CREATE TABLE IF NOT EXISTS supplier_invoice_rules (
            id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            supplier_id   uuid,
            supplier_name text NOT NULL,
            rules         jsonb NOT NULL DEFAULT '{}',
            version       integer NOT NULL DEFAULT 1,
            created_at    timestamptz DEFAULT NOW(),
            updated_at    timestamptz DEFAULT NOW(),
            created_by    text)""")
    _w("CREATE INDEX IF NOT EXISTS idx_sir_name "
       "ON supplier_invoice_rules(LOWER(supplier_name))")

def _bkey(name: str) -> str:
    n = str(name or "").lower()
    for k in ("alcon","bonzer","shamir"): 
        if k in n: return k
    return "default"

def get_rules(supplier_name: str = "", supplier_id: str = "") -> Dict[str, Any]:
    rows = []
    if supplier_id:
        rows = _q("SELECT rules FROM supplier_invoice_rules "
                   "WHERE supplier_id=%(s)s::uuid LIMIT 1", {"s": supplier_id})
    if not rows and supplier_name:
        rows = _q("SELECT rules FROM supplier_invoice_rules "
                   "WHERE LOWER(supplier_name) LIKE %(n)s ORDER BY updated_at DESC LIMIT 1",
                   {"n": f"%{supplier_name.strip().lower()[:20]}%"})
    if rows:
        r = rows[0].get("rules") or {}
        return json.loads(r) if isinstance(r, str) else dict(r)
    return dict(_BUILTIN.get(_bkey(supplier_name), _BUILTIN["default"]))

def save_rules(supplier_name: str, rules: Dict[str, Any],
               supplier_id: str = "", created_by: str = "staff") -> bool:
    ensure_table()
    rj = json.dumps(rules)
    ex = _q("SELECT id FROM supplier_invoice_rules "
             "WHERE LOWER(supplier_name)=LOWER(%(n)s) LIMIT 1", {"n": supplier_name})
    if ex:
        return _w("UPDATE supplier_invoice_rules SET rules=%(r)s::jsonb, version=version+1,"
                   "updated_at=NOW(), created_by=%(b)s WHERE LOWER(supplier_name)=LOWER(%(n)s)",
                   {"r": rj, "b": created_by, "n": supplier_name})
    return _w("INSERT INTO supplier_invoice_rules (supplier_name, supplier_id, rules, created_by) "
               "VALUES (%(n)s, NULLIF(%(s)s,'')::uuid, %(r)s::jsonb, %(b)s)",
               {"n": supplier_name, "s": supplier_id or "", "r": rj, "b": created_by})

def list_all_rules() -> List[Dict[str, Any]]:
    ensure_table()
    return _q("SELECT id::text, supplier_name, supplier_id::text, version, rules, "
               "updated_at::text FROM supplier_invoice_rules ORDER BY supplier_name")

# ── Normalisation engine ──────────────────────────────────────────────────────

def normalise_product_text(text: str) -> str:
    """
    Canonical normalisation before matching:
    1. Uppercase
    2. Strip power values (numeric)
    3. Strip pack-size tokens
    4. Expand abbreviations
    5. Collapse whitespace
    """
    t = str(text or "").upper().strip()
    # Strip power numbers first
    t = _POWER_STRIP_RE.sub(" ", t)
    # Strip pack-size tokens
    t = _PACK_SIZE_RE.sub(" ", t)
    # Expand abbreviations
    for pat, repl in _ABBREV_EXPAND.items():
        t = re.sub(pat, repl, t, flags=re.I)
    # Collapse whitespace
    return " ".join(t.split())


# ── Matching engine (priority order) ─────────────────────────────────────────

def ai_match_product(
    invoice_text: str,
    products: List[Dict[str, Any]],
    rules: Dict[str, Any],
    supplier_id: str = "",
) -> Dict[str, Any]:
    """
    Returns {product_id, product_name, confidence, method, reasoning}.

    Confidence tiers:
      >= 0.95  → auto-accept (✅)
      0.80-0.95 → amber confirm (⚠️)
      < 0.80   → mandatory manual selection (❌)
    """
    text = str(invoice_text or "").strip()
    norm = normalise_product_text(text)

    # ── Tier 0: Bonzer structured matching (index + coating + treatment) ─────
    # Runs before alias lookup for Bonzer invoices because the description
    # encodes index/coating/treatment which text fuzzy cannot reliably decode.
    _pwr_fmt = str(rules.get("power_format","")).upper()
    if _pwr_fmt == "BONZER_RL" or "bonzer" in str(rules.get("notes","")).lower():
        _bz = match_bonzer_to_product(text, products)
        if _bz.get("confidence", 0) >= 0.60:
            return _bz

    # ── Tier 1: exact alias memory ────────────────────────────────────────────
    for alias, our_name in (rules.get("product_aliases") or {}).items():
        if alias.upper() in text.upper() or alias.upper() in norm:
            for p in products:
                if str(p.get("product_name","")).lower() == our_name.lower():
                    return _hit(p, 0.97, "ALIAS", alias)

    # ── Tier 2: historical supplier_product_map ───────────────────────────────
    if supplier_id:
        hist = _load_supplier_map(supplier_id)
        for row in hist:
            sup_item = str(row.get("supplier_product_name") or "")
            if not sup_item: continue
            sup_norm = normalise_product_text(sup_item)
            # Exact normalised match
            if sup_norm == norm or sup_item.upper() == text.upper():
                pid = str(row.get("product_id") or "")
                for p in products:
                    if str(p["id"]) == pid:
                        return _hit(p, 0.96, "HISTORY", sup_item)
            # Partial normalised match (text contains the key)
            if sup_norm and (sup_norm in norm or norm in sup_norm) and len(sup_norm) > 5:
                pid = str(row.get("product_id") or "")
                for p in products:
                    if str(p["id"]) == pid:
                        return _hit(p, 0.88, "HISTORY_PARTIAL", sup_item)

    # ── Tier 3: normalise + token fuzzy ──────────────────────────────────────
    import difflib
    best_id, best_score, best_p = "", 0.0, {}
    for p in products:
        pnorm = normalise_product_text(f"{p.get('brand','')} {p.get('product_name','')}")
        score = difflib.SequenceMatcher(None, norm, pnorm).ratio()
        # Token containment bonus
        toks = [t for t in norm.split() if len(t) > 3]
        for tok in toks:
            if tok in pnorm: score += 0.08
        if score > best_score:
            best_score, best_id, best_p = score, str(p["id"]), p
    if best_score >= 0.55:
        return _hit(best_p, round(min(best_score, 0.89), 2), "NORMALISE", norm)

    # ── Tier 4: Claude AI semantic fallback ───────────────────────────────────
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key and products:
        result = _claude_match(text, products, rules, api_key)
        if result and result.get("confidence", 0) >= 0.6:
            return result

    # ── Tier 5: difflib fallback (raw text) ───────────────────────────────────
    best_id, best_score, best_p = "", 0.0, {}
    tl = text.lower()
    for p in products:
        hay   = f"{p.get('brand','')} {p.get('product_name','')}".lower()
        score = difflib.SequenceMatcher(None, tl, hay).ratio()
        pn    = str(p.get("product_name","")).lower()
        if pn and pn in tl: score += 0.2
        if score > best_score:
            best_score, best_id, best_p = score, str(p["id"]), p
    if best_score >= 0.38:
        return _hit(best_p, round(best_score, 2), "DIFFLIB", "")
    return {"product_id":"","product_name":"","confidence":0.0,"method":"NO_MATCH","reasoning":""}


def _hit(p: Dict, conf: float, method: str, reasoning: str) -> Dict[str, Any]:
    return {"product_id": str(p["id"]), "product_name": str(p.get("product_name","")),
            "confidence": conf, "method": method, "reasoning": reasoning}


def _load_supplier_map(supplier_id: str) -> List[Dict[str, Any]]:
    """Load historical supplier product mappings ordered by usage."""
    return _q("""
        SELECT psm.product_id::text, psm.supplier_product_name,
               COALESCE(psm.usage_count, 1) AS usage_count
        FROM product_supplier_map psm
        WHERE psm.supplier_id = %(sid)s::uuid
          AND COALESCE(psm.is_active, TRUE) = TRUE
          AND psm.supplier_product_name IS NOT NULL
          AND psm.supplier_product_name != ''
        ORDER BY COALESCE(psm.usage_count, 1) DESC, psm.updated_at DESC
        LIMIT 200
    """, {"sid": supplier_id})


def _claude_match(text: str, products: List[Dict[str, Any]],
                  rules: Dict[str, Any], api_key: str) -> Optional[Dict[str, Any]]:
    try:
        import requests, json as _j
        prod_list = [{"id": str(p["id"]),
                      "name": f"{p.get('brand','')} {p.get('product_name','')}".strip()}
                     for p in products[:300]]
        prompt = (
            f"Match this optical supplier invoice line to a product in the ERP database.\n\n"
            f"Invoice line: {text!r}\n"
            f"Normalised: {normalise_product_text(text)!r}\n\n"
            f"Supplier notes: {rules.get('notes','')}\n"
            f"CYL rule: {rules.get('cyl_sign','AS_WRITTEN')}\n\n"
            f"Products:\n"
            + "\n".join(f"- {p['id']}: {p['name']}" for p in prod_list)
            + "\n\nRespond ONLY with JSON: "
              "{\"product_id\": \"uuid or empty\", \"product_name\": \"name\", "
              "\"confidence\": 0.0-1.0, \"reasoning\": \"brief\"}"
        )
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "Content-Type": "application/json",
                     "anthropic-version": "2023-06-01"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 256,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=10,
        )
        resp.raise_for_status()
        raw  = resp.json()["content"][0]["text"].strip()
        raw  = re.sub(r"^```[a-z]*\n?|```$", "", raw, flags=re.M).strip()
        data = _j.loads(raw)
        return {"product_id": str(data.get("product_id") or ""),
                "product_name": str(data.get("product_name") or ""),
                "confidence": float(data.get("confidence") or 0),
                "method": "CLAUDE_AI",
                "reasoning": str(data.get("reasoning") or "")}
    except Exception as exc:
        _log.debug("[sir] claude: %s", exc); return None

# ── Power parsers ─────────────────────────────────────────────────────────────

def parse_power_merged_rl(text: str) -> Dict[str, Any]:
    t = str(text or "").replace("\xa0"," ").strip()
    m = re.search(r"\[R\]\s*(.*?)\s*\[L\]\s*(.+)$", t, re.I)
    if not m:
        return {"product_name": t, "right": {}, "left": {}}
    return {"product_name": t[:m.start()].strip(" -"),
            "right": _rx_seg(m.group(1)), "left": _rx_seg(m.group(2))}

def parse_power_alcon_toric(desc: str, cyl_sign: str = "ALWAYS_NEGATIVE") -> Dict[str, Any]:
    d = str(desc or "").upper()
    m = re.search(r"(\d{3})\s+(\d{3})\s+([-+]?\d{2}\.\d{2})\s+(\d+)\s+(\d{1,3})", d)
    if not m: return {}
    cyl_raw = float(m.group(4))
    return {"bc": f"{int(m.group(1))/100:.2f}", "dia": f"{int(m.group(2))/10:.1f}",
            "sph": round(float(m.group(3)),2),
            "cyl": -round(cyl_raw/100,2) if cyl_sign=="ALWAYS_NEGATIVE" else round(cyl_raw,2),
            "axis": int(m.group(5))}

def _rx_seg(seg: str) -> Dict[str, Any]:
    s = str(seg or "").replace("°"," ").replace(",",".")
    nums = re.findall(r"[+-]?\d+(?:\.\d+)?", s)
    signed = [n for n in nums if n.startswith(("+","-"))]
    unsigned = [n for n in nums if not n.startswith(("+","-"))]
    out: Dict[str, Any] = {}
    try:
        if signed:           out["sph"]  = float(signed[0])
        if len(signed)>=2:   out["cyl"]  = float(signed[1])
        if len(signed)>=3:   out["add"]  = float(signed[2])
        for u in unsigned:
            v = int(float(u))
            if 1<=v<=180: out["axis"]=v; break
    except Exception: pass
    return out

# ── Supplier + header helpers ─────────────────────────────────────────────────

def resolve_supplier(header: Dict[str, Any], parties: List[Dict[str, Any]]) -> Tuple[Optional[Dict],float]:
    sup_text = str(header.get("supplier") or "").strip().lower()
    if not sup_text: return None, 0.0
    import difflib
    best, bs = None, 0.0
    for p in parties:
        nl = str(p.get("party_name") or "").lower()
        score = difflib.SequenceMatcher(None, sup_text, nl).ratio()
        for w in (w for w in sup_text.split() if len(w)>3):
            if w in nl: score += 0.12
        if score > bs: bs, best = score, p
    return (best, round(min(bs,1.0),2)) if bs >= 0.4 else (None, 0.0)

def extract_header_fields(raw_text: str, rules: Dict[str, Any]) -> Dict[str, Any]:
    patterns = rules.get("field_patterns") or {}
    out = {"invoice_no":"","date":""}
    for field in ("invoice_no","date"):
        pat = patterns.get(field)
        if pat:
            m = re.search(pat, raw_text, re.I)
            if m: out[field] = m.group(1).strip()
    if not out["invoice_no"]:
        for pat in [r"Invoice\s*No\.?\s*:?\s*([A-Z0-9\-/]+)",
                    r"Challan\s*No\.?\s*:?\s*([A-Z0-9\-/]+)", r"\b(9\d{9})\b"]:
            m = re.search(pat, raw_text, re.I)
            if m: out["invoice_no"]=m.group(1).strip(); break
    if not out["date"]:
        for pat in [r"\b(\d{2}[./-]\d{2}[./-]\d{4})\b",
                    r"\b(\d{1,2}\s+[A-Za-z]{3,}\s+\d{4})\b"]:
            m = re.search(pat, raw_text)
            if m: out["date"]=m.group(1).strip(); break
    return out


# ── Power-aware inventory_stock matching ──────────────────────────────────────

def match_to_inventory_stock(
    product_id: str,
    power: Dict[str, Any],
    is_cl_product: bool = True,
) -> Optional[Dict[str, Any]]:
    """
    For power-stocked products (contact lenses): find the exact inventory_stock
    row matching product_id + sph + cyl + axis + bc + dia.

    For custom-Rx products (ophthalmic): return None — no stock row exists.

    Returns inventory_stock row dict or None.
    """
    if not product_id or not is_cl_product:
        return None

    sph  = power.get("sph")
    cyl  = power.get("cyl")
    axis = power.get("axis")
    bc   = power.get("bc")
    dia  = power.get("dia")
    add  = power.get("add")

    # Build a tolerant power match query (±0.01 dioptre tolerance for float rounding)
    conditions = ["product_id = %(pid)s::uuid",
                  "COALESCE(is_active, TRUE) = TRUE"]
    params: Dict[str, Any] = {"pid": product_id}

    if sph is not None:
        conditions.append("ABS(COALESCE(sph, 0) - %(sph)s) < 0.02")
        params["sph"] = float(sph)
    if cyl is not None and abs(float(cyl)) > 0.01:
        conditions.append("ABS(COALESCE(cyl, 0) - %(cyl)s) < 0.02")
        params["cyl"] = float(cyl)
    if axis is not None and int(axis) > 0:
        conditions.append("COALESCE(axis, 0) = %(axis)s")
        params["axis"] = int(axis)
    # base_curve is on products table, not inventory_stock — skip for stock matching
    if add is not None and abs(float(add)) > 0.01:
        conditions.append("ABS(COALESCE(add_power, 0) - %(add)s) < 0.02")
        params["add"] = float(add)

    sql = (
        "SELECT id::text, batch_no, COALESCE(quantity,0)-COALESCE(allocated_qty,0) AS avail_qty, "
        "       sph, cyl, axis, add_power, "
        "       COALESCE(quantity,0)-COALESCE(allocated_qty,0) AS avail_qty, ""       colour, expiry_date::text, purchase_price, selling_price "
        "FROM inventory_stock "
        f"WHERE {' AND '.join(conditions)} "
        "ORDER BY "
        "  COALESCE(quantity,0)-COALESCE(allocated_qty,0) DESC, "
        "  updated_at DESC NULLS LAST LIMIT 1"
    )
    rows = _q(sql, params)
    return rows[0] if rows else None


def is_cl_product(product_id: str, products: List[Dict[str, Any]]) -> bool:
    """Returns True if this product is a contact lens (stocked by power in inventory_stock)."""
    for p in products:
        if str(p.get("id","")) == product_id:
            mg = str(p.get("main_group","")).lower()
            cat = str(p.get("category","")).lower()
            return ("contact" in mg or "contact" in cat or
                    "cl" in mg or "lens" in cat)
    return False


# ── Bonzer structured description parser ─────────────────────────────────────
# Bonzer description format:
#   [TYPE] [INDEX] [COATING] [PRODUCT_NAME] [TREATMENT] [R/L power]
#   e.g. "PROG 1.56 BB EASY SURE MURK ARC [R] ..."
#        "BB 1.56 NEXIS AI CORE - NIGHTOLUX [R] ..."
#        "ALFA RX FF PROG 1 50 CLEAR PREMIUM HC [R] ..."

_BONZER_INDEX_MAP = {
    "1 50": 1.50, "1.50": 1.50, "150": 1.50,
    "1 56": 1.56, "1.56": 1.56, "156": 1.56,
    "1 60": 1.60, "1.60": 1.60, "160": 1.60,
    "1 67": 1.67, "1.67": 1.67, "167": 1.67,
    "1 74": 1.74, "1.74": 1.74, "174": 1.74,
}
_BONZER_COATING_MAP = {
    "BB": "BLUEBLOCK", "BLUE BLOCK": "BLUEBLOCK", "BLUE-BLOCK": "BLUEBLOCK",
    "CLEAR": "CLEAR", "CL": "CLEAR",
    "PHOTOCHROMIC": "PHOTOCHROMIC", "PHOTO": "PHOTOCHROMIC",
    "TINTED": "TINTED",
}
_BONZER_TREATMENT_MAP = {
    "MURK ARC":      "MURK ARC",
    "MAGNETIC ARC":  "MAGNETIC ARC",
    "IRIDIO ARC":    "IRIDIO ARC",
    "NIGHTOLUX":     "NIGHTOLUX",
    "PREMIUM HC":    "PREMIUM HC",
    "HC":            "HC",
    "ARC":           "ARC",
    "EASY WIDE":     "EASY WIDE",
    "EASY SURE":     "EASY SURE",
    "WIDE":          "WIDE",
    "SURE":          "SURE",
}
_BONZER_PRODUCT_MAP = {
    "NEXIS AI CORE": "NEXIS AI CORE",
    "NEXIS":         "NEXIS",
    "EASY SURE":     "EASY SURE",
    "EASY WIDE":     "EASY WIDE",
    "ALFA RX FF":    "ALFA RX FREEFORM",
    "ALFA RX":       "ALFA RX",
    "KPT":           "KPT",
    "PROG":          "PROGRESSIVE",
}


def parse_bonzer_description(text: str) -> Dict[str, Any]:
    """
    Decompose a Bonzer lens description into structured attributes.

    Returns:
        {
            "product_family": str,   e.g. "NEXIS AI CORE" / "EASY SURE"
            "lens_type":      str,   e.g. "PROG" / "SV"
            "index":          float, e.g. 1.56
            "coating":        str,   e.g. "BLUEBLOCK" / "CLEAR"
            "treatment":      str,   e.g. "MURK ARC" / "NIGHTOLUX"
            "raw":            str,   original text (power stripped)
        }
    """
    # Strip R/L power section first
    t = re.split(r'\[R\]|\[L\]', str(text or ""), flags=re.I)[0].strip().upper()

    result: Dict[str, Any] = {
        "product_family": "",
        "lens_type":      "",
        "index":          None,
        "coating":        "",
        "treatment":      "",
        "raw":            t,
    }

    # Index — try "1 56" format first (Bonzer uses space in some invoices)
    for pattern, val in sorted(_BONZER_INDEX_MAP.items(), key=lambda x: -len(x[0])):
        if pattern in t:
            result["index"] = val
            t = t.replace(pattern, " ").strip()
            break

    # Lens type
    for lt in ("PROG", "SV", "BIFOCAL", "READING"):
        if re.search(rf"\b{lt}\b", t):
            result["lens_type"] = lt
            t = re.sub(rf"\b{lt}\b", " ", t).strip()
            break

    # Treatment (check longest first to avoid partial matches)
    for key in sorted(_BONZER_TREATMENT_MAP, key=len, reverse=True):
        if key in _BONZER_PRODUCT_MAP:
            continue
        if key in t:
            result["treatment"] = _BONZER_TREATMENT_MAP[key]
            t = t.replace(key, " ").strip()
            break

    # Coating
    for key in sorted(_BONZER_COATING_MAP, key=len, reverse=True):
        if re.search(rf"\b{key}\b", t):
            result["coating"] = _BONZER_COATING_MAP[key]
            t = re.sub(rf"\b{key}\b", " ", t).strip()
            break

    # Product family (what remains after stripping above)
    for key in sorted(_BONZER_PRODUCT_MAP, key=len, reverse=True):
        if key in t:
            result["product_family"] = _BONZER_PRODUCT_MAP[key]
            break

    return result


def match_bonzer_to_product(
    description: str,
    products: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Structured match for Bonzer descriptions using Index + Coating + Treatment.

    Match priority:
    1. Exact: index + coating + treatment + product_family all match
    2. Strong: index + coating + treatment match
    3. Good: index + coating match
    4. Fallback: normalised fuzzy on remaining text

    Returns same shape as ai_match_product().
    """
    parsed = parse_bonzer_description(description)
    idx    = parsed.get("index")
    coat   = str(parsed.get("coating") or "").upper()
    treat  = str(parsed.get("treatment") or "").upper()
    pfam   = str(parsed.get("product_family") or "").upper()
    def _product_specs(p: Dict[str, Any]) -> List[Dict[str, Any]]:
        specs = p.get("oph_specs") or []
        if isinstance(specs, str):
            try:
                specs = json.loads(specs)
            except Exception:
                specs = []
        return specs if isinstance(specs, list) else []

    def _score(p: Dict) -> float:
        score = 0.0
        pname = str(p.get("product_name","") or "").upper()
        specs = _product_specs(p)

        # Bonzer variants live in ophthalmic_lens_specs. Score the selected
        # product through those rows first so index/coating/treatment really
        # participate in the match, instead of only the base product name.
        best_spec = 0.0
        for sp in specs:
            sp_score = 0.0
            spidx = sp.get("index_value")
            spcoat = str(sp.get("coating") or "").upper()
            sptreat = str(sp.get("treatment") or "").upper()
            if idx and spidx:
                if abs(float(spidx) - idx) < 0.01:
                    sp_score += 0.40
                else:
                    sp_score -= 0.30
            if coat:
                if coat in spcoat or spcoat in coat:
                    sp_score += 0.25
                elif coat == "BLUEBLOCK" and ("BB" in spcoat or "BLUE" in spcoat):
                    sp_score += 0.25
                elif coat == "BLUEBLOCK" and "BLUE" in sptreat:
                    sp_score += 0.25
                elif coat == "CLEAR" and "CLEAR" in sptreat:
                    sp_score += 0.25
                elif coat == "PHOTOCHROMIC" and "PHOTO" in sptreat:
                    sp_score += 0.25
            if treat and (
                treat in sptreat
                or sptreat in treat
                or treat in spcoat
                or spcoat in treat
            ):
                sp_score += 0.20
            best_spec = max(best_spec, sp_score)
        score += best_spec

        pcoat = str(p.get("coating","") or p.get("coating_type","") or "").upper()
        pidx  = p.get("index_value")

        # Product-level fallback only when no spec row helped.
        if idx and pidx and best_spec <= 0:
            if abs(float(pidx) - idx) < 0.01:
                score += 0.40
            else:
                score -= 0.30  # wrong index is a hard negative

        if coat and best_spec <= 0:
            if coat in pcoat or pcoat in coat:
                score += 0.25
            elif coat == "BLUEBLOCK" and ("BB" in pcoat or "BLUE" in pcoat):
                score += 0.25

        # Treatment in product name
        if treat:
            treat_words = treat.split()
            for tw in treat_words:
                if len(tw) > 2 and tw in pname:
                    score += 0.10

        # Product family in name
        if pfam:
            pfam_words = [w for w in pfam.split() if len(w) > 3]
            for pw in pfam_words:
                if pw in pname:
                    score += 0.15

        # BB/CLEAR/PHOTO in Bonzer text describes the lens treatment family.
        # Prefer the matching base product when spec rows alone tie.
        if coat == "BLUEBLOCK" and ("BLUE BLOCK" in pname or "BLUEBLOCK" in pname):
            score += 0.20
        elif coat == "CLEAR" and "CLEAR" in pname:
            score += 0.20
        elif coat == "PHOTOCHROMIC" and ("PHOTO" in pname or "PG" in pname):
            score += 0.20

        return score

    best_p, best_s = None, 0.0
    for p in products:
        s = _score(p)
        if s > best_s:
            best_s, best_p = s, p

    if best_p and best_s >= 0.40:
        confidence = min(0.95, 0.55 + best_s)
        return {
            "product_id":   str(best_p["id"]),
            "product_name": str(best_p.get("product_name","")),
            "confidence":   round(confidence, 2),
            "method":       "BONZER_STRUCTURED",
            "reasoning":    f"idx={idx} coat={coat} treat={treat} pfam={pfam}",
        }
    return {"product_id":"","product_name":"","confidence":0.0,
            "method":"NO_MATCH","reasoning":"bonzer structured failed"}
