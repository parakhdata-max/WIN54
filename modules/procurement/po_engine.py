"""
procurement/po_engine.py
=========================
Unified PO Engine — Priority 4.

WHY THIS EXISTS
---------------
  Before: Daily POs (supplier_orders_management.py) and Advisory POs
          (advisory_service.py) were two separate code paths with
          different DB shapes, different WhatsApp templates, and
          different PO number sequences.

  After:  ONE engine. One PO lifecycle. One WhatsApp template.
          Both daily and advisory POs call create_po() here.

ARCHITECTURE
------------
  advisory_service.py ─┐
                        ├─► po_engine.create_po(...)
  daily_fulfillment  ──┘         ↓
                           supplier_orders table
                                 ↓
                           po_engine.send_po(...)   (WhatsApp / Email)

PO SOURCES
----------
  "DAILY"    — from the daily backoffice fulfillment flow
  "ADVISORY" — from advisory procurement (smart alerts / quick refill)
  "AUTO"     — future: automation-triggered reorders

PUBLIC API
----------
  create_po(source, supplier_id, items, notes="", expected_days=None)
      → POResult

  send_po(po_id, channel="whatsapp")
      → SendResult

  get_po(po_id) → dict | None

  update_po_status(po_id, status, notes="") → bool

  format_po_message(po) → str  (WhatsApp / print preview)
"""

import datetime
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# ── PO source constants ───────────────────────────────────────────────
SOURCE_DAILY    = "DAILY"
SOURCE_ADVISORY = "ADVISORY"
SOURCE_AUTO     = "AUTO"

# ── Status lifecycle ──────────────────────────────────────────────────
STATUS_DRAFT     = "Draft"
STATUS_SENT      = "Sent"
STATUS_CONFIRMED = "Confirmed"
STATUS_RECEIVED  = "Received"
STATUS_CANCELLED = "Cancelled"
STATUS_REJECTED  = "Rejected"

VALID_STATUSES = {
    STATUS_DRAFT, STATUS_SENT, STATUS_CONFIRMED,
    STATUS_RECEIVED, STATUS_CANCELLED, STATUS_REJECTED
}


# ═══════════════════════════════════════════════════════════════════════
# RESULT OBJECTS
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class POItem:
    product_id:   str
    product_name: str
    qty:          int
    unit_price:   float = 0.0
    notes:        str   = ""


@dataclass
class POResult:
    success:   bool
    po_id:     Optional[str]  = None
    po_number: Optional[str]  = None
    message:   str            = ""
    error:     Optional[str]  = None


@dataclass
class SendResult:
    success:  bool
    channel:  str
    message:  str
    error:    Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════
# MAIN PO CREATION
# ═══════════════════════════════════════════════════════════════════════

def create_po(
    source:        str,
    supplier_id:   str,
    supplier_name: str,
    items:         List[POItem],
    notes:         str = "",
    expected_days: Optional[int] = None,
    created_by:    Optional[str] = None,
) -> POResult:
    """
    Unified PO creation — used by both daily fulfillment and advisory.

    Args:
        source:        SOURCE_DAILY | SOURCE_ADVISORY | SOURCE_AUTO
        supplier_id:   DB id of supplier party
        supplier_name: Display name (for logging / messages)
        items:         List of POItem objects
        notes:         Free-text PO notes
        expected_days: Days until expected delivery (default varies by source)
        created_by:    User id for audit trail

    Returns:
        POResult with po_id + po_number on success
    """
    if not items:
        return POResult(success=False, error="Cannot create PO with no items")

    if source not in (SOURCE_DAILY, SOURCE_ADVISORY, SOURCE_AUTO):
        return POResult(success=False, error=f"Unknown PO source: {source}")

    expected_delivery = None
    if expected_days:
        expected_delivery = (
            datetime.date.today() + datetime.timedelta(days=expected_days)
        ).isoformat()

    try:
        from modules.sql_adapter import run_query

        # Insert PO header
        po_rows = run_query("""
            INSERT INTO supplier_orders
              (supplier_id, status, source, notes, expected_delivery, created_at, created_by)
            VALUES
              (%(sid)s, %(status)s, %(source)s, %(notes)s,
               %(expected_delivery)s, NOW(), %(created_by)s)
            RETURNING id::text AS po_id
        """, {
            "sid":               supplier_id,
            "status":            STATUS_DRAFT,
            "source":            source,
            "notes":             notes,
            "expected_delivery": expected_delivery,
            "created_by":        created_by,
        })

        if not po_rows:
            return POResult(success=False, error="DB insert returned no rows")

        po_id = po_rows[0]["po_id"]

        # Generate PO number (e.g. ADV-2026-0042 or PO-2026-0042)
        prefix     = "ADV" if source == SOURCE_ADVISORY else "PO"
        year       = datetime.date.today().year
        # Use transactional registry for gap-free PO numbers
        try:
            from modules.db.order_number_registry import alloc_doc_number
            po_number = alloc_doc_number("PURCHASE_ORDER")
        except Exception:
            po_number = f"{prefix}-{year}-{po_id[-4:].upper()}"

        run_query("""
            UPDATE supplier_orders SET po_number = %(po_number)s
            WHERE id::text = %(po_id)s
        """, {"po_number": po_number, "po_id": po_id})

        # Insert line items
        for item in items:
            run_query("""
                INSERT INTO supplier_order_items
                  (supplier_order_id, product_id, product_name, quantity,
                   unit_price, notes)
                VALUES
                  (%(po_id)s, %(pid)s, %(pname)s, %(qty)s,
                   %(price)s, %(notes)s)
            """, {
                "po_id":  po_id,
                "pid":    item.product_id,
                "pname":  item.product_name,
                "qty":    item.qty,
                "price":  item.unit_price,
                "notes":  item.notes,
            })

        log.info(
            f"[POEngine] Created {source} PO {po_number} "
            f"for {supplier_name} — {len(items)} items"
        )

        return POResult(
            success   = True,
            po_id     = po_id,
            po_number = po_number,
            message   = (
                f"✅ PO {po_number} created for {supplier_name} "
                f"({len(items)} item{'s' if len(items) > 1 else ''})"
            ),
        )

    except Exception as e:
        log.error(f"[POEngine] create_po failed: {e}")
        return POResult(success=False, error=str(e))


