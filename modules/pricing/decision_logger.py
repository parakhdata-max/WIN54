"""
core/decision_logger.py — FINAL LOCKED v3.0
============================================

Decision Logger — writes every discount decision to discount_decisions table.

WHY THIS EXISTS:
  Without logging: "We don't know which discount works" (Trap 4 from the doc).
  With logging: every fired rule, every competing rule, every margin outcome
  is recorded. Enables analytics, AI training, audit, and rollback analysis.

WHAT IT LOGS PER LINE:
  - which rule fired (applied_rule_id, name, type)
  - all competing rules that were evaluated (competing_rules JSONB)
  - full inputs (price, qty, gross, channel, brand_group, promo_code)
  - full outputs (discount, net, gst, final)
  - margin at time of decision (cost_price, margin_pct, margin_status)
  - conflict resolution context (conflict_strategy, rules_evaluated_count)

USAGE:
  # After calculating a line:
  result = engine.calculate(item)

  # Log the decision (async-safe: catches all DB errors, never crashes billing)
  logger = DecisionLogger(db_conn)
  logger.log(
      invoice_id = "inv-001",
      line_id    = "line-001",
      item       = item,
      result     = result,
  )

  # Retrieve decisions for an invoice:
  decisions = logger.get_invoice_decisions("inv-001")

  # Analytics:
  stats = logger.get_rule_stats("rule-id-here")
"""

from __future__ import annotations
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional
import json
import uuid

try:
    from .discount_rule import DiscountResult, LineItem
    from .engine import compute_discount
except ImportError:
    from pricing.discount_rule import DiscountResult, LineItem
    from pricing.engine import compute_discount


# ─────────────────────────────────────────────
# DECISION RECORD (pure Python — no DB dep)
# ─────────────────────────────────────────────

class DecisionRecord:
    """
    One immutable decision record.
    Build from a DiscountResult, then persist via DecisionLogger.
    """

    def __init__(
        self,
        invoice_id:         str,
        line_id:            str,
        item:               LineItem,
        result:             DiscountResult,
        channel:            str            = "all",
        namespace:          str            = "core",
        conflict_strategy:  str            = "best_price",
        created_by:         str            = "system",
    ):
        self.id                      = str(uuid.uuid4())
        self.invoice_id              = invoice_id
        self.line_id                 = line_id
        self.party_id                = item.party_id
        self.product_id              = item.product_id
        self.channel                 = channel
        self.namespace               = namespace
        self.brand_group             = item.brand_group
        self.promo_code_used         = item.promo_code

        # Applied rule
        self.applied_rule_id         = result.rule_applied.id   if result.rule_applied else None
        self.applied_rule_name       = result.rule_applied.name if result.rule_applied else None
        self.applied_rule_type       = result.rule_applied.type.value if result.rule_applied else None

        # All competing rules
        self.competing_rules: List[dict] = self._build_competing(item, result)
        self.rules_evaluated_count   = len(result.evaluated_rules)
        self.conflict_strategy       = conflict_strategy

        # Inputs
        self.base_price              = float(item.base_price)
        self.quantity                = item.quantity
        self.gross_amount            = float(item.gross)

        # Outputs
        self.discount_pct            = float(result.discount_pct)
        self.discount_amount         = float(result.discount_amount)
        self.net_amount              = float(result.net_amount)
        self.gst_rate                = float(result.gst_rate)
        self.gst_amount              = float(result.gst_amount)
        self.final_amount            = float(result.final_amount)

        # Margin
        self.cost_price              = float(item.cost_price) if item.cost_price else None
        self.margin_pct              = float(result.margin_pct) if result.margin_pct is not None else None
        self.margin_status           = result.margin_status

        self.created_at              = datetime.utcnow()
        self.created_by              = created_by

    def _build_competing(self, item: LineItem, result: DiscountResult) -> List[dict]:
        """Build the competing_rules JSONB array."""
        competing = []
        winner_id = result.rule_applied.id if result.rule_applied else None
        for rule in result.evaluated_rules:
            try:
                disc_amt, eff_pct = compute_discount(rule, item)
            except Exception:
                disc_amt, eff_pct = Decimal("0"), Decimal("0")
            competing.append({
                "rule_id":      rule.id,
                "rule_name":    rule.name,
                "rule_type":    rule.type.value,
                "priority":     rule.priority,
                "discount_pct": float(eff_pct),
                "discount_amt": float(disc_amt),
                "was_winner":   rule.id == winner_id,
            })
        return competing

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id":                     self.id,
            "invoice_id":             self.invoice_id,
            "line_id":                self.line_id,
            "party_id":               self.party_id,
            "product_id":             self.product_id,
            "channel":                self.channel,
            "namespace":              self.namespace,
            "brand_group":            self.brand_group,
            "promo_code_used":        self.promo_code_used,
            "applied_rule_id":        self.applied_rule_id,
            "applied_rule_name":      self.applied_rule_name,
            "applied_rule_type":      self.applied_rule_type,
            "competing_rules":        self.competing_rules,
            "rules_evaluated_count":  self.rules_evaluated_count,
            "conflict_strategy":      self.conflict_strategy,
            "base_price":             self.base_price,
            "quantity":               self.quantity,
            "gross_amount":           self.gross_amount,
            "discount_pct":           self.discount_pct,
            "discount_amount":        self.discount_amount,
            "net_amount":             self.net_amount,
            "gst_rate":               self.gst_rate,
            "gst_amount":             self.gst_amount,
            "final_amount":           self.final_amount,
            "cost_price":             self.cost_price,
            "margin_pct":             self.margin_pct,
            "margin_status":          self.margin_status,
            "created_at":             self.created_at.isoformat(),
            "created_by":             self.created_by,
        }


