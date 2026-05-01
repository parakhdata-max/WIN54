-- ============================================================
-- OPTICAL DISCOUNT ENGINE — Migration 003 (FINAL LOCKED)
-- Run after: 001_discount_schema.sql + 002_v2_brand_channel_promo.sql
--
-- What this adds:
--   1. discount_decisions   — full audit log of every pricing decision
--   2. margin_config        — per-namespace/channel guardrail thresholds
--   3. pricing_policies     — named policy groups
--   4. ALTER discount_rules — namespace, conflict_strategy, version,
--                             parent_rule_id, conditions_dsl
--   5. Analytics views      — effectiveness, dead rules, brand performance,
--                             promo ROI
-- ============================================================

-- ─────────────────────────────────────────────────────────────
-- 1. DISCOUNT DECISIONS — Full audit log per invoice line
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS discount_decisions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Invoice context
    invoice_id              UUID,
    line_id                 UUID,
    party_id                UUID,
    product_id              UUID,
    channel                 TEXT NOT NULL DEFAULT 'all',
    namespace               TEXT NOT NULL DEFAULT 'core',

    -- What fired
    applied_rule_id         UUID REFERENCES discount_rules(id),
    applied_rule_name       TEXT,
    applied_rule_type       TEXT,

    -- What competed — full list for analytics + AI training
    -- [ {"rule_id":"...", "rule_name":"...", "discount_pct":12.0,
    --    "discount_amt":144.0, "was_winner":true}, ... ]
    competing_rules         JSONB NOT NULL DEFAULT '[]',
    rules_evaluated_count   INT   DEFAULT 0,
    conflict_strategy       TEXT  DEFAULT 'best_price',

    -- Inputs
    base_price              NUMERIC(12,2) NOT NULL,
    quantity                INT NOT NULL,
    gross_amount            NUMERIC(12,2) NOT NULL,
    brand_group             TEXT,
    promo_code_used         TEXT,

    -- Outputs
    discount_pct            NUMERIC(8,4),
    discount_amount         NUMERIC(12,2),
    net_amount              NUMERIC(12,2),
    gst_rate                NUMERIC(5,2),
    gst_amount              NUMERIC(12,2),
    final_amount            NUMERIC(12,2),

    -- Margin at decision time
    cost_price              NUMERIC(12,2),
    margin_pct              NUMERIC(8,4),
    margin_status           TEXT DEFAULT 'ok'
                                CHECK (margin_status IN ('ok','soft_warning','hard_stop')),

    -- For future AI training: was this decision later flagged?
    flagged                 BOOLEAN DEFAULT FALSE,
    flag_reason             TEXT,

    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by              TEXT DEFAULT 'system'
);

CREATE INDEX idx_decisions_invoice    ON discount_decisions(invoice_id);
CREATE INDEX idx_decisions_rule       ON discount_decisions(applied_rule_id);
CREATE INDEX idx_decisions_channel_ns ON discount_decisions(channel, namespace);
CREATE INDEX idx_decisions_margin     ON discount_decisions(margin_status) WHERE margin_status != 'ok';
CREATE INDEX idx_decisions_promo      ON discount_decisions(promo_code_used) WHERE promo_code_used IS NOT NULL;
CREATE INDEX idx_decisions_brand      ON discount_decisions(brand_group)     WHERE brand_group IS NOT NULL;
CREATE INDEX idx_decisions_created    ON discount_decisions(created_at DESC);

-- ─────────────────────────────────────────────────────────────
-- 2. MARGIN CONFIG — Per-namespace guardrail thresholds
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS margin_config (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace       TEXT NOT NULL DEFAULT 'core',
    channel         TEXT NOT NULL DEFAULT 'all',
    product_cat     TEXT,               -- NULL = applies to all categories

    -- Thresholds (percentage points)
    safe_pct        NUMERIC(5,2) NOT NULL DEFAULT 20.00,
    warn_pct        NUMERIC(5,2) NOT NULL DEFAULT 10.00,
    block_pct       NUMERIC(5,2) NOT NULL DEFAULT 5.00,

    -- What to DO when block threshold hit:
    --   warn             = surface status, allow billing to proceed
    --   require_approval = flag for manager approval
    --   hard_block       = raise exception, disallow
    block_action    TEXT NOT NULL DEFAULT 'warn'
                        CHECK (block_action IN ('warn','require_approval','hard_block')),

    note            TEXT,
    active          BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE(namespace, channel, COALESCE(product_cat, ''))
);

-- Seed defaults for optical billing
INSERT INTO margin_config (namespace, channel, safe_pct, warn_pct, block_pct, block_action, note)
VALUES
    ('core',      'all',       20.00, 15.00, 5.00,  'warn',             'Global default'),
    ('wholesale', 'wholesale', 18.00, 12.00, 4.00,  'warn',             'Wholesale: leaner margin ok'),
    ('retail',    'retail',    25.00, 15.00, 8.00,  'require_approval', 'Retail: higher margin expected'),
    ('ecommerce', 'online',    22.00, 15.00, 5.00,  'warn',             'Online: same as core'),
    ('franchise', 'all',       30.00, 20.00, 10.00, 'hard_block',       'Franchise: strict enforcement')
