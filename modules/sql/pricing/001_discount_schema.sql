-- ============================================================
-- OPTICAL DISCOUNT ENGINE — PostgreSQL Schema
-- Version: 1.0  |  Compatible: PostgreSQL 13+
-- ============================================================

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ─────────────────────────────────────────────
-- DISCOUNT RULES TABLE
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS discount_rules (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    description     TEXT,

    -- Rule classification
    type            TEXT NOT NULL CHECK (type IN (
                        'party',        -- Party/customer wise discount
                        'product',      -- Product category wise
                        'special',      -- Special price override
                        'offer_bogo',   -- Buy X Get Y Free
                        'offer_slab',   -- Quantity slab discount
                        'coating'       -- Coating upgrade offer
                    )),

    -- Priority (lower = higher priority, fires first in best-wins)
    -- 1=Special Price, 2=Fixed, 3=%, 4=Offers
    priority        INT NOT NULL DEFAULT 3,

    -- Discount value
    value_type      TEXT NOT NULL CHECK (value_type IN ('percent', 'fixed', 'special_price', 'bogo')),
    value           NUMERIC(10,4),          -- % or ₹ amount
    special_price   NUMERIC(12,2),          -- Used when value_type = special_price

    -- BOGO config (e.g., Buy 10 Get 1 Free)
    bogo_buy        INT,
    bogo_get        INT,

    -- Slab config (stored as JSONB array of {min_qty, max_qty, discount_pct})
    slab_config     JSONB,

    -- Applicability conditions
    conditions      JSONB NOT NULL DEFAULT '{}',
    -- Conditions schema:
    -- {
    --   "party_ids":     ["uuid1", "uuid2"],    -- specific parties
    --   "party_tags":    ["wholesale", "vip"],  -- party category tags
    --   "product_ids":   ["uuid1"],             -- specific products
    --   "product_cats":  ["frame", "lens"],     -- product categories
    --   "min_qty":       10,
    --   "max_qty":       null,
    --   "min_amount":    500,
    --   "valid_from":    "2025-01-01",
    --   "valid_to":      "2025-12-31"
    -- }

    -- GST rate (applied AFTER discount)
    gst_rate        NUMERIC(5,2) NOT NULL DEFAULT 12.00,

    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────
-- DISCOUNT RULE AUDIT LOG
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS discount_rule_audit (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_id         UUID REFERENCES discount_rules(id),
    action          TEXT NOT NULL CHECK (action IN ('created','updated','deactivated','applied')),
    changed_by      TEXT,
    old_data        JSONB,
    new_data        JSONB,
    note            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────
-- DISCOUNT APPLICATIONS LOG (per invoice line)
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS discount_applications (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id      UUID,               -- FK to your invoices table
    invoice_line_id UUID,               -- FK to your invoice_lines table
    rule_id         UUID REFERENCES discount_rules(id),
    rule_name       TEXT NOT NULL,
    rule_type       TEXT NOT NULL,

    base_price      NUMERIC(12,2) NOT NULL,
    quantity        INT NOT NULL,
    gross_amount    NUMERIC(12,2) NOT NULL,
    discount_amount NUMERIC(12,2) NOT NULL,
    net_amount      NUMERIC(12,2) NOT NULL,
    gst_rate        NUMERIC(5,2) NOT NULL,
    gst_amount      NUMERIC(12,2) NOT NULL,
    final_amount    NUMERIC(12,2) NOT NULL,

    applied_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_by      TEXT
);

-- ─────────────────────────────────────────────
-- INDEXES
-- ─────────────────────────────────────────────
CREATE INDEX idx_discount_rules_active   ON discount_rules(active, type, priority);
CREATE INDEX idx_discount_rules_type     ON discount_rules(type) WHERE active = TRUE;
CREATE INDEX idx_discount_apps_invoice   ON discount_applications(invoice_id);
CREATE INDEX idx_discount_apps_rule      ON discount_applications(rule_id);

-- ─────────────────────────────────────────────
-- UPDATE TRIGGER
-- ─────────────────────────────────────────────
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_discount_rules_updated
    BEFORE UPDATE ON discount_rules
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ─────────────────────────────────────────────
-- SEED: Sample optical discount rules
-- ─────────────────────────────────────────────
INSERT INTO discount_rules (name, description, type, priority, value_type, value, gst_rate, conditions) VALUES
(
    'Standard Party Discount — 12%',
    'Default 12% discount for all registered wholesale parties',
    'party', 3, 'percent', 12.00, 12.00,
    '{"party_tags": ["wholesale"]}'
),
(
    'VIP Party Discount — 18%',
    'Premium 18% discount for VIP/gold tier parties',
    'party', 2, 'percent', 18.00, 12.00,
    '{"party_tags": ["vip", "gold"]}'
),
(
    'AR Coating Upgrade Offer',
    'Special discount on AR lens coating upgrade',
    'coating', 3, 'percent', 20.00, 18.00,
    '{"product_cats": ["ar_coating"]}'
),
(
    'Bulk Frame Buy — Slab Discount',
    '5% on 10+, 10% on 25+, 15% on 50+ frames',
    'offer_slab', 4, 'percent', 0, 12.00,
    '{"product_cats": ["frame"]}',
    -- slab_config set separately
    NULL
),
(
    'Buy 10 Get 1 Free — Contact Lens',
    'BOGO: Purchase 10 boxes, get 1 free',
    'offer_bogo', 4, 'bogo', NULL, 18.00,
    '{"product_cats": ["contact_lens"]}'
);

-- Fix the slab config for the slab rule
UPDATE discount_rules
SET slab_config = '[
    {"min_qty": 10, "max_qty": 24, "discount_pct": 5},
    {"min_qty": 25, "max_qty": 49, "discount_pct": 10},
    {"min_qty": 50, "max_qty": null, "discount_pct": 15}
]'::jsonb,
    bogo_buy = NULL, bogo_get = NULL
WHERE name = 'Bulk Frame Buy — Slab Discount';

UPDATE discount_rules
SET bogo_buy = 10, bogo_get = 1
WHERE name = 'Buy 10 Get 1 Free — Contact Lens';

-- ─────────────────────────────────────────────
-- HELPER VIEW: Active rules summary
-- ─────────────────────────────────────────────
CREATE OR REPLACE VIEW v_active_discount_rules AS
SELECT
    id,
    name,
    type,
    priority,
    value_type,
    CASE
        WHEN value_type = 'percent'       THEN value::TEXT || '%'
        WHEN value_type = 'fixed'         THEN '₹' || value::TEXT
        WHEN value_type = 'special_price' THEN 'SP: ₹' || special_price::TEXT
        WHEN value_type = 'bogo'          THEN 'Buy ' || bogo_buy || ' Get ' || bogo_get || ' Free'
        ELSE 'Slab'
    END AS discount_display,
    gst_rate,
    conditions,
    slab_config,
    created_at
FROM discount_rules
WHERE active = TRUE
ORDER BY priority, type, name;

COMMENT ON TABLE discount_rules IS 'Core discount rules for optical billing engine';
COMMENT ON TABLE discount_applications IS 'Audit trail of every discount applied to invoice lines';
