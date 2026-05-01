"""
api/simulate_api.py — FINAL LOCKED v3.0
=========================================

Simulator API — exposes the discount engine as a clean POST endpoint.

WHY THIS EXISTS:
  The playground UI is great for humans.
  But mobile billing, WhatsApp billing, AI layer, and franchise portals
  need a JSON API — not a Streamlit UI.

  This module provides:
    simulate_request()   — pure Python function, no web framework needed
    Flask route example  — drop into existing Flask app
    FastAPI route        — if you use FastAPI

WHAT IT DOES:
  POST /pricing/simulate
  Input:  SimulateRequest (JSON)
  Output: SimulateResponse (JSON)

  - Runs full engine pipeline
  - Returns winner + all evaluated rules + margin status
  - Optionally logs the simulation (audit trail)
  - Safe to call from any billing context

INPUT FORMAT:
  {
    "items": [
      {
        "base_price": 1200,
        "quantity": 10,
        "product_cat": "frame",
        "brand_group": "titan",
        "party_tags": ["wholesale"],
        "channel": "wholesale",
        "promo_code": null,
        "cost_price": 700
      }
    ],
    "party_id": "...",
    "channel": "wholesale",
    "namespace": "core",
    "promo_code": null,
    "log_decision": false
  }

OUTPUT FORMAT:
  {
    "status": "ok",
    "channel": "wholesale",
    "namespace": "core",
    "lines": [ ... DiscountResult.to_dict() per line ... ],
    "totals": { gross, total_discount, total_gst, payable, margin_status },
    "simulations": [ ... per-line simulate() output ... ],
    "pipeline": "INPUT → Policy → Engine → GST → Margin → Decision Log"
  }
"""

from __future__ import annotations
from decimal import Decimal
from typing import Any, Dict, List, Optional

try:
    from pricing.discount_rule import (
        DiscountRule, LineItem, SalesChannel, ConflictStrategy
    )
    from pricing.pricing_policy import get_policy, PricingPolicy
    from pricing.decision_logger import DecisionLogger
except ImportError:
    from modules.pricing.discount_rule import (
        DiscountRule, LineItem, SalesChannel, ConflictStrategy
    )
    from modules.pricing.pricing_policy import get_policy, PricingPolicy
    from modules.pricing.decision_logger import DecisionLogger


# ─────────────────────────────────────────────
# REQUEST / RESPONSE MODELS
# ─────────────────────────────────────────────

class SimulateRequest:
    """
    Parsed simulate request.
    Build from a JSON dict: SimulateRequest.from_dict(payload)
    """

    def __init__(
        self,
        items:        List[LineItem],
        channel:      SalesChannel  = SalesChannel.ALL,
        namespace:    str           = "core",
        party_id:     Optional[str] = None,
        party_tags:   List[str]     = None,
        promo_code:   Optional[str] = None,
        log_decision: bool          = False,
        invoice_id:   Optional[str] = None,
    ):
        self.items        = items
        self.channel      = channel
        self.namespace    = namespace
        self.party_id     = party_id
        self.party_tags   = party_tags or []
        self.promo_code   = promo_code
        self.log_decision = log_decision
        self.invoice_id   = invoice_id

    @classmethod
    def from_dict(cls, d: dict) -> "SimulateRequest":
        """Parse a raw JSON payload into a SimulateRequest."""
        try:
            channel = SalesChannel(d.get("channel", "all"))
        except ValueError:
            channel = SalesChannel.ALL

        items = []
        for raw_item in d.get("items", []):
            try:
                ch_val = raw_item.get("channel", d.get("channel", "all"))
                item_ch = SalesChannel(ch_val)
            except ValueError:
                item_ch = channel

            items.append(LineItem(
                base_price  = Decimal(str(raw_item["base_price"])),
                quantity    = int(raw_item.get("quantity", 1)),
                product_id  = raw_item.get("product_id"),
                product_cat = raw_item.get("product_cat"),
                party_id    = raw_item.get("party_id") or d.get("party_id"),
                party_tags  = raw_item.get("party_tags") or d.get("party_tags", []),
                gst_rate    = Decimal(str(raw_item["gst_rate"])) if raw_item.get("gst_rate") else None,
                brand_group = raw_item.get("brand_group"),
                channel     = item_ch,
                promo_code  = raw_item.get("promo_code") or d.get("promo_code"),
                cost_price  = Decimal(str(raw_item["cost_price"])) if raw_item.get("cost_price") else None,
                namespace   = d.get("namespace", "core"),
            ))

        return cls(
            items        = items,
            channel      = channel,
            namespace    = d.get("namespace", "core"),
            party_id     = d.get("party_id"),
            party_tags   = d.get("party_tags", []),
            promo_code   = d.get("promo_code"),
            log_decision = bool(d.get("log_decision", False)),
            invoice_id   = d.get("invoice_id"),
        )


# ─────────────────────────────────────────────
# CORE SIMULATE FUNCTION
# Framework-agnostic — pure Python
# ─────────────────────────────────────────────

