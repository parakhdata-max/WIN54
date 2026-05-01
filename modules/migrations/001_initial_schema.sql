-- ============================================================
-- Migration 001 — Initial Schema
-- DV ERP core tables (run before 002)
-- ============================================================

-- Core tables (products, frames, parties, patients,
-- inventory_stock, batches, blank_inventory, patient_visits)
-- are managed by your existing DB schema.
-- This file is a placeholder for future tracked migrations.

-- Feature flags table (required by feature_flags.py)
CREATE TABLE IF NOT EXISTS system_flags (
    key         TEXT PRIMARY KEY,
    value       TEXT        NOT NULL DEFAULT 'true',
    description TEXT,
    updated_at  TIMESTAMP   DEFAULT NOW()
);

INSERT INTO system_flags (key, value, description) VALUES
    ('loader.opening_enabled',   'true',  'Allow OPENING stock reset mode'),
    ('loader.schema_guard',      'true',  'Enable schema diff engine before import'),
    ('loader.preview_required',  'true',  'Require preview approval before LIVE import'),
    ('loader.strict_mode',       'false', 'Block import if unknown column detected'),
    ('loader.ai_advisor',        'true',  'Show AI advisor tips in loader UI')
ON CONFLICT (key) DO NOTHING;

-- Schema history table (used by schema_guard.py)
CREATE TABLE IF NOT EXISTS loader_schema_history (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    file_type      TEXT,
    file_name      TEXT,
    change_summary JSONB,
    approved_by    TEXT DEFAULT 'user',
    approved_at    TIMESTAMP DEFAULT NOW()
);
