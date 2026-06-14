"""
Contact Lens Power Intelligence Resolver.

The punching UI may receive only one selected product, but the entered R/L
powers can require different contact lens products.  This module treats the
selected product as a family hint and resolves each eye independently.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class LensType(str, Enum):
    SPHERICAL = "SPHERICAL"
    TORIC = "TORIC"
    MULTIFOCAL = "MULTIFOCAL"
    MULTIFOCAL_TORIC = "MULTIFOCAL_TORIC"
    SPECIAL = "SPECIAL"


class Route(str, Enum):
    STOCK = "STOCK"
    SUPPLIER_ORDER = "SUPPLIER_ORDER"
    SPECIAL_ORDER = "SPECIAL_ORDER"
    UNRESOLVED = "UNRESOLVED"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class CLPower:
    sph: Optional[float] = None
    cyl: Optional[float] = None
    axis: Optional[int] = None
    add: Optional[float] = None

    def is_blank(self) -> bool:
        return self.sph is None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ResolvedEye:
    eye: str
    entered_power: dict
    lens_type: str
    product_id: Optional[str] = None
    product_name: Optional[str] = None
    product_row: Optional[dict] = None
    stock_id: Optional[str] = None
    available_qty: float = 0.0
    route: str = Route.UNRESOLVED.value
    message: str = ""
    confidence: str = Confidence.LOW.value
    alternate_products: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


_FAMILY_ALIAS = {
    "air optix hydraglyde": "AOHG",
    "air optix plus hydraglyde": "AOHG",
    "airoptix hydraglyde": "AOHG",
    "aohg": "AOHG",
    "air optix": "AOHG",
    "air optix aqua": "AOA",
    "dailies total1": "DT1",
    "dailies total 1": "DT1",
    "dailies aquacomfort plus": "DACP",
    "total30": "TOTAL30",
    "total 30": "TOTAL30",
    "acuvue oasys": "ACUVUE_OASYS",
    "acuvue moist": "ACUVUE_MOIST",
    "biofinity": "BIOFINITY",
    "clariti": "CLARITI",
    "myday": "MYDAY",
    "pure vision 2": "PV2",
    "purevision2": "PV2",
    "purevision": "PV2",
}

_REVERSE_FAMILY_TERMS = {
    "AOHG": ["air", "optix", "hydraglyde"],
    "AOA": ["air", "optix", "aqua"],
    "DT1": ["dailies", "total"],
    "DACP": ["dailies", "aquacomfort"],
    "TOTAL30": ["total30", "total"],
    "ACUVUE_OASYS": ["acuvue", "oasys"],
    "ACUVUE_MOIST": ["acuvue", "moist"],
    "BIOFINITY": ["biofinity"],
    "CLARITI": ["clariti"],
    "MYDAY": ["myday"],
    "PV2": ["pure", "vision"],
}

_TYPE_WORDS = {
    "toric", "astigmatism", "multifocal", "multi", "mf", "bifocal",
    "progressive", "presbyopia", "spherical", "sphere", "sph",
}
_COMMON_WORDS = {
    "contact", "lens", "lenses", "pk", "pack", "box", "daily", "monthly",
    "day", "days", "one", "plus", "for", "with", "the",
}


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or str(value).strip() in ("", "None", "null"):
            return None
        return float(value)
    except Exception:
        return None


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None or str(value).strip() in ("", "None", "null"):
            return None
        return int(float(value))
    except Exception:
        return None


def parse_cl_power(raw: str | dict | None) -> CLPower:
    if raw is None:
        return CLPower()

    if isinstance(raw, dict):
        sph = _safe_float(raw.get("sph"))
        cyl = _safe_float(raw.get("cyl"))
        axis = _safe_int(raw.get("axis"))
        add = _safe_float(raw.get("add") if "add" in raw else raw.get("add_power"))
        if cyl == 0:
            cyl = None
        if add == 0:
            add = None
        if axis == 0:
            axis = None
        return CLPower(sph=sph, cyl=cyl, axis=axis, add=add)

    text = str(raw).strip().lower()
    text = re.sub(r"\b(plano|pl|ds)\b", "0", text)
    nums = []
    for token in re.findall(r"[+-]?\d+(?:\.\d+)?", text):
        val = float(token)
        if abs(val) > 20 and val == int(val):
            val = val / 100.0
        nums.append(val)
    sph = nums[0] if len(nums) > 0 else None
    cyl = nums[1] if len(nums) > 1 else None
    axis = int(nums[2]) if len(nums) > 2 else None
    add = nums[3] if len(nums) > 3 else None
    return parse_cl_power({"sph": sph, "cyl": cyl, "axis": axis, "add": add})


def classify_cl_power(
    sph: Optional[float],
    cyl: Optional[float],
    axis: Optional[int],
    add_power: Optional[float],
) -> tuple[LensType, str, Confidence]:
    has_cyl = cyl not in (None, 0, 0.0)
    has_axis = axis not in (None, 0)
    has_add = add_power not in (None, 0, 0.0)

    if has_cyl and not has_axis:
        return (
            LensType.SPECIAL,
            f"CYL {cyl:+.2f} entered but AXIS is missing.",
            Confidence.LOW,
        )
    if not has_cyl and not has_add:
        return LensType.SPHERICAL, "No CYL/ADD, so this eye needs spherical CL.", Confidence.HIGH
    if has_cyl and has_axis and not has_add:
        return LensType.TORIC, f"CYL {cyl:+.2f} with AXIS {axis}, so this eye needs toric CL.", Confidence.HIGH
    if not has_cyl and has_add:
        return LensType.MULTIFOCAL, f"ADD {add_power:+.2f}, so this eye needs multifocal CL.", Confidence.HIGH
    if has_cyl and has_axis and has_add:
        return LensType.MULTIFOCAL_TORIC, "CYL/AXIS plus ADD, so this is multifocal toric/special.", Confidence.MEDIUM
    return LensType.SPECIAL, "Unusual CL power combination.", Confidence.LOW


def normalize_cl_family(
    product_name: Optional[str],
    brand: Optional[str] = None,
    company_product_name: Optional[str] = None,
) -> str:
    candidates = [product_name, company_product_name, brand]
    for raw in candidates:
        text = str(raw or "").strip().lower()
        if not text:
            continue
        if text in _FAMILY_ALIAS:
            return _FAMILY_ALIAS[text]
        for alias, family in _FAMILY_ALIAS.items():
            if alias in text:
                return family
    best = next((str(c).strip() for c in candidates if str(c or "").strip()), "UNKNOWN")
    return re.sub(r"[^A-Z0-9]+", "_", best.upper()).strip("_") or "UNKNOWN"


def _family_terms(selected_product: str, brand: str = "", company_product_name: str = "") -> list[str]:
    family = normalize_cl_family(selected_product, brand, company_product_name)
    terms = list(_REVERSE_FAMILY_TERMS.get(family, []))
    source = f"{selected_product or ''} {company_product_name or ''} {brand or ''}".lower()
    source = re.sub(r"\b\d+\s*(pk|pack|box|lens|lenses)\b", " ", source)
    words = [
        w for w in re.findall(r"[a-z0-9]+", source)
        if len(w) > 1 and w not in _TYPE_WORDS and w not in _COMMON_WORDS
    ]
    for w in words:
        if w not in terms:
            terms.append(w)
    return terms or [family.lower()]


def _product_lens_type(row: dict) -> LensType:
    text = " ".join(str(row.get(k) or "") for k in (
        "product_name", "company_product_name", "category", "lens_category", "brand"
    )).lower()
    has_toric = "toric" in text or "astig" in text
    has_multi = any(w in text for w in ("multifocal", "multi focal", "presbyopia", "bifocal", "progressive", " mf "))
    if has_toric and has_multi:
        return LensType.MULTIFOCAL_TORIC
    if has_toric:
        return LensType.TORIC
    if has_multi:
        return LensType.MULTIFOCAL
    return LensType.SPHERICAL


def _pack_size(row: dict) -> int:
    text = " ".join(str(row.get(k) or "") for k in ("product_name", "company_product_name", "box_size")).lower()
    match = re.search(r"\b(1|3|6|12|30|90)\s*(pk|pack|box)\b", text)
    if match:
        return int(match.group(1))
    return int(_safe_int(row.get("box_size")) or 0)


def _score_product(row: dict, terms: list[str], lens_type: LensType, pack_hint: Optional[int]) -> int:
    text = " ".join(str(row.get(k) or "") for k in (
        "product_name", "company_product_name", "category", "lens_category", "brand"
    )).lower()
    score = 0
    for term in terms:
        if term and term in text:
            score += 12
    product_type = _product_lens_type(row)
    if product_type == lens_type:
        score += 80
    elif lens_type == LensType.MULTIFOCAL_TORIC and product_type in (LensType.TORIC, LensType.MULTIFOCAL):
        score += 25
    else:
        score -= 60
    if pack_hint and _pack_size(row) == int(pack_hint):
        score += 8
    return score


def _query_contact_products() -> list[dict]:
    from modules.sql_adapter import run_query

    return run_query("""
        SELECT
            p.id::text AS product_id,
            p.product_name,
            COALESCE(p.brand, '') AS brand,
            COALESCE(p.company_product_name, '') AS company_product_name,
            COALESCE(p.category, '') AS category,
            COALESCE(p.lens_category, '') AS lens_category,
            COALESCE(p.main_group, '') AS main_group,
            COALESCE(p.unit, 'BOX') AS unit,
            COALESCE(p.box_size, 1) AS box_size,
            COALESCE(p.gst_percent, 0) AS gst_percent,
            COALESCE(MAX(NULLIF(s.mrp, 0)), 0) AS mrp,
            COALESCE(MAX(NULLIF(s.selling_price, 0)), 0) AS selling_price,
            COALESCE(MAX(NULLIF(s.purchase_rate, 0)), 0) AS purchase_rate
        FROM products p
        LEFT JOIN inventory_stock s
               ON s.product_id = p.id
              AND COALESCE(s.is_active, TRUE) = TRUE
        WHERE COALESCE(p.is_active, TRUE) = TRUE
          AND (
              LOWER(COALESCE(p.main_group, '')) LIKE '%%contact%%'
              OR LOWER(COALESCE(p.category, '')) LIKE '%%contact%%'
              OR LOWER(COALESCE(p.lens_category, '')) LIKE '%%contact%%'
          )
        GROUP BY p.id, p.product_name, p.brand, p.company_product_name,
                 p.category, p.lens_category, p.main_group, p.unit,
                 p.box_size, p.gst_percent
    """) or []


def _stock_for_power(product_id: str, power: CLPower) -> dict:
    from modules.sql_adapter import run_query

    clauses = ["s.product_id = %(pid)s::uuid", "COALESCE(s.is_active, TRUE) = TRUE"]
    params = {"pid": product_id}
    if power.sph is not None:
        clauses.append("ROUND(COALESCE(s.sph,0)::numeric,2) = ROUND(%(sph)s::numeric,2)")
        params["sph"] = float(power.sph)
    if power.cyl not in (None, 0, 0.0):
        clauses.append("ROUND(COALESCE(s.cyl,0)::numeric,2) = ROUND(%(cyl)s::numeric,2)")
        params["cyl"] = float(power.cyl)
    else:
        clauses.append("ROUND(COALESCE(s.cyl,0)::numeric,2) = 0")
    if power.axis not in (None, 0):
        clauses.append("COALESCE(s.axis,0) = %(axis)s")
        params["axis"] = int(power.axis)
    if power.add not in (None, 0, 0.0):
        clauses.append("ROUND(COALESCE(s.add_power,0)::numeric,2) = ROUND(%(add)s::numeric,2)")
        params["add"] = float(power.add)

    rows = run_query(f"""
        SELECT
            s.id::text AS stock_id,
            GREATEST(
                COALESCE(s.quantity,0)::numeric
                - COALESCE(s.allocated_qty,0)::numeric
                - COALESCE(s.reserved_qty,0)::numeric,
                0
            ) AS available_qty,
            COALESCE(s.batch_no, '') AS batch_no,
            s.expiry_date::text AS expiry_date
        FROM inventory_stock s
        WHERE {' AND '.join(clauses)}
        ORDER BY available_qty DESC, s.expiry_date NULLS LAST
        LIMIT 1
    """, params) or []
    return rows[0] if rows else {}


def find_matching_cl_product(
    selected_product: str,
    lens_type: LensType,
    pack_hint: Optional[int] = None,
    eye_power: Optional[CLPower] = None,
    brand: Optional[str] = None,
    company_product_name: Optional[str] = None,
) -> list[dict]:
    terms = _family_terms(selected_product, brand or "", company_product_name or "")
    products = _query_contact_products()
    scored = []
    for row in products:
        score = _score_product(row, terms, lens_type, pack_hint)
        if score > 0:
            item = dict(row)
            item["_resolver_score"] = score
            item["_stock_id"] = None
            item["_stock_qty"] = 0.0
            scored.append(item)
    scored.sort(key=lambda r: r.get("_resolver_score", 0), reverse=True)
    scored = scored[:12]
    if eye_power:
        for item in scored:
            stock = _stock_for_power(str(item["product_id"]), eye_power)
            item["_stock_id"] = stock.get("stock_id")
            item["_stock_qty"] = float(stock.get("available_qty") or 0)
        scored.sort(
            key=lambda r: (float(r.get("_stock_qty") or 0) > 0, r.get("_resolver_score", 0)),
            reverse=True,
        )
    return scored


def _resolve_one_eye(
    eye: str,
    power_raw: Any,
    selected_product: str,
    pack_hint: Optional[int],
    brand: Optional[str],
    company_product_name: Optional[str],
) -> ResolvedEye:
    power = parse_cl_power(power_raw)
    if power.is_blank():
        return ResolvedEye(
            eye=eye,
            entered_power={},
            lens_type=LensType.SPECIAL.value,
            message=f"{eye}: no power entered.",
        )

    lens_type, reason, confidence = classify_cl_power(power.sph, power.cyl, power.axis, power.add)
    result = ResolvedEye(
        eye=eye,
        entered_power=power.to_dict(),
        lens_type=lens_type.value,
        confidence=confidence.value,
    )
    if lens_type == LensType.SPECIAL:
        result.route = Route.UNRESOLVED.value
        result.message = f"{eye}: {reason}"
        return result

    candidates = find_matching_cl_product(
        selected_product=selected_product,
        lens_type=lens_type,
        pack_hint=pack_hint,
        eye_power=power,
        brand=brand,
        company_product_name=company_product_name,
    )
    if not candidates:
        result.route = Route.SPECIAL_ORDER.value
        result.message = f"{eye}: {reason} No matching contact lens product found."
        return result

    best = candidates[0]
    stock_qty = float(best.get("_stock_qty") or 0)
    product_row = {
        **best,
        "product_id": best.get("product_id"),
        "id": best.get("product_id"),
    }
    result.product_id = str(best.get("product_id") or "")
    result.product_name = str(best.get("product_name") or "")
    result.product_row = product_row
    result.stock_id = best.get("_stock_id")
    result.available_qty = stock_qty
    result.route = Route.STOCK.value if stock_qty > 0 else Route.SUPPLIER_ORDER.value
    result.confidence = Confidence.HIGH.value if stock_qty > 0 else Confidence.MEDIUM.value
    result.message = (
        f"{eye}: {reason} Mapped to {result.product_name}. "
        + (f"Stock available {stock_qty:g}." if stock_qty > 0 else "No exact stock, route to supplier.")
    )
    result.alternate_products = [
        {
            "product_id": c.get("product_id"),
            "product_name": c.get("product_name"),
            "available_qty": float(c.get("_stock_qty") or 0),
        }
        for c in candidates[1:4]
    ]
    return result


def resolve_contact_lens_order(
    selected_product: str,
    right_power: Any,
    left_power: Any,
    qty_mode: str = "per_eye",
    pack_hint: Optional[int] = None,
    brand: Optional[str] = None,
    company_product_name: Optional[str] = None,
    db_conn=None,
) -> dict:
    """Resolve R/L CL product lines. db_conn is accepted for Claude API compatibility."""
    family_key = normalize_cl_family(selected_product, brand, company_product_name)
    lines = []
    if right_power is not None:
        lines.append(_resolve_one_eye("R", right_power, selected_product, pack_hint, brand, company_product_name))
    if left_power is not None:
        lines.append(_resolve_one_eye("L", left_power, selected_product, pack_hint, brand, company_product_name))

    requires_confirmation = any(
        line.confidence != Confidence.HIGH.value or line.route != Route.STOCK.value
        for line in lines
    )
    return {
        "family_key": family_key,
        "lines": [line.to_dict() for line in lines],
        "requires_confirmation": requires_confirmation,
        "summary": "\n".join(
            f"{line.eye}: {line.lens_type} -> {line.product_name or 'NOT FOUND'} | {line.route}"
            for line in lines
        ),
    }


def resolve_for_selected_product(
    selected_product_row: dict,
    right_power: Any,
    left_power: Any,
    pack_hint: Optional[int] = None,
) -> dict:
    row = selected_product_row or {}
    return resolve_contact_lens_order(
        selected_product=str(row.get("product_name") or ""),
        right_power=right_power,
        left_power=left_power,
        pack_hint=pack_hint or _pack_size(row) or None,
        brand=row.get("brand"),
        company_product_name=row.get("company_product_name"),
    )


def line_for_eye(result: dict, eye: str) -> dict:
    for line in (result or {}).get("lines", []):
        if str(line.get("eye") or "").upper() == str(eye or "").upper():
            return line
    return {}


def should_show_resolution_notice(result: dict, selected_product_id: str | None = None) -> bool:
    selected = str(selected_product_id or "")
    for line in (result or {}).get("lines", []):
        pid = str(line.get("product_id") or "")
        if pid and selected and pid != selected:
            return True
        if line.get("route") in (Route.SUPPLIER_ORDER.value, Route.SPECIAL_ORDER.value, Route.UNRESOLVED.value):
            return True
    return False


def dry_run_resolver() -> None:
    cases = [
        ("Air Optix Hydraglyde", {"sph": -2, "cyl": 0}, {"sph": -2, "cyl": -1.25, "axis": 180}),
        ("Pure Vision 2", {"sph": 0.75, "add": 2.5}, {"sph": 1.0, "add": 2.5}),
    ]
    for selected, r_power, l_power in cases:
        print(resolve_contact_lens_order(selected, r_power, l_power)["summary"])


if __name__ == "__main__":
    dry_run_resolver()