def simulate_request(
    request:   SimulateRequest,
    all_rules: List[DiscountRule],
    db_conn:   Any = None,
) -> dict:
    """
    Run a simulate request through the full engine pipeline.
    Returns a JSON-serializable dict.

    Never raises — all errors surface in response["status"] = "error".
    """
    try:
        # Select policy for this context
        policy = get_policy(request.channel, request.namespace)
        engine = policy.build_engine(all_rules)

        # Calculate all lines
        results      = [engine.calculate(item) for item in request.items]
        invoice_data = engine.calculate_invoice(request.items)

        # Per-line simulations (all evaluated rules + margin)
        simulations  = [engine.simulate(item) for item in request.items]

        # Optional: log decisions
        decision_ids: List[Optional[str]] = []
        if request.log_decision and request.invoice_id:
            logger       = DecisionLogger(db_conn)
            decision_ids = logger.log_invoice(
                invoice_id = request.invoice_id,
                items      = request.items,
                results    = results,
                namespace  = request.namespace,
            )

        return {
            "status":       "ok",
            "channel":      request.channel.value,
            "namespace":    request.namespace,
            "policy":       policy.name,
            "lines":        invoice_data["lines"],
            "totals":       invoice_data["totals"],
            "simulations":  simulations,
            "decision_ids": decision_ids,
            "pipeline":     "INPUT → Policy → Engine → GST → Margin[Tiered] → Log",
        }

    except Exception as e:
        return {
            "status":  "error",
            "message": str(e),
            "channel": request.channel.value if request.channel else "unknown",
        }


# ─────────────────────────────────────────────
# FLASK INTEGRATION  (drop-in if you use Flask)
# ─────────────────────────────────────────────

def make_flask_blueprint(all_rules_loader, db_conn_getter=None):
    """
    Returns a Flask Blueprint with POST /pricing/simulate route.

    Usage:
        from api.simulate_api import make_flask_blueprint
        from your_app import get_all_rules, get_db_conn

        bp = make_flask_blueprint(
            all_rules_loader = get_all_rules,
            db_conn_getter   = get_db_conn,
        )
        app.register_blueprint(bp, url_prefix="/api/v1")

    Then: POST /api/v1/pricing/simulate
    """
    try:
        from flask import Blueprint, request as flask_request, jsonify
    except ImportError:
        raise ImportError("Flask not installed. Run: pip install flask")

    bp = Blueprint("pricing", __name__)

    @bp.route("/pricing/simulate", methods=["POST"])
    def simulate():
        payload    = flask_request.get_json(force=True, silent=True) or {}
        all_rules  = all_rules_loader()
        db_conn    = db_conn_getter() if db_conn_getter else None
        req        = SimulateRequest.from_dict(payload)
        response   = simulate_request(req, all_rules, db_conn)
        return jsonify(response), 200 if response["status"] == "ok" else 500

    return bp


# ─────────────────────────────────────────────
# FASTAPI INTEGRATION  (if you use FastAPI)
# ─────────────────────────────────────────────

def make_fastapi_router(all_rules_loader, db_conn_getter=None):
    """
    Returns a FastAPI APIRouter with POST /pricing/simulate route.

    Usage:
        from api.simulate_api import make_fastapi_router
        router = make_fastapi_router(get_all_rules)
        app.include_router(router, prefix="/api/v1")
    """
    try:
        from fastapi import APIRouter
        from fastapi.responses import JSONResponse
    except ImportError:
        raise ImportError("FastAPI not installed. Run: pip install fastapi")

    router = APIRouter()

    @router.post("/pricing/simulate")
    async def simulate(payload: dict):
        all_rules = all_rules_loader()
        db_conn   = db_conn_getter() if db_conn_getter else None
        req       = SimulateRequest.from_dict(payload)
        response  = simulate_request(req, all_rules, db_conn)
        status    = 200 if response["status"] == "ok" else 500
        return JSONResponse(content=response, status_code=status)

    return router


# ─────────────────────────────────────────────
# STANDALONE TEST  (run this file directly)
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, "..")

    from pricing.discount_rule import DiscountRule, RuleType, ValueType, RuleConditions

    # Sample rules for testing
    sample_rules = [
        DiscountRule(
            id="ws-12", name="Wholesale 12%",
            type=RuleType.PARTY, value_type=ValueType.PERCENT,
            value=Decimal("12"), priority=3, gst_rate=Decimal("12"),
            conditions=RuleConditions(party_tags=["wholesale"], channel=SalesChannel.WHOLESALE),
        ),
        DiscountRule(
            id="promo-diwali", name="Diwali 15%",
            type=RuleType.PROMO_CODE, value_type=ValueType.PERCENT,
            value=Decimal("15"), priority=4, gst_rate=Decimal("12"),
            conditions=RuleConditions(promo_code="DIWALI25"),
            show_in_offers=True,
        ),
    ]

    # Test payload
    payload = {
        "channel":   "wholesale",
        "namespace": "core",
        "party_tags": ["wholesale"],
        "items": [
            {
                "base_price":  1200,
                "quantity":    10,
                "product_cat": "frame",
                "brand_group": "titan",
                "cost_price":  700,
            }
        ]
    }

    req      = SimulateRequest.from_dict(payload)
    response = simulate_request(req, sample_rules)

    print("\n── Simulator API Test ──────────────────────────")
    print(f"Status  : {response['status']}")
    print(f"Policy  : {response.get('policy')}")
    print(f"Payable : ₹{response['totals']['payable']:,.2f}")
    print(f"Winner  : {response['lines'][0]['rule_applied']}")
    print(f"Pipeline: {response.get('pipeline')}")
    print("────────────────────────────────────────────────\n")