# ═══════════════════════════════════════════════════════════════════════
# SEND  (WhatsApp / Email stub — wire real provider here)
# ═══════════════════════════════════════════════════════════════════════

def send_po(po_id: str, channel: str = "whatsapp") -> SendResult:
    """
    Send a PO via WhatsApp or Email.
    Updates PO status to Sent on success.
    """
    po = get_po(po_id)
    if not po:
        return SendResult(success=False, channel=channel, message="", error="PO not found")

    message = format_po_message(po)

    try:
        if channel == "whatsapp":
            _send_whatsapp(po, message)
        elif channel == "email":
            _send_email(po, message)
        else:
            return SendResult(
                success=False, channel=channel, message=message,
                error=f"Unknown channel: {channel}"
            )

        update_po_status(po_id, STATUS_SENT)
        log.info(f"[POEngine] PO {po.get('po_number')} sent via {channel}")
        return SendResult(success=True, channel=channel, message=message)

    except Exception as e:
        log.error(f"[POEngine] send_po failed: {e}")
        return SendResult(success=False, channel=channel, message=message, error=str(e))


def _send_whatsapp(po: Dict, message: str) -> None:
    """Stub — wire to WhatsApp Business API or wa-automate here."""
    from modules.backoffice.kernel import flag
    if flag("enable_whatsapp_po", False):
        # TODO: integrate WhatsApp provider
        log.info(f"[POEngine] WhatsApp stub for PO {po.get('po_number')}")
    else:
        log.info("[POEngine] WhatsApp disabled by flag enable_whatsapp_po=False")


def _send_email(po: Dict, message: str) -> None:
    """Stub — wire to email provider (SendGrid, SES, SMTP) here."""
    log.info(f"[POEngine] Email stub for PO {po.get('po_number')}")


# ═══════════════════════════════════════════════════════════════════════
# PO RETRIEVAL
# ═══════════════════════════════════════════════════════════════════════

