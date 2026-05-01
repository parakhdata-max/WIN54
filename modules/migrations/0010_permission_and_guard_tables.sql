-- ============================================================
-- migrations/0010_permission_and_guard_tables.sql
-- ============================================================
-- ALL DDL for the permission + guard system.
-- Run ONCE via migration manager.
-- NEVER run ALTER TABLE or CREATE TABLE in runtime Python.
-- ============================================================

-- ── Permission system ────────────────────────────────────────

CREATE TABLE IF NOT EXISTS role_module_grants (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    role        TEXT        NOT NULL,
    module_key  TEXT        NOT NULL,
    can_view    BOOLEAN     NOT NULL DEFAULT TRUE,
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(role, module_key)
);

CREATE TABLE IF NOT EXISTS role_action_grants (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    role        TEXT        NOT NULL,
    module_key  TEXT        NOT NULL,
    action_key  TEXT        NOT NULL,
    granted     BOOLEAN     NOT NULL DEFAULT TRUE,
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(role, module_key, action_key)
);

CREATE TABLE IF NOT EXISTS user_module_grants (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID        NOT NULL,
    module_key  TEXT        NOT NULL,
    can_view    BOOLEAN,
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, module_key)
);

CREATE TABLE IF NOT EXISTS user_action_grants (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID        NOT NULL,
    module_key  TEXT        NOT NULL,
    action_key  TEXT        NOT NULL,
    granted     BOOLEAN,
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, module_key, action_key)
);

CREATE TABLE IF NOT EXISTS user_acting_roles (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID        NOT NULL,
    acting_role     TEXT        NOT NULL,
    granted_by      UUID,
    granted_reason  TEXT,
    expires_at      TIMESTAMPTZ NOT NULL,
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS permission_override_log (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID,
    username        TEXT,
    action_type     TEXT        NOT NULL,
    order_no        TEXT,
    original_val    TEXT,
    new_val         TEXT,
    reason          TEXT        NOT NULL,
    reason_note     TEXT,
    approved_by     TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS permission_settings (
    key         TEXT        PRIMARY KEY,
    value       TEXT        NOT NULL,
    updated_by  TEXT,
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Default discount threshold (20%)
INSERT INTO permission_settings (key, value, updated_by)
VALUES ('discount_approval_threshold', '20', 'system')
ON CONFLICT (key) DO NOTHING;

-- ── Backoffice touch flag ────────────────────────────────────

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS bo_opened_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS bo_opened_by  TEXT;

-- ── Soft delete columns on orders ───────────────────────────
-- (order_lines already has is_deleted in most installs)

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS is_deleted    BOOLEAN     DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS deleted_at    TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS deleted_by    TEXT,
    ADD COLUMN IF NOT EXISTS delete_reason TEXT;

-- ── ARC backstep audit log ───────────────────────────────────

CREATE TABLE IF NOT EXISTS arc_backstep_log (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id              UUID        NOT NULL,
    order_id            UUID,
    order_line_id       UUID,
    eye_side            CHAR(1),
    from_stage          TEXT        NOT NULL,
    to_stage            TEXT        NOT NULL DEFAULT 'REVERSED',
    vendor_id           UUID,
    vendor_name         TEXT,
    vendor_contact      TEXT,
    po_id               INTEGER,
    po_ref              TEXT,
    reason              TEXT        NOT NULL,
    notified_wa         BOOLEAN     DEFAULT FALSE,
    performed_by        UUID,
    performed_by_name   TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_arc_log_job_id
    ON arc_backstep_log (job_id);

CREATE INDEX IF NOT EXISTS idx_arc_log_vendor_id
    ON arc_backstep_log (vendor_id)
    WHERE vendor_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_arc_log_created
    ON arc_backstep_log (created_at DESC);

-- ── Indexes for permission lookups (performance) ─────────────

CREATE INDEX IF NOT EXISTS idx_rmg_role
    ON role_module_grants (role);

CREATE INDEX IF NOT EXISTS idx_rag_role
    ON role_action_grants (role);

CREATE INDEX IF NOT EXISTS idx_umg_user
    ON user_module_grants (user_id);

CREATE INDEX IF NOT EXISTS idx_uag_user
    ON user_action_grants (user_id);

CREATE INDEX IF NOT EXISTS idx_uar_user_active
    ON user_acting_roles (user_id, is_active)
    WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_pol_created
    ON permission_override_log (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_orders_bo_opened
    ON orders (bo_opened_at)
    WHERE bo_opened_at IS NOT NULL;
