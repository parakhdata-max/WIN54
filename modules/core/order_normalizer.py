"""
modules/core/order_normalizer.py

Universal Order Normalizer
===========================
Single entry point that guarantees every order dict — regardless of
whether it came from retail_punching, wholesale_punching, backoffice,
or future channels — has the SAME shape before any engine touches it.

PROBLEM IT SOLVES:
    Each channel builds order dicts differently:
        retail    → has gst_percent, unit_price, billing_total
        backoffice loader → may miss gst_percent if SQL join was incomplete
        future channels  → unknown shape

    Downstream engines (tax, pricing, validators) assumed fields exist.
    When they didn't → silent 0, wrong totals, confusing bugs.

WHAT IT DOES:
    1. Stamps order_type (default RETAIL if missing)
    2. Stamps order_source (default "unknown")
    3. Normalizes all lines:
        - Ensures gst_percent is float (not None / string)
        - Ensures unit_price, billing_qty, billing_total are float/int
        - Stamps line_index for tracing
        - Stamps channel_normalized_at timestamp
    4. Returns NormalizationReport — tells caller what was fixed
       so bugs can be caught and surfaced, not silently patched

USAGE:
    from modules.core.order_normalizer import normalize_order

    order, report = normalize_order(order)

    if report.had_issues:
        logger.warning(report.summary())
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

VALID_ORDER_TYPES  = {"RETAIL", "WHOLESALE", "PURCHASE", "ONLINE"}
VALID_GST_SLABS    = {0.0, 5.0, 12.0, 18.0, 28.0}
NUMERIC_LINE_FIELDS = {
    "billing_qty":   int,
    "billing_total": float,
    "unit_price":    float,
    "gst_percent":   float,
    "discount_percent": float,
    "gst_amount":    float,
    "box_size":      int,
}


# ============================================================================
# REPORT
# ============================================================================

@dataclass
class LineNormalization:
    line_index:  int
    product:     str
    fixed:       List[str] = field(default_factory=list)
    warnings:    List[str] = field(default_factory=list)

    @property
    def had_issues(self) -> bool:
        return bool(self.fixed or self.warnings)


@dataclass
class NormalizationReport:
    order_no:         str
    order_type:       str
    source:           str
    line_reports:     List[LineNormalization] = field(default_factory=list)
    order_fixes:      List[str]               = field(default_factory=list)
    normalized_at:    str                     = ""

    @property
    def had_issues(self) -> bool:
        return bool(self.order_fixes) or any(l.had_issues for l in self.line_reports)

    @property
    def missing_gst_lines(self) -> List[str]:
        return [
            l.product for l in self.line_reports
            if any("gst_percent" in f for f in l.fixed + l.warnings)
        ]

    def summary(self) -> str:
        parts = [f"[Normalizer] {self.order_no} ({self.order_type} from {self.source})"]
        if self.order_fixes:
            parts.append(f"  Order fixes: {'; '.join(self.order_fixes)}")
        for lr in self.line_reports:
            if lr.had_issues:
                parts.append(f"  Line {lr.line_index} ({lr.product}):")
                for f in lr.fixed:
                    parts.append(f"    FIXED: {f}")
                for w in lr.warnings:
                    parts.append(f"    WARN:  {w}")
        return "\n".join(parts)


# ============================================================================
# MAIN ENTRY
# ============================================================================

def normalize_order(order: dict) -> tuple[dict, NormalizationReport]:
    """
    Normalize order in-place. Returns (order, NormalizationReport).
    Safe to call multiple times — idempotent.
    """
    ts = datetime.now().isoformat(timespec="seconds")

    # ── Order-level fields ────────────────────────────────────────────────────
    order_no   = order.get("order_no") or order.get("provisional_order_id") or "UNKNOWN"
    order_type = str(order.get("order_type") or "").upper()
    source     = order.get("order_source") or "unknown"

    report = NormalizationReport(
        order_no=order_no,
        order_type=order_type,
        source=source,
        normalized_at=ts,
    )

    # Fix order_type
    if order_type not in VALID_ORDER_TYPES:
        old = order_type or "(empty)"
        order["order_type"] = "RETAIL"
        report.order_fixes.append(f"order_type '{old}' → 'RETAIL' (defaulted)")
        order_type = "RETAIL"

    # Ensure order_info dict carries order_type for engines that read from there
    if "order_info" in order:
        order["order_info"].setdefault("order_type", order_type)

    # ── Line-level normalization ───────────────────────────────────────────────
    lines = order.get("lines", [])

    for idx, line in enumerate(lines, 1):
        product = line.get("product_name") or line.get("product_id") or f"Line {idx}"
        lr = LineNormalization(line_index=idx, product=product)

        # Stamp index for traceability
        line.setdefault("line_index", idx)
        line["channel_normalized_at"] = ts

        # Numeric field coercion
        for field_name, coerce in NUMERIC_LINE_FIELDS.items():
            raw = line.get(field_name)
            if raw is None:
                # Field completely absent — set to 0 and flag
                line[field_name] = coerce(0)
                if field_name == "gst_percent":
                    lr.warnings.append(
                        f"gst_percent absent — set to 0. "
                        f"DATA STARVATION: check fetch_orders_with_lines SQL "
                        f"includes products.gst_percent JOIN."
                    )
                else:
                    lr.fixed.append(f"{field_name} missing → set to 0")
            else:
                try:
                    coerced = coerce(raw)
                    if coerced != raw:
                        line[field_name] = coerced
                        lr.fixed.append(f"{field_name}: {raw!r} → {coerced}")
                except (TypeError, ValueError):
                    line[field_name] = coerce(0)
                    lr.fixed.append(f"{field_name}: invalid {raw!r} → 0")

        # GST slab check (warn only — normalizer doesn't change valid data)
        gst = float(line.get("gst_percent", 0))
        if gst > 0 and gst not in VALID_GST_SLABS:
            lr.warnings.append(
                f"gst_percent={gst} not in standard slabs {sorted(VALID_GST_SLABS)}"
            )

        # billing_total vs unit_price * qty consistency check
        qty   = int(line.get("billing_qty", 0) or 0)
        price = float(line.get("unit_price", 0) or 0)
        total = float(line.get("billing_total", 0) or 0)

        if qty > 0 and price > 0 and total == 0:
            # total missing — reconstruct
            try:
                from modules.core.price_qty_governor import compute_line_gst as _clg_norm
                _ot_n  = str(order.get("order_type") or "RETAIL").upper()
                _gst_n = float(line.get("gst_percent") or 0)
                line["billing_total"] = _clg_norm(price, qty, _gst_n, _ot_n)["grand_total"]
            except Exception:
                line["billing_total"] = round(price * qty, 2)
            lr.fixed.append(
                f"billing_total=0 with qty={qty} price={price} → "
                f"reconstructed to {line['billing_total']}"
            )

        if lr.had_issues:
            report.line_reports.append(lr)

    if report.had_issues:
        logger.warning(report.summary())

    order["_normalized_at"] = ts
    return order, report


# ============================================================================
# CONVENIENCE: normalize all lines from any source dict
# ============================================================================

def normalize_lines(lines: list, order_type: str = "RETAIL") -> tuple[list, list]:
    """
    Normalize a bare list of lines (no parent order dict needed).
    Returns (lines, warnings_list).
    Used by backoffice loader for quick per-line normalization.
    """
    dummy = {"order_type": order_type, "lines": lines}
    _, report = normalize_order(dummy)
    warnings = [
        f"{lr.product}: {'; '.join(lr.fixed + lr.warnings)}"
        for lr in report.line_reports
        if lr.had_issues
    ]
    return lines, warnings
