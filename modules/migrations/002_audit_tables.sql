-- ============================================================
-- DV ERP — Production Patches DDL
-- Run ONCE in your PostgreSQL database before deploying patches
-- ============================================================

-- PATCH 1: Import audit log
-- One row per file import (DRY/SHADOW/LIVE)
CREATE TABLE IF NOT EXISTS loader_import_log (
    import_id    UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    file_name    TEXT,
    file_type    TEXT,                          -- PRODUCT/FRAME/PARTY/etc.
    mode         TEXT,                          -- DRY/SHADOW/LIVE
    stock_mode   TEXT,                          -- ADD/OPENING
    "user"       TEXT,                          -- operator identifier
    imported_at  TIMESTAMPTZ  DEFAULT NOW(),
    rows_total   INT,
    rows_ok      INT,
    rows_skipped INT,
    error_count  INT,
    duration_s   NUMERIC(10,3),
    status       TEXT                           -- OK/PARTIAL/FAILED/DRY
);

CREATE INDEX IF NOT EXISTS idx_import_log_file_type   ON loader_import_log(file_type);
CREATE INDEX IF NOT EXISTS idx_import_log_imported_at ON loader_import_log(imported_at DESC);
CREATE INDEX IF NOT EXISTS idx_import_log_status       ON loader_import_log(status);

-- PATCH 1: Row hash dedup history
-- Stores MD5 of every successfully imported row to prevent double-loading
CREATE TABLE IF NOT EXISTS loader_row_history (
    row_hash     CHAR(32)     PRIMARY KEY,      -- MD5 of sorted row key+values
    import_id    UUID         REFERENCES loader_import_log(import_id) ON DELETE CASCADE,
    file_type    TEXT,
    imported_at  TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_row_history_file_type ON loader_row_history(file_type);
CREATE INDEX IF NOT EXISTS idx_row_history_import_id ON loader_row_history(import_id);

-- ============================================================
-- OPTIONAL: View to get quick stats on recent imports
-- ============================================================
CREATE OR REPLACE VIEW v_import_summary AS
SELECT
    file_type,
    COUNT(*)                                         AS total_imports,
    SUM(CASE WHEN status = 'OK'      THEN 1 ELSE 0 END) AS ok_count,
    SUM(CASE WHEN status = 'PARTIAL' THEN 1 ELSE 0 END) AS partial_count,
    SUM(CASE WHEN status = 'FAILED'  THEN 1 ELSE 0 END) AS failed_count,
    SUM(rows_total)                                  AS total_rows_processed,
    SUM(rows_ok)                                     AS total_rows_ok,
    MAX(imported_at)                                 AS last_import_at
FROM loader_import_log
WHERE status != 'DRY'
GROUP BY file_type
ORDER BY file_type;

-- ============================================================
-- OPTIONAL: Clean up old row hashes (run monthly if needed)
-- Removes hashes older than 90 days — adjust as needed
-- ============================================================
-- DELETE FROM loader_row_history WHERE imported_at < NOW() - INTERVAL '90 days';

-- ============================================================
-- VERIFY (run after DDL)
-- ============================================================
SELECT 'loader_import_log' AS table_name, COUNT(*) AS rows FROM loader_import_log
UNION ALL
SELECT 'loader_row_history',              COUNT(*) FROM loader_row_history;