ON CONFLICT DO NOTHING;

-- ─────────────────────────────────────────────────────────────
-- 3. PRICING POLICIES — Named policy groups
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pricing_policies (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                        TEXT NOT NULL UNIQUE,
    namespace                   TEXT NOT NULL DEFAULT 'core',
    channel                     TEXT NOT NULL DEFAULT 'all',
    description                 TEXT,
    margin_config_id            UUID REFERENCES margin_config(id),
    default_conflict_strategy   TEXT NOT NULL DEFAULT 'best_price'
                                    CHECK (default_conflict_strategy IN
                                        ('best_price','highest_priority','stack','margin_safe')),
    max_rules_per_invoice_line  INT DEFAULT 1,
    expose_simulate_api         BOOLEAN NOT NULL DEFAULT TRUE,
    active                      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO pricing_policies (name, namespace, channel, description, default_conflict_strategy)
VALUES
    ('Wholesale Core',  'wholesale', 'wholesale', 'Standard B2B counter billing',          'highest_priority'),
    ('Retail Standard', 'retail',    'retail',    'OTC retail walk-in, offers panel on',   'best_price'),
    ('Online / App',    'ecommerce', 'online',    'App and website checkout',               'best_price'),
    ('Franchise',       'franchise', 'all',       'Franchise outlets — strict guardrails',  'margin_safe')
ON CONFLICT DO NOTHING;

-- ─────────────────────────────────────────────────────────────
-- 4. ALTER discount_rules — add all v3 columns
-- ─────────────────────────────────────────────────────────────

-- Namespace — scopes rule to business context
ALTER TABLE discount_rules
    ADD COLUMN IF NOT EXISTS namespace TEXT NOT NULL DEFAULT 'core';
-- Values: core | retail | wholesale | ecommerce | franchise
-- Rules with namespace='core' are included in ALL policy contexts

-- Conflict strategy — how this rule resolves conflicts at engine level
ALTER TABLE discount_rules
    ADD COLUMN IF NOT EXISTS conflict_strategy TEXT NOT NULL DEFAULT 'best_price'
        CHECK (conflict_strategy IN ('best_price','highest_priority','stack','margin_safe'));

-- Rule versioning — never delete a rule, version it
ALTER TABLE discount_rules
    ADD COLUMN IF NOT EXISTS version         INT  NOT NULL DEFAULT 1;
ALTER TABLE discount_rules
    ADD COLUMN IF NOT EXISTS parent_rule_id  UUID REFERENCES discount_rules(id);
-- parent_rule_id = NULL on original. Set to original rule UUID when editing.
-- To update a rule: INSERT new row with incremented version + parent_rule_id,
--                   then SET active=FALSE on old row.

-- Universal condition DSL — side-by-side with legacy conditions JSON
-- NULL = engine uses legacy conditions column (existing rules unaffected)
-- When set: condition_dsl.py evaluates this instead of hardcoded checks
ALTER TABLE discount_rules
    ADD COLUMN IF NOT EXISTS conditions_dsl  JSONB;
-- DSL format:
-- { "op": "all", "conditions": [
--     {"field": "brand_group",  "op": "in",  "value": ["titan"]},
--     {"field": "channel",      "op": "=",   "value": "retail"},
--     {"field": "party_tags",   "op": "any", "value": ["vip","gold"]},
--     {"field": "quantity",     "op": ">=",  "value": 10},
--     {"field": "promo_code",   "op": "=",   "value": "DIWALI25"}
-- ]}

-- Indexes
CREATE INDEX IF NOT EXISTS idx_discount_rules_namespace  ON discount_rules(namespace, active) WHERE active = TRUE;
CREATE INDEX IF NOT EXISTS idx_discount_rules_version    ON discount_rules(parent_rule_id)    WHERE parent_rule_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_discount_rules_dsl        ON discount_rules USING gin(conditions_dsl) WHERE conditions_dsl IS NOT NULL;

-- ─────────────────────────────────────────────────────────────
-- 5. ANALYTICS VIEWS
-- ─────────────────────────────────────────────────────────────

-- Which rules fire, how often, and what margin they produce
CREATE OR REPLACE VIEW v_rule_effectiveness AS
SELECT
    dd.applied_rule_id                                            AS rule_id,
    dr.name                                                       AS rule_name,
    dr.type                                                       AS rule_type,
    dr.namespace,
    COUNT(*)                                                      AS fire_count,
    ROUND(AVG(dd.discount_pct)::NUMERIC, 2)                      AS avg_discount_pct,
    ROUND(AVG(dd.margin_pct)::NUMERIC, 2)                        AS avg_margin_pct,
    ROUND(SUM(dd.discount_amount)::NUMERIC, 2)                   AS total_discount_given,
    ROUND(SUM(dd.final_amount)::NUMERIC, 2)                      AS total_revenue,
    COUNT(*) FILTER (WHERE dd.margin_status = 'hard_stop')       AS hard_stop_count,
    COUNT(*) FILTER (WHERE dd.margin_status = 'soft_warning')    AS soft_warn_count,
    MAX(dd.created_at)                                            AS last_fired_at
FROM discount_decisions dd
JOIN discount_rules dr ON dr.id = dd.applied_rule_id
WHERE dd.applied_rule_id IS NOT NULL
GROUP BY dd.applied_rule_id, dr.name, dr.type, dr.namespace
ORDER BY fire_count DESC;

-- Rules that haven't fired in 30 days — candidates for cleanup
CREATE OR REPLACE VIEW v_dead_rules AS
SELECT
    dr.id,
    dr.name,
    dr.type,
    dr.namespace,
    dr.active,
    MAX(dd.created_at)   AS last_fired_at,
    COUNT(dd.id)         AS total_lifetime_fires
FROM discount_rules dr
LEFT JOIN discount_decisions dd ON dd.applied_rule_id = dr.id
WHERE dr.active = TRUE
GROUP BY dr.id, dr.name, dr.type, dr.namespace, dr.active
HAVING MAX(dd.created_at) < NOW() - INTERVAL '30 days'
    OR MAX(dd.created_at) IS NULL
ORDER BY total_lifetime_fires ASC;

-- Brand group discount performance
CREATE OR REPLACE VIEW v_brand_performance AS
SELECT
    brand_group,
    channel,
    COUNT(*)                                                      AS line_count,
    ROUND(AVG(discount_pct)::NUMERIC, 2)                         AS avg_discount_pct,
    ROUND(AVG(margin_pct)::NUMERIC, 2)                           AS avg_margin_pct,
    ROUND(SUM(discount_amount)::NUMERIC, 2)                      AS total_discounts,
    ROUND(SUM(final_amount)::NUMERIC, 2)                         AS total_revenue,
    COUNT(*) FILTER (WHERE margin_status = 'hard_stop')          AS hard_stops
FROM discount_decisions
WHERE brand_group IS NOT NULL
GROUP BY brand_group, channel
ORDER BY total_revenue DESC;

-- Promo code usage and ROI
CREATE OR REPLACE VIEW v_promo_effectiveness AS
SELECT
    promo_code_used                                               AS promo_code,
    COUNT(*)                                                      AS total_uses,
    COUNT(DISTINCT invoice_id)                                    AS invoices,
    ROUND(AVG(discount_pct)::NUMERIC, 2)                         AS avg_discount_pct,
    ROUND(SUM(discount_amount)::NUMERIC, 2)                      AS total_discount_cost,
    ROUND(SUM(final_amount)::NUMERIC, 2)                         AS total_revenue,
    ROUND(AVG(margin_pct)::NUMERIC, 2)                           AS avg_margin_pct,
    MIN(created_at)                                               AS first_used,
    MAX(created_at)                                               AS last_used
FROM discount_decisions
WHERE promo_code_used IS NOT NULL
GROUP BY promo_code_used
ORDER BY total_revenue DESC;

-- Channel margin health — quick margin dashboard by channel
CREATE OR REPLACE VIEW v_channel_margin_health AS
SELECT
    channel,
    namespace,
    COUNT(*)                                                      AS decisions,
    ROUND(AVG(margin_pct)::NUMERIC, 2)                           AS avg_margin_pct,
    COUNT(*) FILTER (WHERE margin_status = 'ok')                 AS ok_count,
    COUNT(*) FILTER (WHERE margin_status = 'soft_warning')       AS warn_count,
    COUNT(*) FILTER (WHERE margin_status = 'hard_stop')          AS hard_stop_count,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE margin_status = 'hard_stop') / NULLIF(COUNT(*), 0),
        1
    )                                                             AS hard_stop_rate_pct,
    DATE_TRUNC('day', MAX(created_at))                           AS latest_decision
