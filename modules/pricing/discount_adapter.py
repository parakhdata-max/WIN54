"""
optical_discount_engine/core/discount_adapter.py

Database adapter for the Optical Discount Engine.
Handles all DB read/write operations for discount rules.

Compatible with:
    - psycopg2 (direct)
    - psycopg2 connection pool
    - SQLAlchemy engine (pass conn = engine.connect())

Usage:
    from core.discount_adapter import DiscountAdapter

    adapter = DiscountAdapter(conn)
    rules   = adapter.get_active_rules()
    engine  = DiscountEngine(rules)
    result  = engine.calculate(item)
    adapter.log_application(invoice_id, line_id, item, result)
"""

from __future__ import annotations
import json
import uuid
from datetime import datetime
from decimal import Decimal
from typing import List, Optional, Dict, Any

try:
    from ..models.discount_rule import DiscountRule, DiscountResult, LineItem
except ImportError:
    from models.discount_rule import DiscountRule, DiscountResult, LineItem


# ─────────────────────────────────────────────
# ADAPTER
# ─────────────────────────────────────────────

class DiscountAdapter:
    """
    PostgreSQL adapter for discount rules.

    Pass a psycopg2 connection (or compatible) on init.
    All methods auto-handle dict_cursor style or tuple results.
    """

    def __init__(self, conn):
        """
        Args:
            conn: psycopg2 connection OR SQLAlchemy connection
        """
        self.conn = conn

    # ── INTERNAL ────────────────────────────────────────

    def _execute(self, sql: str, params=None) -> list:
        """Execute a query and return rows as list of dicts."""
        with self.conn.cursor() as cur:
            cur.execute(sql, params or ())
            if cur.description:
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
            return []

    def _execute_one(self, sql: str, params=None) -> Optional[dict]:
        rows = self._execute(sql, params)
        return rows[0] if rows else None

    def _execute_write(self, sql: str, params=None) -> Optional[dict]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params or ())
            if cur.description:
                cols = [d[0] for d in cur.description]
                row  = cur.fetchone()
                return dict(zip(cols, row)) if row else None
        self.conn.commit()
        return None

    # ── READ OPERATIONS ──────────────────────────────────

    def get_active_rules(self, rule_type: Optional[str] = None) -> List[DiscountRule]:
        """
        Fetch all active discount rules from DB.
        Optionally filter by type (party, product, offer_slab, etc.)
        """
        sql = """
            SELECT
                id::text, name, description, type, priority,
                value_type, value, special_price,
                bogo_buy, bogo_get,
                slab_config::text as slab_config,
                gst_rate,
                conditions::text as conditions,
                active, created_at
            FROM discount_rules
            WHERE active = TRUE
        """
        params = []
        if rule_type:
            sql    += " AND type = %s"
            params.append(rule_type)

        sql += " ORDER BY priority ASC, name ASC"
        rows = self._execute(sql, params)

        rules = []
        for row in rows:
            # Parse JSONB fields (returned as string from psycopg2)
            if row.get("slab_config") and isinstance(row["slab_config"], str):
                row["slab_config"] = json.loads(row["slab_config"])
            if row.get("conditions") and isinstance(row["conditions"], str):
                row["conditions"] = json.loads(row["conditions"])
            rules.append(DiscountRule.from_dict(row))

        return rules

    def get_rule_by_id(self, rule_id: str) -> Optional[DiscountRule]:
        """Fetch a single rule by ID."""
        row = self._execute_one(
            """
            SELECT id::text, name, description, type, priority,
                   value_type, value, special_price, bogo_buy, bogo_get,
                   slab_config::text, gst_rate, conditions::text, active
            FROM discount_rules WHERE id = %s
            """,
            (rule_id,)
        )
        if not row:
            return None
        if row.get("slab_config") and isinstance(row["slab_config"], str):
            row["slab_config"] = json.loads(row["slab_config"])
        if row.get("conditions") and isinstance(row["conditions"], str):
            row["conditions"] = json.loads(row["conditions"])
        return DiscountRule.from_dict(row)

    def list_all_rules(self, include_inactive: bool = False) -> List[dict]:
        """
        Return all rules as raw dicts — for admin/listing views.
        """
        sql = "SELECT * FROM v_active_discount_rules"
        if include_inactive:
            sql = """
                SELECT id::text, name, type, priority, value_type, gst_rate, active, created_at
                FROM discount_rules ORDER BY priority, name
            """
        return self._execute(sql)

    # ── WRITE OPERATIONS ─────────────────────────────────

    def create_rule(self, rule_data: dict, created_by: str = "system") -> str:
        """
        Insert a new discount rule.
        Returns the new rule's UUID as string.
        """
        new_id = str(uuid.uuid4())
        sql = """
            INSERT INTO discount_rules (
                id, name, description, type, priority,
                value_type, value, special_price,
                bogo_buy, bogo_get, slab_config,
                gst_rate, conditions, active, created_by
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s::jsonb,
                %s, %s::jsonb, %s, %s
            )
        """
        self._execute_write(sql, (
            new_id,
            rule_data["name"],
            rule_data.get("description", ""),
            rule_data["type"],
            rule_data.get("priority", 3),
            rule_data["value_type"],
            rule_data.get("value"),
            rule_data.get("special_price"),
            rule_data.get("bogo_buy"),
            rule_data.get("bogo_get"),
            json.dumps(rule_data.get("slab_config")) if rule_data.get("slab_config") else None,
            rule_data.get("gst_rate", 12),
            json.dumps(rule_data.get("conditions", {})),
            rule_data.get("active", True),
            created_by,
        ))
        self.conn.commit()

        # Audit
        self._audit(new_id, "created", None, rule_data, created_by)
        return new_id

    def update_rule(self, rule_id: str, updates: dict, updated_by: str = "system"):
        """
        Update fields on an existing rule.
        Only updates provided keys.
        """
        old_rule = self.get_rule_by_id(rule_id)
        if not old_rule:
            raise ValueError(f"Rule {rule_id} not found")

        allowed_fields = {
            "name", "description", "type", "priority", "value_type",
            "value", "special_price", "bogo_buy", "bogo_get",
            "slab_config", "gst_rate", "conditions", "active"
        }
        set_clauses = []
        params      = []

        for field, val in updates.items():
            if field not in allowed_fields:
                continue
            if field in ("slab_config", "conditions"):
                set_clauses.append(f"{field} = %s::jsonb")
                params.append(json.dumps(val))
            else:
                set_clauses.append(f"{field} = %s")
                params.append(val)

        if not set_clauses:
            return

        params.append(rule_id)
        sql = f"UPDATE discount_rules SET {', '.join(set_clauses)} WHERE id = %s"
        self._execute_write(sql, params)
        self.conn.commit()
        self._audit(rule_id, "updated", vars(old_rule), updates, updated_by)

    def deactivate_rule(self, rule_id: str, by: str = "system"):
        """Soft-delete: set active = FALSE."""
        self.update_rule(rule_id, {"active": False}, updated_by=by)
        self._audit(rule_id, "deactivated", None, {"active": False}, by)

    # ── LOGGING ─────────────────────────────────────────

    def log_application(
        self,
        invoice_id:      Optional[str],
        invoice_line_id: Optional[str],
        item:            LineItem,
        result:          DiscountResult,
        applied_by:      str = "system"
    ) -> str:
        """
        Log a discount application to the audit trail.
        Call this after applying discount to an invoice line.
        """
        log_id = str(uuid.uuid4())
        sql = """
            INSERT INTO discount_applications (
                id, invoice_id, invoice_line_id,
                rule_id, rule_name, rule_type,
                base_price, quantity, gross_amount,
                discount_amount, net_amount,
                gst_rate, gst_amount, final_amount,
                applied_by
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        self._execute_write(sql, (
            log_id,
            invoice_id,
            invoice_line_id,
            result.rule_applied.id if result.rule_applied else None,
            result.rule_name,
            result.rule_applied.type.value if result.rule_applied else "none",
            float(result.base_price),
            result.quantity,
            float(result.gross_amount),
            float(result.discount_amount),
            float(result.net_amount),
            float(result.gst_rate),
            float(result.gst_amount),
            float(result.final_amount),
            applied_by,
        ))
        self.conn.commit()
        return log_id

    def get_discount_history(self, invoice_id: str) -> List[dict]:
        """Fetch all discount applications for an invoice."""
        return self._execute(
            "SELECT * FROM discount_applications WHERE invoice_id = %s ORDER BY applied_at",
            (invoice_id,)
        )

    # ── PRIVATE ─────────────────────────────────────────

    def _audit(self, rule_id: str, action: str, old: Any, new: Any, by: str):
        try:
            self._execute_write(
                """
                INSERT INTO discount_rule_audit (rule_id, action, changed_by, old_data, new_data)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb)
                """,
                (
                    rule_id, action, by,
                    json.dumps(old, default=str) if old else None,
                    json.dumps(new, default=str) if new else None,
                )
            )
            self.conn.commit()
        except Exception:
            pass  # Audit failure should not break main flow


# ─────────────────────────────────────────────
# QUICK CONNECT HELPER
# ─────────────────────────────────────────────

def get_adapter(database_url: str) -> DiscountAdapter:
    """
    Convenience factory: create adapter from a DATABASE_URL.

    Usage:
        adapter = get_adapter("postgresql://user:pass@localhost/mydb")
    """
    try:
        import psycopg2
        conn = psycopg2.connect(database_url)
        return DiscountAdapter(conn)
    except ImportError:
        raise ImportError(
            "psycopg2 not installed. Run: pip install psycopg2-binary"
        )
