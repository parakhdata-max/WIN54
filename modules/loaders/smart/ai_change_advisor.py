"""
modules/loaders/smart/ai_change_advisor.py
============================================
AI Change Advisor — Rule-based intelligence. No external API needed.

Analyses a ChangeReport and produces:
  - Plain-English summary of what will change
  - Risk-level explanation per field type
  - Impact warnings (e.g. margin impact when purchase_rate changes)
  - Recommended action: proceed / review / stop
  - Interactive Yes/No/Ask flow text

Three user modes (auto-detected from risk level):
  QUICK    — Low risk only → brief one-liner + Yes/No
  CAREFUL  — Medium/High risk → full explanation + confirm
  GUIDED   — User asked a question → answer it first

Usage:
    from modules.loaders.smart.ai_change_advisor import advise

    advice = advise(change_report)
    print(advice.summary)
    print(advice.recommendation)
    # In UI: show advice.prompt_text and Yes/No buttons
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional
from modules.loaders.smart.change_detector import (
    ChangeReport, FieldChange,
    RISK_SAFE, RISK_CAUTION, RISK_WARNING, RISK_BLOCKED,
)


@dataclass
class Advice:
    recommendation:  str              # "PROCEED" | "REVIEW" | "STOP"
    summary:         str              # one-line summary
    explanation:     str              # full plain-English explanation
    warnings:        List[str]        # specific warnings per risk
    prompt_text:     str              # text to show above Yes/No buttons
    requires_typed:  bool = False     # True = require typing "CONFIRM" for high-risk
    field_advice:    Dict[str, str] = field(default_factory=dict)   # per-field guidance
    backup_required: bool = False


# ── Field-specific human-readable names ──────────────────────────────────────
FIELD_LABELS = {
    "selling_price":   "Selling Price",
    "purchase_rate":   "Purchase Rate",
    "mrp":             "MRP",
    "quantity":        "Quantity",
    "qty":             "Quantity",
    "is_active":       "Active Status",
    "expiry_date":     "Expiry Date",
    "box_size":        "Box Size",
    "lens_design":     "Lens Design",
    "material":        "Material",
    "index_value":     "Lens Index",
    "coating":         "Coating",
    "coating_type":    "Coating Type",
    "wear_schedule":   "Wear Schedule",
    "gst_rate":        "GST Rate",
    "hsn_code":        "HSN Code",
    "credit_limit":    "Credit Limit",
    "credit_days":     "Credit Days",
    "cost_price":      "Cost Price",
    "brand":           "Brand",
    "colour":          "Colour",
    "address":         "Address",
    "city":            "City",
    "gstin":           "GSTIN",
    "is_batch_applicable": "Batch Tracking",
    "is_eye_specific": "Eye-Specific Setting",
    "allow_loose":     "Allow Loose Sale",
}

# ── Impact rules per field ────────────────────────────────────────────────────
FIELD_IMPACTS = {
    "selling_price": lambda old, new: _price_impact("Selling Price", old, new),
    "purchase_rate": lambda old, new: _purchase_impact(old, new),
    "mrp":           lambda old, new: _price_impact("MRP", old, new),
    "box_size":      lambda old, new: _box_size_impact(old, new),
    "gst_rate":      lambda old, new: _gst_impact(old, new),
    "is_active":     lambda old, new: _active_impact(old, new),
    "is_batch_applicable": lambda old, new: _batch_impact(old, new),
    "credit_limit":  lambda old, new: _credit_impact("Credit Limit", old, new),
    "credit_days":   lambda old, new: _credit_impact("Credit Days", old, new),
}


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def advise(report: ChangeReport) -> Advice:
    """
    Analyse a ChangeReport and return an Advice object.
    This is the only public function — call this from the UI.
    """
    if not report.has_changes and not report.has_blocked:
        return Advice(
            recommendation = "PROCEED",
            summary        = "No changes detected.",
            explanation    = "The uploaded file is identical to the current database values. Nothing will be updated.",
            warnings       = [],
            prompt_text    = "No changes found. Nothing to apply.",
            requires_typed = False,
            backup_required = False,
        )

    highest_risk  = report.highest_risk
    risk_counts   = report.risk_counts
    field_summary = report.changed_fields_summary
    total_changes = len(report.changes)
    total_rows    = report.total_rows

    warnings      = []
    field_advice  = {}
    backup_req    = False
    requires_typed = False

    # ── Blocked column changes ────────────────────────────────────────────────
    if report.has_blocked:
        blocked_fields = list({b.field_name for b in report.blocked})
        warnings.append(
            f"⛔ {len(report.blocked)} change(s) to locked fields will be IGNORED: "
            f"{', '.join(blocked_fields)}. "
            "These identity fields cannot be changed. Only editable fields will be updated."
        )

    # ── Per-field analysis ────────────────────────────────────────────────────
    for fname, count in field_summary.items():
        affected_changes = [c for c in report.changes if c.field_name == fname]
        if not affected_changes:
            continue

        sample = affected_changes[0]
        label  = FIELD_LABELS.get(fname, fname.replace("_", " ").title())
        impact_fn = FIELD_IMPACTS.get(fname)

        if impact_fn:
            impact_msg = impact_fn(sample.old_value, sample.new_value)
            if impact_msg:
                warnings.append(f"📊 {label}: {impact_msg}")

        risk = FIELD_RISK_LEVEL(fname)

        if risk == RISK_WARNING:
            backup_req = True
            requires_typed = True
            field_advice[fname] = (
                f"⚠️ '{label}' is a master field. Changing it affects ALL records using "
                f"this product. The change applies from NOW onwards only — historical "
                f"records will NOT be retroactively changed. A backup will be taken before applying."
            )
        elif risk == RISK_CAUTION:
            field_advice[fname] = (
                f"🟡 '{label}' affects financial calculations. "
                f"Verify the new value is correct before proceeding."
            )

    # ── Rows not found ────────────────────────────────────────────────────────
    if report.rows_not_found:
        warnings.append(
            f"ℹ️ {len(report.rows_not_found)} record(s) from the file were not found in the database "
            f"and will be skipped: {', '.join(report.rows_not_found[:5])}"
            + (" ..." if len(report.rows_not_found) > 5 else "")
        )

    # ── Build summary text ────────────────────────────────────────────────────
    summary = _build_summary(report, field_summary, risk_counts, total_changes, total_rows)

    # ── Build explanation ─────────────────────────────────────────────────────
    explanation = _build_explanation(report, field_summary, risk_counts, warnings)

    # ── Recommendation ────────────────────────────────────────────────────────
    if highest_risk == RISK_WARNING:
        recommendation = "REVIEW"
    elif highest_risk == RISK_CAUTION and risk_counts[RISK_CAUTION] > 20:
        recommendation = "REVIEW"
    else:
        recommendation = "PROCEED"

    # ── Prompt text (shown above Yes/No buttons) ──────────────────────────────
    prompt_text = _build_prompt(recommendation, highest_risk, total_changes, requires_typed)

    return Advice(
        recommendation  = recommendation,
        summary         = summary,
        explanation     = explanation,
        warnings        = warnings,
        prompt_text     = prompt_text,
        requires_typed  = requires_typed,
        field_advice    = field_advice,
        backup_required = backup_req,
    )


def answer_question(question: str, report: ChangeReport) -> str:
    """
    Rule-based Q&A. User typed a question — return a plain-English answer.
    Called when user clicks 'Ask AI' instead of Yes/No.
    """
    q = question.lower().strip()
    field_summary = report.changed_fields_summary

    # What will change?
    if any(w in q for w in ["what", "which", "show", "list"]):
        if not field_summary:
            return "No changes detected — the uploaded file matches the current database."
        lines = ["Here is exactly what will change:\n"]
        for fname, count in field_summary.items():
            label = FIELD_LABELS.get(fname, fname)
            affected = [c for c in report.changes if c.field_name == fname]
            sample = affected[0] if affected else None
            if sample:
                lines.append(f"• {label}: {count} record(s) — e.g. {sample.old_value} → {sample.new_value}")
            else:
                lines.append(f"• {label}: {count} record(s)")
        return "\n".join(lines)

    # Is it safe?
    if any(w in q for w in ["safe", "risk", "danger", "ok", "fine"]):
        rc = report.risk_counts
        if rc[RISK_WARNING] > 0:
            return (
                f"There are {rc[RISK_WARNING]} high-risk changes to master fields. "
                "These require careful review. A backup will be taken before applying. "
                "Proceed only if you are sure."
            )
        elif rc[RISK_CAUTION] > 0:
            return (
                f"There are {rc[RISK_CAUTION]} financial field changes (purchase rate, cost price etc). "
                "These are generally safe but verify the new values are correct."
            )
        else:
            return "All changes are low-risk (prices, status, contact details). Safe to proceed."

    # Will old records change?
    if any(w in q for w in ["old", "historical", "previous", "existing", "past", "earlier"]):
        has_master = any(FIELD_RISK_LEVEL(f) == RISK_WARNING for f in field_summary)
        if has_master:
            return (
                "Master field changes (like box size, lens design) apply FROM NOW onwards only. "
                "Existing historical transactions, orders, and records will NOT be changed retroactively. "
                "The old value is preserved in the audit log."
            )
        return (
            "No master field changes detected. "
            "Price and quantity changes apply to the current stock record only — "
            "historical transactions are never modified."
        )

    # What is box size / what does this field mean?
    for fname, label in FIELD_LABELS.items():
        if fname in q or label.lower() in q:
            return _explain_field(fname, report)

    # Undo / rollback?
    if any(w in q for w in ["undo", "rollback", "reverse", "revert", "backup"]):
        return (
            "Yes — before any high-risk changes are applied, the system takes a full backup of "
            "the affected records. You can rollback from the Import Rollback tab at any time."
        )

    # How many?
    if any(w in q for w in ["how many", "count", "number"]):
        rc = report.risk_counts
        return (
            f"Total: {len(report.changes)} field changes across {report.total_rows} rows.\n"
            f"  🟢 Safe:    {rc[RISK_SAFE]}\n"
            f"  🟡 Caution: {rc[RISK_CAUTION]}\n"
            f"  🔴 Warning: {rc[RISK_WARNING]}\n"
        )

    # Default
    return (
        f"I found {len(report.changes)} changes across {len(report.changed_fields_summary)} field(s). "
        f"Highest risk level: {report.highest_risk}. "
        "Ask me 'what will change', 'is it safe', 'will old records change', or 'how many' for more details."
    )


# ══════════════════════════════════════════════════════════════════════════════
# BUILDERS
# ══════════════════════════════════════════════════════════════════════════════

def _build_summary(report, field_summary, risk_counts, total_changes, total_rows) -> str:
    if total_changes == 0:
        return "No changes detected."

    field_names = [FIELD_LABELS.get(f, f) for f in list(field_summary.keys())[:3]]
    fields_str  = ", ".join(field_names)
    if len(field_summary) > 3:
        fields_str += f" +{len(field_summary) - 3} more"

    risk_icon = {"SAFE": "🟢", "CAUTION": "🟡", "WARNING": "🔴"}.get(report.highest_risk, "")
    return (
        f"{risk_icon} {total_changes} change(s) across {len(field_summary)} field(s) "
        f"in {total_rows} rows — {fields_str}"
    )


def _build_explanation(report, field_summary, risk_counts, warnings) -> str:
    lines = []

    if risk_counts[RISK_WARNING] > 0:
        lines.append(
            f"🔴 HIGH RISK: {risk_counts[RISK_WARNING]} change(s) affect master/configuration fields. "
            "These fields define how the product works in the system. "
            "A backup will be taken automatically before applying."
        )

    if risk_counts[RISK_CAUTION] > 0:
        lines.append(
            f"🟡 CAUTION: {risk_counts[RISK_CAUTION]} change(s) affect financial fields (rates, costs). "
            "Verify the new values are correct."
        )

    if risk_counts[RISK_SAFE] > 0:
        lines.append(
            f"🟢 {risk_counts[RISK_SAFE]} low-risk change(s) (prices, status, contact info)."
        )

    lines.append("")
    lines.append("What will change:")
    for fname, count in field_summary.items():
        label    = FIELD_LABELS.get(fname, fname)
        affected = [c for c in report.changes if c.field_name == fname]
        if affected:
            sample = affected[0]
            lines.append(f"  • {label}: {count} record(s) — e.g. {sample.old_value} → {sample.new_value}")

    if report.rows_not_found:
        lines.append(f"\nℹ️ {len(report.rows_not_found)} row(s) not found in DB — will be skipped.")

    return "\n".join(lines)


def _build_prompt(recommendation, highest_risk, total_changes, requires_typed) -> str:
    if recommendation == "PROCEED":
        if highest_risk == RISK_SAFE:
            return f"✅ {total_changes} safe change(s) ready to apply. Proceed?"
        else:
            return f"🟡 {total_changes} change(s) detected including financial fields. Review above and proceed?"
    else:
        if requires_typed:
            return (
                f"🔴 HIGH RISK changes detected. Read the warnings carefully.\n"
                f"A backup will be taken before applying.\n"
                f"Type CONFIRM to proceed, or click No to cancel."
            )
        return (
            f"⚠️ {total_changes} change(s) require your review. "
            "Proceed only if you have verified the changes above."
        )


# ══════════════════════════════════════════════════════════════════════════════
# IMPACT CALCULATORS
# ══════════════════════════════════════════════════════════════════════════════

def _price_impact(label, old, new) -> str:
    try:
        o, n = float(old or 0), float(new or 0)
        if o == 0:
            return f"New {label} set to ₹{n:,.2f}"
        diff = n - o
        pct  = (diff / o) * 100
        arrow = "📈" if diff > 0 else "📉"
        return f"{arrow} {label} changing by ₹{abs(diff):,.2f} ({abs(pct):.1f}% {'increase' if diff > 0 else 'decrease'})"
    except Exception:
        return ""


def _purchase_impact(old, new) -> str:
    try:
        o, n = float(old or 0), float(new or 0)
        diff = n - o
        arrow = "📈" if diff > 0 else "📉"
        return (
            f"{arrow} Purchase Rate changing by ₹{abs(diff):,.2f}. "
            "This affects margin calculations on future sales."
        )
    except Exception:
        return ""


def _box_size_impact(old, new) -> str:
    try:
        o, n = int(float(old or 0)), int(float(new or 0))
        return (
            f"Box size changing from {o} to {n} units. "
            f"This affects quantity calculations for all future stock entries. "
            f"Existing stock records will NOT be recalculated retroactively."
        )
    except Exception:
        return ""


def _gst_impact(old, new) -> str:
    return (
        f"GST rate changing from {old}% to {new}%. "
        "This will affect all future invoices for this product/party."
    )


def _active_impact(old, new) -> str:
    if str(new).upper() in ("NO", "FALSE", "0"):
        return "Record will be DEACTIVATED — it will no longer appear in search/sales."
    return "Record will be REACTIVATED."


def _batch_impact(old, new) -> str:
    if str(new).upper() in ("NO", "FALSE", "0"):
        return (
            "Batch tracking will be DISABLED for this product. "
            "This is a significant change — existing batches will remain but new stock won't be batch-tracked."
        )
    return "Batch tracking will be ENABLED for this product."


def _credit_impact(label, old, new) -> str:
    try:
        o, n = float(old or 0), float(new or 0)
        diff = n - o
        return f"{label} changing by {'+' if diff > 0 else ''}{diff:,.0f}."
    except Exception:
        return ""


def _explain_field(fname: str, report: ChangeReport) -> str:
    explanations = {
        "box_size": (
            "Box size defines how many units are in one box of this product. "
            "For example, box_size=6 means 1 box = 6 lenses. "
            "Changing this affects how quantity is displayed and calculated for future entries."
        ),
        "lens_design": (
            "Lens design (SPHERICAL, TORIC, MULTIFOCAL) defines the optical category. "
            "This is used for filtering, reporting, and prescription matching."
        ),
        "is_batch_applicable": (
            "Controls whether this product tracks stock by batch number and expiry date. "
            "YES = full batch tracking. NO = simple qty tracking only."
        ),
        "purchase_rate": (
            "The cost price paid to the supplier. Used to calculate gross margin. "
            "Changes here do NOT affect past invoices — only future margin calculations."
        ),
        "selling_price": (
            "The default selling price shown at the counter. "
            "Can be overridden at invoice time. Changing this affects the default price only."
        ),
        "mrp": (
            "Maximum Retail Price — the printed price on the box. "
            "Cannot sell above MRP legally. Changing this updates the displayed MRP."
        ),
        "gst_rate": (
            "GST percentage applied to this product on invoices. "
            "Changes apply to future invoices only — past invoices are not affected."
        ),
        "credit_limit": (
            "Maximum outstanding amount allowed for this party. "
            "System will warn at billing if credit limit is exceeded."
        ),
    }
    label = FIELD_LABELS.get(fname, fname)
    changes_for_field = [c for c in report.changes if c.field_name == fname]
    base = explanations.get(fname, f"{label} is a standard field.")
    if changes_for_field:
        s = changes_for_field[0]
        base += f"\n\nIn your file: changing from '{s.old_value}' to '{s.new_value}' across {len(changes_for_field)} record(s)."
    return base


def FIELD_RISK_LEVEL(fname: str) -> str:
    from modules.loaders.smart.change_detector import FIELD_RISK, RISK_SAFE
    return FIELD_RISK.get(fname, RISK_SAFE)