# ─────────────────────────────────────────────
# DECISION LOGGER
# ─────────────────────────────────────────────

class DecisionLogger:
    """
    Writes decision records to the discount_decisions table.

    Design principles:
      1. NEVER raises — billing must not crash due to logging failure
      2. All DB errors are caught and silently logged to self.errors
      3. Works with any psycopg2-compatible connection
      4. Async-friendly: connection is passed in, not owned

    Usage:
      logger = DecisionLogger(db_conn)
      logger.log(invoice_id, line_id, item, result)

      # Retrieve for display / audit:
      decisions = logger.get_invoice_decisions(invoice_id)
    """

    def __init__(self, db_conn=None):
        self.conn   = db_conn
        self.errors: List[str] = []   # Non-fatal error log

    def log(
        self,
        invoice_id:  str,
        line_id:     str,
        item:        LineItem,
        result:      DiscountResult,
        namespace:   str = "core",
        created_by:  str = "system",
    ) -> Optional[str]:
        """
        Write one decision record.
        Returns the decision ID on success, None on failure.
        Never raises.
        """
        record = DecisionRecord(
            invoice_id        = invoice_id,
            line_id           = line_id,
            item              = item,
            result            = result,
            channel           = item.channel.value,
            namespace         = namespace or item.namespace,
            conflict_strategy = (
                result.rule_applied.conflict_strategy.value
                if result.rule_applied else "best_price"
            ),
            created_by        = created_by,
        )

        if self.conn is None:
            # No DB — store in memory (useful for tests / dry-run mode)
            if not hasattr(self, "_memory_log"):
                self._memory_log: List[dict] = []
            self._memory_log.append(record.to_dict())
            return record.id

        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO discount_decisions (
                        id, invoice_id, line_id, party_id, product_id,
                        channel, namespace, brand_group, promo_code_used,
                        applied_rule_id, applied_rule_name, applied_rule_type,
                        competing_rules, rules_evaluated_count, conflict_strategy,
                        base_price, quantity, gross_amount,
                        discount_pct, discount_amount, net_amount,
                        gst_rate, gst_amount, final_amount,
                        cost_price, margin_pct, margin_status,
                        created_at, created_by
                    ) VALUES (
                        %(id)s, %(invoice_id)s, %(line_id)s, %(party_id)s, %(product_id)s,
                        %(channel)s, %(namespace)s, %(brand_group)s, %(promo_code_used)s,
                        %(applied_rule_id)s, %(applied_rule_name)s, %(applied_rule_type)s,
                        %(competing_rules)s, %(rules_evaluated_count)s, %(conflict_strategy)s,
                        %(base_price)s, %(quantity)s, %(gross_amount)s,
                        %(discount_pct)s, %(discount_amount)s, %(net_amount)s,
                        %(gst_rate)s, %(gst_amount)s, %(final_amount)s,
                        %(cost_price)s, %(margin_pct)s, %(margin_status)s,
                        %(created_at)s, %(created_by)s
                    )
                """, {
                    **record.to_dict(),
                    "competing_rules": json.dumps(record.competing_rules),
                    "applied_rule_id": record.applied_rule_id,
                })
                self.conn.commit()
            return record.id

        except Exception as e:
            self.errors.append(f"Decision log failed: {e}")
            try:
                self.conn.rollback()
            except Exception:
                pass
            return None

    def log_invoice(
        self,
        invoice_id:  str,
        items:       List,     # List[LineItem]
        results:     List,     # List[DiscountResult]
        namespace:   str = "core",
        created_by:  str = "system",
    ) -> List[Optional[str]]:
        """Log all lines in an invoice in one call. Returns list of decision IDs."""
        decision_ids = []
        for i, (item, result) in enumerate(zip(items, results)):
            did = self.log(
                invoice_id = invoice_id,
                line_id    = f"{invoice_id}_line_{i+1}",
                item       = item,
                result     = result,
                namespace  = namespace,
                created_by = created_by,
            )
            decision_ids.append(did)
        return decision_ids

    def get_invoice_decisions(self, invoice_id: str) -> List[dict]:
        """Retrieve all decisions for an invoice."""
        if self.conn is None:
            memory = getattr(self, "_memory_log", [])
            return [d for d in memory if d["invoice_id"] == invoice_id]

        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM discount_decisions WHERE invoice_id = %s ORDER BY created_at",
                    (invoice_id,)
                )
                cols = [desc[0] for desc in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception as e:
            self.errors.append(f"get_invoice_decisions failed: {e}")
            return []

    def get_rule_stats(self, rule_id: str) -> dict:
        """
        Analytics: how often a rule fires, avg margin impact, etc.
        Used by the dead-rule detector and effectiveness dashboard.
        """
        if self.conn is None:
            memory = getattr(self, "_memory_log", [])
            fires  = [d for d in memory if d["applied_rule_id"] == rule_id]
            if not fires:
                return {"rule_id": rule_id, "fire_count": 0}
            total_disc = sum(f["discount_amount"] for f in fires)
            avg_margin = sum(f["margin_pct"] for f in fires if f["margin_pct"]) / max(len(fires), 1)
            return {
                "rule_id":          rule_id,
                "fire_count":       len(fires),
                "total_discount":   round(total_disc, 2),
                "avg_margin_pct":   round(avg_margin, 2),
                "hard_stop_count":  sum(1 for f in fires if f["margin_status"] == "hard_stop"),
            }

        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        COUNT(*) AS fire_count,
                        AVG(discount_pct) AS avg_discount_pct,
                        SUM(discount_amount) AS total_discount,
                        AVG(margin_pct) AS avg_margin_pct,
                        COUNT(*) FILTER (WHERE margin_status = 'hard_stop') AS hard_stop_count,
                        MAX(created_at) AS last_fired_at
                    FROM discount_decisions
                    WHERE applied_rule_id = %s
                """, (rule_id,))
                row = cur.fetchone()
                cols = [d[0] for d in cur.description]
                return {"rule_id": rule_id, **dict(zip(cols, row))}
        except Exception as e:
            self.errors.append(f"get_rule_stats failed: {e}")
            return {"rule_id": rule_id, "error": str(e)}