FROM discount_decisions
WHERE created_at >= NOW() - INTERVAL '90 days'
GROUP BY channel, namespace
ORDER BY hard_stop_rate_pct DESC NULLS LAST;

-- ─────────────────────────────────────────────────────────────
-- COMMENTS
-- ─────────────────────────────────────────────────────────────
COMMENT ON TABLE  discount_decisions                              IS 'Full audit log of every discount decision — analytics + AI training source';
COMMENT ON TABLE  margin_config                                   IS 'Per-namespace/channel margin guardrail thresholds';
COMMENT ON TABLE  pricing_policies                                IS 'Named policy groups linking namespace, channel, and guardrails';
COMMENT ON COLUMN discount_rules.namespace                        IS 'Rule scope: core (all) | retail | wholesale | ecommerce | franchise';
COMMENT ON COLUMN discount_rules.conflict_strategy               IS 'Conflict resolution: best_price | highest_priority | stack | margin_safe';
COMMENT ON COLUMN discount_rules.version                         IS 'Incremented on each edit. Use parent_rule_id to chain versions.';
COMMENT ON COLUMN discount_rules.parent_rule_id                  IS 'UUID of the rule this was versioned from. NULL on original.';
COMMENT ON COLUMN discount_rules.conditions_dsl                  IS 'Universal condition DSL. NULL = use legacy conditions JSON. See condition_dsl.py.';