def get_po(po_id: str) -> Optional[Dict]:
    """Load a full PO with its items."""
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT
                so.id::text          AS po_id,
                so.po_number,
                so.source,
                so.status,
                so.notes,
                so.created_at,
                so.expected_delivery,
                p.party_name         AS supplier_name,
                p.mobile             AS supplier_mobile,
                p.email              AS supplier_email,
                json_agg(json_build_object(
                    'product_id',   soi.product_id,
                    'product_name', soi.product_name,
                    'qty',          soi.quantity,
                    'unit_price',   soi.unit_price,
                    'notes',        soi.notes
                )) AS items
            FROM supplier_orders so
            JOIN parties p ON p.id::text = so.supplier_id::text
            LEFT JOIN supplier_order_items soi ON soi.supplier_order_id = so.id
            WHERE so.id::text = %(po_id)s
            GROUP BY so.id, p.party_name, p.mobile, p.email
        """, {"po_id": po_id})
        return rows[0] if rows else None
    except Exception as e:
        log.warning(f"[POEngine] get_po failed: {e}")
        return None


def update_po_status(po_id: str, status: str, notes: str = "") -> bool:
    """Update PO status. Returns True on success."""
    if status not in VALID_STATUSES:
        log.warning(f"[POEngine] Invalid status: {status}")
        return False
    try:
        from modules.sql_adapter import run_query
        run_query("""
            UPDATE supplier_orders
               SET status     = %(status)s,
                   updated_at = NOW(),
                   notes      = CASE WHEN %(notes)s != '' THEN %(notes)s ELSE notes END
             WHERE id::text = %(po_id)s
        """, {"status": status, "notes": notes, "po_id": po_id})
        return True
    except Exception as e:
        log.error(f"[POEngine] update_po_status failed: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════
# MESSAGE FORMATTING  (shared template for WhatsApp + print)
# ═══════════════════════════════════════════════════════════════════════

def format_po_message(po: Dict) -> str:
    """
    Render a PO as a WhatsApp-ready message.
    Works for both DAILY and ADVISORY sources.
    """
    today         = datetime.date.today().strftime("%d %b %Y")
    supplier_name = po.get("supplier_name", "Supplier")
    po_number     = po.get("po_number", "N/A")
    items         = po.get("items") or []
    notes         = po.get("notes", "")
    expected      = po.get("expected_delivery", "")

    lines = []
    for item in items:
        if isinstance(item, dict):
            name  = item.get("product_name", "N/A")
            qty   = item.get("qty", 1)
            price = item.get("unit_price")
            line  = f"  • {name} × {qty}"
            if price:
                line += f" @ ₹{float(price):.2f}"
            lines.append(line)

    items_text = "\n".join(lines) if lines else "  (No items)"

    message_parts = [
        f"*Purchase Order — {today}*",
        f"PO #: {po_number}",
        "",
        f"Dear {supplier_name},",
        "",
        "Please supply the following items:",
        items_text,
    ]

    if expected:
        message_parts.append(f"\nExpected delivery: {expected}")
    if notes:
        message_parts.append(f"\nNotes: {notes}")

    message_parts += [
        "",
        "Please confirm availability and delivery date.",
        "",
        "Thank you.",
    ]

    return "\n".join(message_parts)


# ═══════════════════════════════════════════════════════════════════════
# AUTO SUPPLIER ORDER POPULATION
# ═══════════════════════════════════════════════════════════════════════

def auto_populate_supplier_orders(order_id: str, created_by: str = "system") -> dict:
    """
    Called after an order is saved (or on demand from backoffice).

    Scans all VENDOR/EXTERNAL_LAB lines of the order that have a
    preferred_supplier_id set on their product.

    Groups lines by supplier → creates or appends to a DRAFT supplier
    order per supplier.

    Returns:
        {
            "created":  [list of new PO numbers],
            "appended": [list of existing PO ids updated],
            "skipped":  [list of {product_name, reason}],
        }
    """
    try:
        from modules.sql_adapter import run_query, run_write
    except ImportError:
        return {"created": [], "appended": [], "skipped": [],
                "error": "sql_adapter unavailable"}

    created  = []
    appended = []
    skipped  = []

    # ── Fetch VENDOR lines with preferred_supplier_id ─────────────────
    rows = run_query("""
        SELECT
            ol.id::text                             AS line_id,
            ol.product_id::text                     AS product_id,
            COALESCE(p.product_name, 'Unknown')     AS product_name,
            COALESCE(p.brand, '')                   AS brand,
            ol.quantity,
            ol.sph, ol.cyl, ol.axis, ol.add_power,
            ol.eye_side,
            COALESCE(ol.unit_price, 0)              AS unit_price,
            p.preferred_supplier_id::text           AS supplier_id,
            COALESCE(sup.party_name, '')            AS supplier_name,
            COALESCE(
                (ol.lens_params::jsonb->>'manufacturing_route'),
                'VENDOR'
            )                                       AS route
        FROM order_lines ol
        JOIN products p ON p.id = ol.product_id
        LEFT JOIN parties sup ON sup.id = p.preferred_supplier_id
        WHERE ol.order_id = %(oid)s::uuid
          AND COALESCE(ol.is_deleted, FALSE) = FALSE
          AND COALESCE(
                ol.lens_params::jsonb->>'manufacturing_route',
                'STOCK'
              ) IN ('VENDOR', 'EXTERNAL_LAB')
          AND p.preferred_supplier_id IS NOT NULL
    """, {"oid": order_id}) or []

    if not rows:
        return {"created": [], "appended": [], "skipped": []}

    # ── Group by supplier ─────────────────────────────────────────────
    by_supplier = {}
    for r in rows:
        sid = r["supplier_id"]
        if sid not in by_supplier:
            by_supplier[sid] = {
                "supplier_name": r["supplier_name"],
                "items": [],
            }
        # Build power note for the PO line
        power_parts = []
        if r.get("sph") is not None:   power_parts.append(f"SPH {float(r['sph']):+.2f}")
        if r.get("cyl") is not None:   power_parts.append(f"CYL {float(r['cyl']):+.2f}")
        if r.get("axis") is not None:  power_parts.append(f"AX {int(r['axis'])}")
        if r.get("add_power"):         power_parts.append(f"ADD {float(r['add_power']):+.2f}")
        if r.get("eye_side"):          power_parts.append(f"Eye:{r['eye_side']}")
        power_note = " | ".join(power_parts) if power_parts else ""

        by_supplier[sid]["items"].append(POItem(
            product_id   = r["product_id"],
            product_name = r["product_name"],
            qty          = int(r["quantity"] or 1),
            unit_price   = float(r["unit_price"] or 0),
            notes        = power_note,
        ))

    # ── Create/append one PO per supplier ─────────────────────────────
    import datetime as dt

    for sid, data in by_supplier.items():
        sname = data["supplier_name"] or sid
        items = data["items"]

        # Resolve effective supplier per item — override may redirect
        # individual products to alternate supplier
        items_by_supplier = {}   # effective_sid → {sname, tat, items[]}
        for item in items:
            eff = get_effective_supplier(item.product_id)
            eff_sid   = eff["supplier_id"] or sid
            eff_sname = eff["supplier_name"] or sname
            eff_tat   = eff["tat_days"]

            if eff["is_override"]:
                # Annotate note with override reason
                item.notes += (
                    f" [⚠️ Routed to {eff_sname}"
                    + (f": {eff['override_reason']}" if eff["override_reason"] else "")
                    + "]"
                )

            if eff_sid not in items_by_supplier:
                items_by_supplier[eff_sid] = {
                    "sname": eff_sname, "tat": eff_tat, "items": []
                }
            items_by_supplier[eff_sid]["items"].append(item)

        # Now create/append one PO per effective supplier
        for eff_sid, eff_data in items_by_supplier.items():
            eff_sname = eff_data["sname"]
            eff_tat   = eff_data["tat"]
            eff_items = eff_data["items"]

            # Expected delivery
            exp_delivery = calculate_expected_delivery(
                supplier_id  = eff_sid,
                placement_dt = dt.datetime.now(),
                tat_days     = eff_tat,
            )

            # Check for existing DRAFT PO for this supplier+order
            existing = run_query("""
                SELECT id::text AS po_id, po_number
                FROM supplier_orders
                WHERE supplier_id = %(sid)s::uuid
                  AND status = 'Draft'
                  AND %(oid)s = ANY(COALESCE(source_order_ids, ARRAY[]::text[]))
                ORDER BY created_at DESC
                LIMIT 1
            """, {"sid": eff_sid, "oid": order_id})

            if existing:
                po_id = existing[0]["po_id"]
                for item in eff_items:
                    run_write("""
                        INSERT INTO supplier_order_items
                          (supplier_order_id, product_id, product_name,
                           quantity, unit_price, notes)
                        VALUES
                          (%(po_id)s::uuid, %(pid)s::uuid, %(pname)s,
                           %(qty)s, %(up)s, %(notes)s)
                        ON CONFLICT DO NOTHING
                    """, {
                        "po_id": po_id, "pid": item.product_id,
                        "pname": item.product_name, "qty": item.qty,
                        "up": item.unit_price, "notes": item.notes,
                    })
                appended.append({"po_id": po_id, "supplier": eff_sname,
                                 "items_added": len(eff_items)})
            else:
                result = create_po(
                    source        = f"ORDER:{order_id}",
                    supplier_id   = eff_sid,
                    supplier_name = eff_sname,
                    items         = eff_items,
                    notes         = f"Auto-populated from order {order_id}",
                    created_by    = created_by,
                )
                if result.success:
                    run_write("""
                        UPDATE supplier_orders
                           SET source_order_ids = ARRAY_APPEND(
                                   COALESCE(source_order_ids, ARRAY[]::text[]),
                                   %(oid)s
                               ),
                               expected_delivery_date = %(exp)s
                         WHERE id = %(po_id)s::uuid
                    """, {"oid": order_id, "po_id": result.po_id,
                          "exp": exp_delivery})
                    created.append({
                        "po_number":         result.po_number,
                        "supplier":          eff_sname,
                        "items":             len(eff_items),
                        "expected_delivery": exp_delivery,
                    })
                else:
                    for item in eff_items:
                        skipped.append({
                            "product_name": item.product_name,
                            "reason": result.error or "PO creation failed",
                        })

    return {"created": created, "appended": appended, "skipped": skipped}


# ═══════════════════════════════════════════════════════════════════════
# TAT & SCHEDULE INTELLIGENCE
# ═══════════════════════════════════════════════════════════════════════

def calculate_expected_delivery(
    supplier_id: str,
    placement_dt=None,
    tat_days: int = 1,
) -> Optional[str]:
    """
    Calculate expected delivery date accounting for:
    - Order cutoff time (orders after cutoff count as next day)
    - Supplier closed days (weekdays skipped in TAT count)

    Returns ISO date string or None on error.
    """
    import datetime as dt

    try:
        from modules.sql_adapter import run_query

        # Fetch supplier schedule
        rows = run_query("""
            SELECT
                COALESCE(supplier_closed_days, ARRAY[]::text[]) AS closed_days,
                order_cutoff_time
            FROM parties
            WHERE id = %(sid)s::uuid
            LIMIT 1
        """, {"sid": supplier_id})

        closed_days  = []   # e.g. ["Sunday", "Saturday"]
        cutoff_time  = None

        if rows:
            raw_closed = rows[0].get("supplier_closed_days") or []
            closed_days = [str(d).strip().lower() for d in raw_closed]
            cutoff_time = rows[0].get("order_cutoff_time")

        # Normalise placement datetime
        if placement_dt is None:
            placement_dt = dt.datetime.now()

        # Day-of-week names (Python: Monday=0 … Sunday=6)
        _dow_map = {
            "monday": 0, "tuesday": 1, "wednesday": 2,
            "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
        }
        closed_nums = {_dow_map[d] for d in closed_days if d in _dow_map}

        # If placed after cutoff → order counts from tomorrow
        start_date = placement_dt.date()
        if cutoff_time:
            try:
                # cutoff_time may arrive as datetime.time or string
                if isinstance(cutoff_time, str):
                    h, m = int(cutoff_time[:2]), int(cutoff_time[3:5])
                    cutoff_time = dt.time(h, m)
                if placement_dt.time() > cutoff_time:
                    start_date += dt.timedelta(days=1)
            except Exception:
                pass

        # Count TAT days forward, skipping closed days
        days_counted = 0
        current     = start_date
        while days_counted < tat_days:
            current += dt.timedelta(days=1)
            if current.weekday() not in closed_nums:
                days_counted += 1

        return current.isoformat()

    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════
# SUPPLIER OVERRIDE LOOKUP
# ═══════════════════════════════════════════════════════════════════════

def get_effective_supplier(product_id: str) -> dict:
    """
    Returns the effective supplier for a product.

    Checks supplier_product_override first (manual leak valve).
    Falls back to products.preferred_supplier_id.

    Returns:
        {
            "supplier_id":   str or None,
            "supplier_name": str,
            "tat_days":      int,
            "is_override":   bool,
            "override_reason": str or None,
        }
    """
    try:
        from modules.sql_adapter import run_query

        # Check active override first
        override = run_query("""
            SELECT
                spo.override_supplier_id::text  AS supplier_id,
                COALESCE(p.party_name, '')       AS supplier_name,
                spo.reason                       AS override_reason
            FROM supplier_product_override spo
            JOIN parties p ON p.id = spo.override_supplier_id
            WHERE spo.product_id = %(pid)s::uuid
              AND spo.is_active = TRUE
            ORDER BY spo.created_at DESC
            LIMIT 1
        """, {"pid": product_id})

        if override:
            # Get TAT from product (same TAT regardless of supplier)
            tat_row = run_query("""
                SELECT COALESCE(supplier_tat_days, 1) AS tat
                FROM products WHERE id = %(pid)s::uuid LIMIT 1
            """, {"pid": product_id})
            tat = int((tat_row[0]["tat"] if tat_row else 1) or 1)

            return {
                "supplier_id":     override[0]["supplier_id"],
                "supplier_name":   override[0]["supplier_name"],
                "tat_days":        tat,
                "is_override":     True,
                "override_reason": override[0].get("override_reason"),
            }

        # Fall back to preferred supplier
        product = run_query("""
            SELECT
                p.preferred_supplier_id::text       AS supplier_id,
                COALESCE(sup.party_name, '')         AS supplier_name,
                COALESCE(p.supplier_tat_days, 1)    AS tat_days
            FROM products p
            LEFT JOIN parties sup ON sup.id = p.preferred_supplier_id
            WHERE p.id = %(pid)s::uuid
            LIMIT 1
        """, {"pid": product_id})

        if product and product[0].get("supplier_id"):
            return {
                "supplier_id":     product[0]["supplier_id"],
                "supplier_name":   product[0]["supplier_name"],
                "tat_days":        int(product[0]["tat_days"] or 1),
                "is_override":     False,
                "override_reason": None,
            }

        return {
            "supplier_id":     None,
            "supplier_name":   "",
            "tat_days":        1,
            "is_override":     False,
            "override_reason": None,
        }

    except Exception:
        return {
            "supplier_id": None, "supplier_name": "",
            "tat_days": 1, "is_override": False, "override_reason": None,
        }


def set_supplier_override(
    product_id: str,
    override_supplier_id: str,
    reason: str = "",
    created_by: str = "operator",
) -> bool:
    """
    Set a manual supplier override for a product.
    Deactivates any existing override first (one active override per product).
    """
    try:
        from modules.sql_adapter import run_write

        # Deactivate existing
        run_write("""
            UPDATE supplier_product_override
               SET is_active = FALSE, updated_at = NOW()
             WHERE product_id = %(pid)s::uuid
               AND is_active  = TRUE
        """, {"pid": product_id})

        # Insert new
        run_write("""
            INSERT INTO supplier_product_override
              (product_id, override_supplier_id, reason,
               is_active, created_by, created_at, updated_at)
            VALUES
              (%(pid)s::uuid, %(sid)s::uuid, %(reason)s,
               TRUE, %(by)s, NOW(), NOW())
        """, {
            "pid": product_id, "sid": override_supplier_id,
            "reason": reason, "by": created_by,
        })
        return True
    except Exception:
        return False


def clear_supplier_override(product_id: str) -> bool:
    """Clear the active override — reverts to preferred supplier."""
    try:
        from modules.sql_adapter import run_write
        run_write("""
            UPDATE supplier_product_override
               SET is_active = FALSE, updated_at = NOW()
             WHERE product_id = %(pid)s::uuid AND is_active = TRUE
        """, {"pid": product_id})
        return True
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════
# REORDER TRIGGER — checks min_stock_qty and raises PO if needed
# ═══════════════════════════════════════════════════════════════════════

def check_and_trigger_reorders(created_by: str = "system") -> dict:
    """
    Scans product_stock_minimum for all reorder_enabled rows.
    For each power-level entry where combined stock across ALL batches
    is below min_qty — and no OPEN reorder_log entry exists — raises a PO.

    Power-level matching:
        inventory_stock joined on product_id + sph + cyl + axis + add_power
        Quantities summed across all active batches for that power.

    Returns summary dict.
    """
    import datetime as dt

    try:
        from modules.sql_adapter import run_query, run_write
    except ImportError:
        return {"triggered": [], "skipped": [], "error": "sql_adapter unavailable"}

    triggered = []
    skipped   = []

    # ── Fetch all reorder-enabled power rows below minimum ─────────────
    rows = run_query("""
        SELECT
            psm.id::text                            AS psm_id,
            psm.product_id::text                    AS product_id,
            p.product_name,
            psm.sph, psm.cyl, psm.axis, psm.add_power, psm.eye_side,
            psm.min_qty,
            psm.reorder_qty,
            COALESCE(p.supplier_tat_days, 1)        AS tat_days,
            COALESCE(SUM(i.quantity), 0)            AS combined_qty
        FROM product_stock_minimum psm
        JOIN products p ON p.id = psm.product_id
        LEFT JOIN inventory_stock i
               ON i.product_id = psm.product_id
              AND COALESCE(i.sph,       0) = COALESCE(psm.sph,       0)
              AND COALESCE(i.cyl,       0) = COALESCE(psm.cyl,       0)
              AND COALESCE(i.axis,      0) = COALESCE(psm.axis,      0)
              AND COALESCE(i.add_power, 0) = COALESCE(psm.add_power, 0)
              AND COALESCE(i.is_active, TRUE) = TRUE
        WHERE psm.reorder_enabled = TRUE
          AND COALESCE(p.is_active, TRUE) = TRUE
          AND p.preferred_supplier_id IS NOT NULL
          AND COALESCE(psm.auto_order_enabled, FALSE) = TRUE
        GROUP BY
            psm.id, psm.product_id, p.product_name,
            psm.sph, psm.cyl, psm.axis, psm.add_power, psm.eye_side,
            psm.min_qty, psm.reorder_qty, p.supplier_tat_days
        HAVING COALESCE(SUM(i.quantity), 0) < psm.min_qty
    """) or []

    for row in rows:
        pid     = row["product_id"]
        pname   = row["product_name"]
        qty     = float(row["combined_qty"])
        minq    = float(row["min_qty"])
        reord_q = int(row["reorder_qty"] or max(1, int(minq - qty)))
        tat     = int(row["tat_days"] or 1)

        # Build power description for PO note
        power_parts = []
        if row.get("sph")       is not None: power_parts.append(f"SPH {float(row['sph']):+.2f}")
        if row.get("cyl")       is not None: power_parts.append(f"CYL {float(row['cyl']):+.2f}")
        if row.get("axis")      is not None: power_parts.append(f"AX {int(row['axis'])}")
        if row.get("add_power") is not None: power_parts.append(f"ADD {float(row['add_power']):+.2f}")
        if row.get("eye_side"):              power_parts.append(f"Eye:{row['eye_side']}")
        power_str = " | ".join(power_parts) if power_parts else "No power"

        # Check for existing open reorder for this exact power
        open_reorder = run_query("""
            SELECT id FROM reorder_log
            WHERE product_id = %(pid)s::uuid
              AND status = 'OPEN'
              AND notes LIKE %(pstr)s
            LIMIT 1
        """, {"pid": pid, "pstr": f"%{power_str}%"})

        if open_reorder:
            skipped.append({
                "product": pname, "power": power_str,
                "reason": f"Open reorder exists (stock {qty:.0f} < min {minq:.0f})"
            })
            continue

        # Effective supplier (override → preferred)
        eff = get_effective_supplier(pid)
        if not eff["supplier_id"]:
            skipped.append({"product": pname, "power": power_str,
                            "reason": "No supplier assigned"})
            continue

        sid   = eff["supplier_id"]
        sname = eff["supplier_name"]

        # ── Smart reorder qty ────────────────────────────────────────
        smart = calculate_smart_reorder_qty(
            product_id  = pid,
            sph         = row.get("sph"),
            cyl         = row.get("cyl"),
            axis        = row.get("axis"),
            add_power   = row.get("add_power"),
            min_qty     = int(minq),
            supplier_id = sid,
            tat_days    = tat,
        )
        reord_q = smart["reorder_qty"]

        # Update advisory fields in psm row
        try:
            run_write("""
                UPDATE product_stock_minimum
                   SET suggested_reorder_qty = %(rq)s,
                       avg_daily_sales       = %(avg)s,
                       last_advisory_at      = NOW()
                 WHERE id = %(id)s::uuid
            """, {
                "id":  row["psm_id"],
                "rq":  reord_q,
                "avg": smart["avg_daily_sales"] or None,
            })
        except Exception:
            pass

        exp_delivery = calculate_expected_delivery(
            supplier_id  = sid,
            placement_dt = dt.datetime.now(),
            tat_days     = tat,
        )

        result = create_po(
            source        = "REORDER",
            supplier_id   = sid,
            supplier_name = sname,
            items         = [POItem(
                product_id   = pid,
                product_name = pname,
                qty          = reord_q,
                notes        = (
                    f"{power_str} — stock {qty:.0f} below min {minq:.0f} | "
                    f"{smart['advisory_note']}"
                    + (f" [override: {eff['override_reason']}]"
                       if eff["is_override"] else "")
                ),
            )],
            notes      = f"Auto reorder — {pname} {power_str}",
            created_by = created_by,
        )

        if result.success:
            run_write("""
                INSERT INTO reorder_log
                  (product_id, supplier_id, supplier_order_id,
                   triggered_at, expected_delivery, status, notes)
                VALUES
                  (%(pid)s::uuid, %(sid)s::uuid, %(so_id)s,
                   NOW(), %(exp)s, 'OPEN', %(notes)s)
            """, {
                "pid":   pid, "sid": sid, "so_id": result.po_id,
                "exp":   exp_delivery,
                "notes": f"{pname} | {power_str} | stock {qty:.0f} < min {minq:.0f} | TAT {tat}d"
                         + (" [OVERRIDE]" if eff["is_override"] else ""),
            })
            if exp_delivery:
                run_write("""
                    UPDATE supplier_orders
                       SET expected_delivery_date = %(exp)s
                     WHERE id = %(so_id)s
                """, {"exp": exp_delivery, "so_id": result.po_id})

            triggered.append({
                "product":           pname,
                "power":             power_str,
                "reorder_qty":       reord_q,
                "po_number":         result.po_number,
                "supplier":          sname,
                "expected_delivery": exp_delivery,
                "is_override":       eff["is_override"],
            })
        else:
            skipped.append({
                "product": pname, "power": power_str,
                "reason": result.error or "PO creation failed",
            })

    return {"triggered": triggered, "skipped": skipped}


# ═══════════════════════════════════════════════════════════════════════
# DEMAND ADVISORY ENGINE
# ═══════════════════════════════════════════════════════════════════════

def _get_data_maturity(product_id: str) -> dict:
    """
    Check how much sales history exists for a product.
    Returns:
        months_of_data: int
        total_orders:   int
        phase:          'NONE' | 'EARLY' | 'PHASE1' | 'PHASE2'
    """
    try:
        from modules.sql_adapter import run_query
        r = run_query("""
            SELECT
                COUNT(DISTINCT ol.id)                           AS total_lines,
                ROUND(
                    EXTRACT(EPOCH FROM (NOW() - MIN(o.created_at)))
                    / (30.0 * 24 * 3600)
                , 1)                                            AS months_of_data
            FROM order_lines ol
            JOIN orders o ON o.id = ol.order_id
            WHERE ol.product_id = %(pid)s::uuid
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
              AND COALESCE(o.status,'') NOT IN ('CANCELLED','RETURNED')
        """, {"pid": product_id})

        months = float(r[0]["months_of_data"] or 0) if r else 0
        total  = int(r[0]["total_lines"]      or 0) if r else 0

        if months < 1 or total < 10:
            phase = "NONE"
        elif months < 3:
            phase = "EARLY"
        elif months < 12:
            phase = "PHASE1"
        else:
            phase = "PHASE2"

        return {"months_of_data": months, "total_orders": total, "phase": phase}
    except Exception:
        return {"months_of_data": 0, "total_orders": 0, "phase": "NONE"}


def calculate_smart_reorder_qty(
    product_id:    str,
    sph:           float = None,
    cyl:           float = None,
    axis:          int   = None,
    add_power:     float = None,
    min_qty:       int   = 1,
    supplier_id:   str   = None,
    tat_days:      int   = 1,
) -> dict:
    """
    Calculate intelligent reorder quantity using the formula:

        Reorder = min_qty
                - current_stock
                + pending_demand          (open order lines not yet billed)
                - stock_in_transit        (open POs not yet received)
                + TAT_demand              (expected sales during delivery window)

    TAT_demand calculation depends on data maturity:
        NONE / EARLY  → 0 (no reliable history)
        PHASE1        → avg_daily_sales (last 90 days) × tat_days
        PHASE2        → weighted avg using seasonal + trend factors (12m+ data)

    Returns dict with:
        reorder_qty         int    — final recommended qty
        current_stock       int
        pending_demand      int
        stock_in_transit    int
        tat_demand          float
        avg_daily_sales     float
        phase               str    — data maturity phase
        breakdown           dict   — all components for display
        advisory_note       str    — human-readable explanation
    """
    import datetime as dt

    try:
        from modules.sql_adapter import run_query
    except ImportError:
        return {"reorder_qty": max(1, min_qty), "phase": "NONE",
                "advisory_note": "DB unavailable"}

    sph_v = sph       or 0
    cyl_v = cyl       or 0
    ax_v  = axis      or 0
    add_v = add_power or 0

    # ── 1. Current combined stock ────────────────────────────────────
    stock_r = run_query("""
        SELECT COALESCE(SUM(quantity), 0) AS qty
        FROM inventory_stock
        WHERE product_id = %(pid)s::uuid
          AND COALESCE(sph,       0) = %(sph)s
          AND COALESCE(cyl,       0) = %(cyl)s
          AND COALESCE(axis,      0) = %(ax)s
          AND COALESCE(add_power, 0) = %(add)s
          AND COALESCE(is_active, TRUE) = TRUE
    """, {"pid": product_id, "sph": sph_v, "cyl": cyl_v,
          "ax": ax_v, "add": add_v})
    current_stock = int(stock_r[0]["qty"] if stock_r else 0)

    # ── 2. Pending demand (open orders, not billed) ──────────────────
    demand_r = run_query("""
        SELECT COALESCE(SUM(ol.quantity - COALESCE(ol.billed_qty, 0)), 0) AS qty
        FROM order_lines ol
        JOIN orders o ON o.id = ol.order_id
        WHERE ol.product_id = %(pid)s::uuid
          AND COALESCE(ol.sph,       0) = %(sph)s
          AND COALESCE(ol.cyl,       0) = %(cyl)s
          AND COALESCE(ol.axis,      0) = %(ax)s
          AND COALESCE(ol.add_power, 0) = %(add)s
          AND COALESCE(ol.is_deleted, FALSE) = FALSE
          AND COALESCE(ol.billed_qty, 0) < ol.quantity
          AND o.status NOT IN ('CANCELLED','RETURNED','CLOSED','BILLED','DELIVERED')
    """, {"pid": product_id, "sph": sph_v, "cyl": cyl_v,
          "ax": ax_v, "add": add_v})
    pending_demand = int(demand_r[0]["qty"] if demand_r else 0)

    # ── 3. Stock in transit (open supplier POs) ──────────────────────
    transit_r = run_query("""
        SELECT COALESCE(SUM(soi.ordered_qty - COALESCE(soi.received_qty, 0)), 0) AS qty
        FROM supplier_order_items soi
        JOIN supplier_orders so ON so.id = soi.supplier_order_id
        JOIN products p ON p.id = soi.product_id::uuid
        WHERE soi.product_id::uuid = %(pid)s::uuid
          AND so.status NOT IN ('Received','Cancelled','Rejected')
          AND COALESCE(soi.received_qty, 0) < soi.ordered_qty
    """, {"pid": product_id})
    stock_in_transit = int(transit_r[0]["qty"] if transit_r else 0)

    # ── 4. TAT demand — depends on data maturity ────────────────────
    maturity   = _get_data_maturity(product_id)
    phase      = maturity["phase"]
    tat_demand = 0.0
    avg_daily  = 0.0

    if phase in ("PHASE1", "PHASE2"):

        if phase == "PHASE1":
            # Simple: avg daily sales last 90 days × TAT
            sales_r = run_query("""
                SELECT COALESCE(SUM(ol.quantity), 0) AS total_qty
                FROM order_lines ol
                JOIN orders o ON o.id = ol.order_id
                WHERE ol.product_id = %(pid)s::uuid
                  AND COALESCE(ol.sph,       0) = %(sph)s
                  AND COALESCE(ol.cyl,       0) = %(cyl)s
                  AND COALESCE(ol.axis,      0) = %(ax)s
                  AND COALESCE(ol.add_power, 0) = %(add)s
                  AND COALESCE(ol.is_deleted, FALSE) = FALSE
                  AND o.created_at >= NOW() - INTERVAL '90 days'
                  AND o.status NOT IN ('CANCELLED','RETURNED')
            """, {"pid": product_id, "sph": sph_v, "cyl": cyl_v,
                  "ax": ax_v, "add": add_v})
            total_90d = float(sales_r[0]["total_qty"] if sales_r else 0)
            avg_daily = round(total_90d / 90, 3)
            tat_demand = round(avg_daily * tat_days, 2)

        elif phase == "PHASE2":
            # Seasonal + trend weighted:
            # Compare same period last year vs 90-day avg
            # Apply growth trend factor
            today = dt.date.today()
            year_ago_start = today - dt.timedelta(days=395)
            year_ago_end   = today - dt.timedelta(days=335)

            # Same month last year
            same_period_r = run_query("""
                SELECT COALESCE(SUM(ol.quantity), 0) AS qty
                FROM order_lines ol
                JOIN orders o ON o.id = ol.order_id
                WHERE ol.product_id = %(pid)s::uuid
                  AND COALESCE(ol.sph,       0) = %(sph)s
                  AND COALESCE(ol.cyl,       0) = %(cyl)s
                  AND COALESCE(ol.axis,      0) = %(ax)s
                  AND COALESCE(ol.add_power, 0) = %(add)s
                  AND COALESCE(ol.is_deleted, FALSE) = FALSE
                  AND o.created_at::date BETWEEN %(from)s AND %(to)s
                  AND o.status NOT IN ('CANCELLED','RETURNED')
            """, {"pid": product_id, "sph": sph_v, "cyl": cyl_v,
                  "ax": ax_v, "add": add_v,
                  "from": year_ago_start, "to": year_ago_end})
            same_period_qty = float(same_period_r[0]["qty"] if same_period_r else 0)

            # Last 90 days
            recent_r = run_query("""
                SELECT COALESCE(SUM(ol.quantity), 0) AS qty
                FROM order_lines ol
                JOIN orders o ON o.id = ol.order_id
                WHERE ol.product_id = %(pid)s::uuid
                  AND COALESCE(ol.sph,       0) = %(sph)s
                  AND COALESCE(ol.cyl,       0) = %(cyl)s
                  AND COALESCE(ol.axis,      0) = %(ax)s
                  AND COALESCE(ol.add_power, 0) = %(add)s
                  AND COALESCE(ol.is_deleted, FALSE) = FALSE
                  AND o.created_at >= NOW() - INTERVAL '90 days'
                  AND o.status NOT IN ('CANCELLED','RETURNED')
            """, {"pid": product_id, "sph": sph_v, "cyl": cyl_v,
                  "ax": ax_v, "add": add_v})
            recent_qty = float(recent_r[0]["qty"] if recent_r else 0)

            avg_daily_recent      = recent_qty / 90
            avg_daily_same_period = same_period_qty / 60

            # Seasonal factor: same period last year vs recent trend
            if avg_daily_same_period > 0 and avg_daily_recent > 0:
                seasonal_factor = avg_daily_same_period / avg_daily_recent
                # Cap seasonal factor to prevent wild swings
                seasonal_factor = max(0.5, min(2.5, seasonal_factor))
            else:
                seasonal_factor = 1.0

            # Growth trend: 12m vs 6m growth rate
            trend_r = run_query("""
                SELECT
                    COALESCE(SUM(CASE WHEN o.created_at >= NOW() - INTERVAL '180 days'
                                     THEN ol.quantity END), 0) AS last_6m,
                    COALESCE(SUM(CASE WHEN o.created_at < NOW() - INTERVAL '180 days'
                                      AND o.created_at >= NOW() - INTERVAL '360 days'
                                     THEN ol.quantity END), 0) AS prev_6m
                FROM order_lines ol
                JOIN orders o ON o.id = ol.order_id
                WHERE ol.product_id = %(pid)s::uuid
                  AND COALESCE(ol.sph,       0) = %(sph)s
                  AND COALESCE(ol.cyl,       0) = %(cyl)s
                  AND COALESCE(ol.axis,      0) = %(ax)s
                  AND COALESCE(ol.add_power, 0) = %(add)s
                  AND COALESCE(ol.is_deleted, FALSE) = FALSE
                  AND o.status NOT IN ('CANCELLED','RETURNED')
            """, {"pid": product_id, "sph": sph_v, "cyl": cyl_v,
                  "ax": ax_v, "add": add_v})
            last_6m = float(trend_r[0]["last_6m"] if trend_r else 0)
            prev_6m = float(trend_r[0]["prev_6m"] if trend_r else 0)

            if prev_6m > 0:
                trend_factor = last_6m / prev_6m
                trend_factor = max(0.7, min(2.0, trend_factor))
            else:
                trend_factor = 1.0

            avg_daily = round(avg_daily_recent * trend_factor, 3)
            tat_demand = round(avg_daily * seasonal_factor * tat_days, 2)

    # ── 5. Final reorder qty ─────────────────────────────────────────
    raw_qty = (
        min_qty
        - current_stock
        + pending_demand
        - stock_in_transit
        + tat_demand
    )
    reorder_qty = max(1, int(round(raw_qty)))

    # ── 6. Build advisory note ───────────────────────────────────────
    if phase == "NONE":
        note = (
            f"📊 Insufficient data (< 1 month). "
            f"Ordering {reorder_qty} to restore min stock of {min_qty}."
        )
    elif phase == "EARLY":
        note = (
            f"📊 Early data ({maturity['months_of_data']:.0f} months). "
            f"TAT demand not yet calculated. "
            f"Ordering {reorder_qty} based on stock + pending orders."
        )
    elif phase == "PHASE1":
        note = (
            f"📊 {maturity['months_of_data']:.0f} months data. "
            f"Avg {avg_daily:.2f}/day × {tat_days}d TAT = "
            f"{tat_demand:.1f} TAT buffer. "
            f"Ordering {reorder_qty}."
        )
    else:  # PHASE2
        note = (
            f"📊 {maturity['months_of_data']:.0f} months data — seasonal + trend applied. "
            f"Weighted avg {avg_daily:.2f}/day × {tat_days}d TAT = "
            f"{tat_demand:.1f} TAT buffer. "
            f"Ordering {reorder_qty}."
        )

    return {
        "reorder_qty":      reorder_qty,
        "current_stock":    current_stock,
        "pending_demand":   pending_demand,
        "stock_in_transit": stock_in_transit,
        "tat_demand":       tat_demand,
        "avg_daily_sales":  avg_daily,
        "phase":            phase,
        "months_of_data":   maturity["months_of_data"],
        "advisory_note":    note,
        "breakdown": {
            "min_qty":           min_qty,
            "current_stock":     current_stock,
            "pending_demand":    pending_demand,
            "stock_in_transit":  stock_in_transit,
            "tat_demand":        tat_demand,
            "raw_qty":           raw_qty,
            "final_qty":         reorder_qty,
        },
    }


def run_advisory_update(product_id: str = None) -> dict:
    """
    Updates system_suggested_min and suggested_reorder_qty
    in product_stock_minimum for all rows (or one product).

    Called:
    - On demand from UI (operator clicks 'Refresh Suggestions')
    - Scheduled: weekly/monthly as data matures

    For each psm row:
    - Calculates smart reorder qty
    - Computes suggested_min = current avg_daily × safety_days (30d default)
    - Writes to system_suggested_min, suggested_reorder_qty, avg_daily_sales,
      last_advisory_at
    - Does NOT change min_qty or reorder_enabled — those are operator decisions
    """
    try:
        from modules.sql_adapter import run_query, run_write
    except ImportError:
        return {"updated": 0, "error": "DB unavailable"}

    where = "WHERE TRUE"
    params = {}
    if product_id:
        where = "WHERE psm.product_id = %(pid)s::uuid"
        params = {"pid": product_id}

    rows = run_query(f"""
        SELECT
            psm.id::text        AS psm_id,
            psm.product_id::text AS product_id,
            psm.sph, psm.cyl, psm.axis, psm.add_power, psm.eye_side,
            psm.min_qty,
            COALESCE(p.supplier_tat_days, 1) AS tat_days,
            p.preferred_supplier_id::text    AS supplier_id
        FROM product_stock_minimum psm
        JOIN products p ON p.id = psm.product_id
        {where}
    """, params) or []

    updated = 0
    for r in rows:
        try:
            result = calculate_smart_reorder_qty(
                product_id  = r["product_id"],
                sph         = r.get("sph"),
                cyl         = r.get("cyl"),
                axis        = r.get("axis"),
                add_power   = r.get("add_power"),
                min_qty     = int(r["min_qty"] or 1),
                supplier_id = r.get("supplier_id"),
                tat_days    = int(r["tat_days"] or 1),
            )

            # Suggested min = 30-day demand (safety stock policy)
            avg_daily   = result["avg_daily_sales"]
            sugg_min    = max(1, int(round(avg_daily * 30))) if avg_daily > 0 else None
            sugg_reord  = result["reorder_qty"]

            run_write("""
                UPDATE product_stock_minimum
                   SET system_suggested_min  = %(sugg_min)s,
                       suggested_reorder_qty = %(sugg_reord)s,
                       avg_daily_sales       = %(avg_daily)s,
                       last_advisory_at      = NOW()
                 WHERE id = %(id)s::uuid
            """, {
                "id":         r["psm_id"],
                "sugg_min":   sugg_min,
                "sugg_reord": sugg_reord,
                "avg_daily":  avg_daily if avg_daily > 0 else None,
            })
            updated += 1
        except Exception:
            pass

    return {"updated": updated, "total": len(rows)}
