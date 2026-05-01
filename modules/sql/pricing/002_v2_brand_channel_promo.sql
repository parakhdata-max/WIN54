-- ============================================================
-- OPTICAL DISCOUNT ENGINE — Migration v2
-- Run this AFTER 001_discount_schema.sql
-- Adds: brand_group, channel, promo_code, stackable, UI fields
-- ============================================================

-- ─────────────────────────────────────────────
-- ALTER existing discount_rules table
-- ─────────────────────────────────────────────

-- Channel: which sales channel this rule applies to
ALTER TABLE discount_rules
    ADD COLUMN IF NOT EXISTS channel TEXT NOT NULL DEFAULT 'all'
        CHECK (channel IN ('wholesale', 'retail', 'online', 'all'));

-- Promo code: code-gated rules (retail / online / app)
ALTER TABLE discount_rules
    ADD COLUMN IF NOT EXISTS promo_code TEXT;

-- Stackable flag (future use)
ALTER TABLE discount_rules
    ADD COLUMN IF NOT EXISTS stackable BOOLEAN NOT NULL DEFAULT FALSE;

-- UI display fields
ALTER TABLE discount_rules
    ADD COLUMN IF NOT EXISTS display_label TEXT DEFAULT '';

ALTER TABLE discount_rules
    ADD COLUMN IF NOT EXISTS icon_emoji TEXT DEFAULT '';

ALTER TABLE discount_rules
    ADD COLUMN IF NOT EXISTS show_in_offers BOOLEAN NOT NULL DEFAULT FALSE;

-- ─────────────────────────────────────────────
-- Extend conditions JSONB to document new fields
-- (JSONB is schemaless — no ALTER needed, just documenting)
-- New condition fields added to JSONB:
--   brand_groups:    ["titan","rayban"]    — links to products.brand_group
--   channel:         "wholesale"|"retail"|"online"|"all"
--   promo_code:      "NEWAPP20"
--   party_whitelist: ["uuid1","uuid2"]     — only these parties
--   party_blacklist: ["uuid1","uuid2"]     — never these parties
-- ─────────────────────────────────────────────

-- ─────────────────────────────────────────────
-- NEW: Promo code usage tracking table
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS promo_code_usage (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_id         UUID REFERENCES discount_rules(id),
    promo_code      TEXT NOT NULL,
    channel         TEXT NOT NULL DEFAULT 'retail',

    -- Who used it
    party_id        UUID,               -- NULL for walk-in retail / online guest
    customer_phone  TEXT,               -- For online/app orders
    invoice_id      UUID,

    discount_amount NUMERIC(12,2),
    used_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_promo_usage_code    ON promo_code_usage(promo_code);
CREATE INDEX idx_promo_usage_party   ON promo_code_usage(party_id);
CREATE INDEX idx_promo_usage_invoice ON promo_code_usage(invoice_id);

-- ─────────────────────────────────────────────
-- NEW: Promo code config table (optional — for max_uses, per_party limits)
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS promo_codes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code            TEXT NOT NULL UNIQUE,
    rule_id         UUID REFERENCES discount_rules(id),
    description     TEXT,
    channel         TEXT NOT NULL DEFAULT 'all'
                        CHECK (channel IN ('wholesale', 'retail', 'online', 'all')),

    -- Usage limits
    max_uses_total  INT,                   -- NULL = unlimited
    max_uses_per_party INT DEFAULT 1,      -- How many times one party can use it
    current_uses    INT NOT NULL DEFAULT 0,

    -- Party restriction (applies to THESE parties only — NULL = anyone)
    party_ids_allowed   JSONB DEFAULT '[]',
    party_tags_allowed  JSONB DEFAULT '[]',

    valid_from      DATE,
    valid_to        DATE,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE promo_codes IS
    'Optional promo code config. If not used, codes are stored directly in discount_rules.conditions JSONB';

-- ─────────────────────────────────────────────
-- INDEXES for new columns
-- ─────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_discount_rules_channel   ON discount_rules(channel) WHERE active = TRUE;
CREATE INDEX IF NOT EXISTS idx_discount_rules_promo     ON discount_rules(promo_code) WHERE promo_code IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_discount_rules_offers    ON discount_rules(show_in_offers) WHERE show_in_offers = TRUE;

-- ─────────────────────────────────────────────
-- SEED: v2 example rules with new fields
-- ─────────────────────────────────────────────

-- New App Download promo (online/retail)
INSERT INTO discount_rules (
    name, description, type, priority, value_type, value,
    gst_rate, conditions, channel, promo_code, show_in_offers,
    display_label, icon_emoji, active
) VALUES (
    'New App Download — 20% off',
    'First purchase after downloading the store app. Use code NEWAPP20.',
    'promo_code', 3, 'percent', 20.00,
    12.00,
    '{"product_cats": [], "channel": "online", "promo_code": "NEWAPP20"}'::jsonb,
    'online',
    'NEWAPP20',
    TRUE,
    'App Download Offer',
    '📱',
    TRUE
);

-- Try First offer (retail only, specific party tags)
INSERT INTO discount_rules (
    name, description, type, priority, value_type, value,
    gst_rate, conditions, channel, promo_code, show_in_offers,
    display_label, icon_emoji, active
) VALUES (
    'Try First — 10% off First Order',
    'New retail customer first order discount. Show promo to billing staff.',
    'promo_code', 3, 'percent', 10.00,
    12.00,
    '{"channel": "retail", "promo_code": "TRYFIRST"}'::jsonb,
    'retail',
    'TRYFIRST',
    TRUE,
    'First Order Offer',
    '🎁',
    TRUE
);

-- Brand group discount: Titan brand frames — wholesale only
INSERT INTO discount_rules (
    name, description, type, priority, value_type, value,
    gst_rate, conditions, channel, show_in_offers,
    display_label, icon_emoji, active
) VALUES (
    'Titan Brand Group — 8% Wholesale',
    'Dedicated 8% discount on all Titan brand products for wholesale accounts.',
    'brand_group', 2, 'percent', 8.00,
    12.00,
    '{"brand_groups": ["titan"], "party_tags": ["wholesale"], "channel": "wholesale"}'::jsonb,
    'wholesale',
    FALSE,
    'Titan 8%',
    '🏷️',
    TRUE
);

-- Diwali festival promo — retail + online, limited validity
INSERT INTO discount_rules (
    name, description, type, priority, value_type, value,
    gst_rate, conditions, channel, promo_code, show_in_offers,
    display_label, icon_emoji, active
) VALUES (
    'Diwali 2025 — 15% off',
    'Festive offer for retail walk-in and online. Code DIWALI25.',
    'promo_code', 3, 'percent', 15.00,
    12.00,
    '{"channel": "all", "promo_code": "DIWALI25", "valid_to": "2025-11-15"}'::jsonb,
    'all',
    'DIWALI25',
    TRUE,
    'Diwali Offer 15%',
    '🪔',
    TRUE
);

-- ─────────────────────────────────────────────
-- UPDATED view: now includes channel + promo
-- ─────────────────────────────────────────────
CREATE OR REPLACE VIEW v_active_discount_rules AS
SELECT
    id,
    name,
    type,
    priority,
    value_type,
    channel,
    promo_code,
    show_in_offers,
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
    stackable,
    created_at
FROM discount_rules
WHERE active = TRUE
ORDER BY priority, channel, type, name;
