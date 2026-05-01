"""
modules/analytics/change_analytics.py
=======================================
Analytics queries over the field_change_log audit table.

v2 — Full Business Dashboard:
  - Risk trends (safe / caution / warning over time)
  - Time-series daily change counts
  - Field volatility (most-changed fields with risk)
  - Product instability scoring
  - Business insight layer
  - Anomaly detection
  - Undo commit wrapper

All functions return [] / {} / 0 on failure — callers degrade gracefully.
"""

from typing import List, Dict, Optional


# ══════════════════════════════════════════════════════════════════════════════
# CORE COUNTS
# ══════════════════════════════════════════════════════════════════════════════

def get_change_summary(days: int = 30) -> List[Dict]:
    """Most changed fields in last N days, with risk level."""
    return _q("""
        SELECT field_name,
               COUNT(*)                                        AS change_count,
               MAX(risk_level)                                 AS highest_risk,
               COUNT(*) FILTER (WHERE risk_level='WARNING')    AS warning_count,
               COUNT(*) FILTER (WHERE risk_level='CAUTION')    AS caution_count,
               COUNT(*) FILTER (WHERE risk_level='SAFE')       AS safe_count
        FROM   field_change_log
        WHERE  changed_at >= NOW() - INTERVAL %s
        GROUP  BY field_name
        ORDER  BY change_count DESC
        LIMIT  20
    """, (f"{days} days",))


def get_top_records(days: int = 30, limit: int = 10) -> List[Dict]:
    """Records with most field changes — product instability indicator."""
    return _q("""
        SELECT entity_key                                          AS record_key,
               file_type,
               COUNT(*)                                            AS changes,
               COUNT(*) FILTER (WHERE risk_level='WARNING')        AS warnings,
               MAX(changed_at)                                     AS last_changed,
               COUNT(DISTINCT field_name)                          AS fields_affected
        FROM   field_change_log
        WHERE  changed_at >= NOW() - INTERVAL %s
        GROUP  BY entity_key, file_type
        ORDER  BY changes DESC
        LIMIT  %s
    """, (f"{days} days", limit))


def get_risk_distribution(days: int = 30) -> List[Dict]:
    """Count of changes by risk level."""
    return _q("""
        SELECT risk_level, COUNT(*) AS cnt
        FROM   field_change_log
        WHERE  changed_at >= NOW() - INTERVAL %s
        GROUP  BY risk_level
        ORDER  BY cnt DESC
    """, (f"{days} days",))


def get_recent_activity(limit: int = 50) -> List[Dict]:
    """Most recent field changes across all file types."""
    return _q("""
        SELECT changed_at, file_type, entity_key, field_name,
               old_value, new_value, risk_level, changed_by
        FROM   field_change_log
        ORDER  BY changed_at DESC
        LIMIT  %s
    """, (limit,))


def get_file_type_activity(days: int = 30) -> List[Dict]:
    """Change count per file type — shows which areas are most active."""
    return _q("""
        SELECT file_type,
               COUNT(*) AS changes,
               COUNT(*) FILTER (WHERE risk_level='WARNING') AS risky
        FROM   field_change_log
        WHERE  changed_at >= NOW() - INTERVAL %s
        GROUP  BY file_type
        ORDER  BY changes DESC
    """, (f"{days} days",))


# ══════════════════════════════════════════════════════════════════════════════
# RISK TREND (TIME SERIES)
# ══════════════════════════════════════════════════════════════════════════════

def get_risk_trend(days: int = 30) -> List[Dict]:
    """
    Daily breakdown: safe / caution / warning counts.
    Returns rows: {date, safe, caution, warning, total}
    Use for a stacked area/bar chart.
    """
    return _q("""
        SELECT
            DATE(changed_at)                                        AS date,
            COUNT(*) FILTER (WHERE risk_level = 'SAFE')             AS safe,
            COUNT(*) FILTER (WHERE risk_level = 'CAUTION')          AS caution,
            COUNT(*) FILTER (WHERE risk_level = 'WARNING')          AS warning,
            COUNT(*)                                                 AS total
        FROM   field_change_log
        WHERE  changed_at >= NOW() - INTERVAL %s
        GROUP  BY DATE(changed_at)
        ORDER  BY date ASC
    """, (f"{days} days",))


def get_time_series(days: int = 30) -> List[Dict]:
    """Daily total changes + cumulative for sparkline chart."""
    return _q("""
        SELECT
            DATE(changed_at)    AS date,
            COUNT(*)            AS changes,
            SUM(COUNT(*)) OVER (ORDER BY DATE(changed_at)) AS cumulative
        FROM   field_change_log
        WHERE  changed_at >= NOW() - INTERVAL %s
        GROUP  BY DATE(changed_at)
        ORDER  BY date ASC
    """, (f"{days} days",))


def get_hourly_pattern(days: int = 7) -> List[Dict]:
    """Hour-of-day activity — when are most changes made?"""
    return _q("""
        SELECT
            EXTRACT(HOUR FROM changed_at)::int AS hour,
            COUNT(*)                            AS changes
        FROM   field_change_log
        WHERE  changed_at >= NOW() - INTERVAL %s
        GROUP  BY EXTRACT(HOUR FROM changed_at)
        ORDER  BY hour ASC
    """, (f"{days} days",))


# ══════════════════════════════════════════════════════════════════════════════
# FIELD VOLATILITY
# ══════════════════════════════════════════════════════════════════════════════

def get_field_volatility(days: int = 30, limit: int = 15) -> List[Dict]:
    """
    Which fields change most, and how risky are those changes?
    Returns instability score = (warning*3 + caution*2 + safe*1) / total
    """
    return _q("""
        SELECT
            field_name,
            file_type,
            COUNT(*)                                               AS total_changes,
            COUNT(*) FILTER (WHERE risk_level='WARNING')           AS warning_count,
            COUNT(*) FILTER (WHERE risk_level='CAUTION')           AS caution_count,
            COUNT(*) FILTER (WHERE risk_level='SAFE')              AS safe_count,
            COUNT(DISTINCT entity_key)                             AS records_affected,
            ROUND(
                (
                  COUNT(*) FILTER (WHERE risk_level='WARNING') * 3.0 +
                  COUNT(*) FILTER (WHERE risk_level='CAUTION') * 2.0 +
                  COUNT(*) FILTER (WHERE risk_level='SAFE')    * 1.0
                ) / GREATEST(COUNT(*), 1),
                2
            )                                                      AS volatility_score
        FROM   field_change_log
        WHERE  changed_at >= NOW() - INTERVAL %s
        GROUP  BY field_name, file_type
        ORDER  BY total_changes DESC, volatility_score DESC
        LIMIT  %s
    """, (f"{days} days", limit))


def get_field_history(field_name: str, days: int = 90) -> List[Dict]:
    """Full history for one specific field — for drill-down."""
    return _q("""
        SELECT changed_at, entity_key, file_type,
               old_value, new_value, risk_level, changed_by
        FROM   field_change_log
        WHERE  field_name = %s
          AND  changed_at >= NOW() - INTERVAL %s
        ORDER  BY changed_at DESC
        LIMIT  200
    """, (field_name, f"{days} days"))


# ══════════════════════════════════════════════════════════════════════════════
# PRODUCT / RECORD INSTABILITY
# ══════════════════════════════════════════════════════════════════════════════

def get_product_instability(days: int = 30, limit: int = 10) -> List[Dict]:
    """
    Products (or records) with highest instability.
    Instability = unique fields changed + weighted risk score.
    These are the records needing audit attention.
    """
    return _q("""
        SELECT
            entity_key,
            file_type,
            COUNT(DISTINCT field_name)                              AS fields_changed,
            COUNT(*)                                                AS total_changes,
            COUNT(*) FILTER (WHERE risk_level='WARNING')            AS high_risk_changes,
            MAX(changed_at)                                         AS last_modified,
            TO_CHAR(MAX(changed_at), 'DD-MM-YYYY HH24:MI')         AS last_modified_display,
            COUNT(DISTINCT DATE(changed_at))                        AS active_days,
            ROUND(
                COUNT(*) FILTER (WHERE risk_level='WARNING') * 3.0 +
                COUNT(*) FILTER (WHERE risk_level='CAUTION') * 2.0 +
                COUNT(*) FILTER (WHERE risk_level='SAFE')    * 1.0,
                1
            )                                                       AS instability_score
        FROM   field_change_log
        WHERE  changed_at >= NOW() - INTERVAL %s
        GROUP  BY entity_key, file_type
        HAVING COUNT(*) >= 2
        ORDER  BY instability_score DESC, total_changes DESC
        LIMIT  %s
    """, (f"{days} days", limit))


def get_user_activity(days: int = 30) -> List[Dict]:
    """Who made most changes — user accountability."""
    return _q("""
        SELECT
            changed_by,
            COUNT(*)                                               AS changes,
            COUNT(*) FILTER (WHERE risk_level='WARNING')           AS risky_changes,
            COUNT(DISTINCT entity_key)                             AS records_touched,
            MAX(changed_at)                                        AS last_active
        FROM   field_change_log
        WHERE  changed_at >= NOW() - INTERVAL %s
        GROUP  BY changed_by
        ORDER  BY changes DESC
    """, (f"{days} days",))


# ══════════════════════════════════════════════════════════════════════════════
# BUSINESS INSIGHTS
# ══════════════════════════════════════════════════════════════════════════════

def get_business_insights(days: int = 30) -> Dict:
    """
    High-level business insight layer.
    Returns a dict of named insights, each with value + status + message.
    Status: 'good' | 'warn' | 'alert'
    """
    insights = {}

    # 1. Price stability — how often are prices changing?
    price_changes = _q("""
        SELECT COUNT(*) AS cnt
        FROM   field_change_log
        WHERE  changed_at >= NOW() - INTERVAL %s
          AND  field_name IN ('mrp', 'selling_price', 'purchase_rate')
    """, (f"{days} days",))
    price_cnt = price_changes[0]["cnt"] if price_changes else 0
    insights["price_stability"] = {
        "label":   "Price Changes",
        "value":   price_cnt,
        "unit":    f"in last {days}d",
        "status":  "alert" if price_cnt > 50 else "warn" if price_cnt > 20 else "good",
        "message": (
            f"⚠️ {price_cnt} price changes detected — review rate card"
            if price_cnt > 20 else
            f"✅ {price_cnt} price change(s) — normal activity"
        ),
    }

    # 2. Product activation changes — products being disabled
    deactivated = _q("""
        SELECT COUNT(*) AS cnt
        FROM   field_change_log
        WHERE  changed_at >= NOW() - INTERVAL %s
          AND  field_name  = 'is_active'
          AND  new_value   = 'no'
    """, (f"{days} days",))
    deact_cnt = deactivated[0]["cnt"] if deactivated else 0
    insights["deactivations"] = {
        "label":   "Products Deactivated",
        "value":   deact_cnt,
        "unit":    f"in last {days}d",
        "status":  "alert" if deact_cnt > 10 else "warn" if deact_cnt > 3 else "good",
        "message": (
            f"🔴 {deact_cnt} products deactivated — verify stock clearing"
            if deact_cnt > 3 else
            f"✅ {deact_cnt} deactivation(s)"
        ),
    }

    # 3. High-risk change ratio
    total_row = _q("""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE risk_level='WARNING') AS warnings
        FROM   field_change_log
        WHERE  changed_at >= NOW() - INTERVAL %s
    """, (f"{days} days",))
    if total_row and total_row[0]["total"] > 0:
        total   = total_row[0]["total"]
        warn    = total_row[0]["warnings"]
        ratio   = round(warn / total * 100, 1) if total else 0
        insights["risk_ratio"] = {
            "label":   "High-Risk Ratio",
            "value":   f"{ratio}%",
            "unit":    "of all changes",
            "status":  "alert" if ratio > 20 else "warn" if ratio > 10 else "good",
            "message": (
                f"🔴 {ratio}% of changes are high-risk — consider role-based approval"
                if ratio > 10 else
                f"✅ Only {ratio}% high-risk changes"
            ),
        }

    # 4. GST / HSN changes — compliance risk
    compliance_changes = _q("""
        SELECT COUNT(*) AS cnt
        FROM   field_change_log
        WHERE  changed_at >= NOW() - INTERVAL %s
          AND  field_name IN ('gst_percent', 'hsn_code', 'gst_rate')
    """, (f"{days} days",))
    comp_cnt = compliance_changes[0]["cnt"] if compliance_changes else 0
    insights["compliance"] = {
        "label":   "GST/HSN Changes",
        "value":   comp_cnt,
        "unit":    f"in last {days}d",
        "status":  "alert" if comp_cnt > 0 else "good",
        "message": (
            f"⚠️ {comp_cnt} GST/HSN field(s) changed — verify compliance"
            if comp_cnt > 0 else
            "✅ No GST/HSN changes"
        ),
    }

    return insights


def get_commit_history(days: int = 30, limit: int = 20) -> List[Dict]:
    """History of commits (grouped by import_id for undo reference)."""
    return _q("""
        SELECT
            import_id,
            file_type,
            MIN(changed_at)                                         AS committed_at,
            TO_CHAR(MIN(changed_at), 'DD-MM-YYYY HH24:MI')         AS committed_display,
            MIN(changed_by)                                         AS committed_by,
            COUNT(*)                                                AS field_changes,
            COUNT(DISTINCT entity_key)                              AS records_changed,
            COUNT(*) FILTER (WHERE risk_level='WARNING')            AS warning_count
        FROM   field_change_log
        WHERE  changed_at >= NOW() - INTERVAL %s
          AND  import_id IS NOT NULL
        GROUP  BY import_id, file_type
        ORDER  BY committed_at DESC
        LIMIT  %s
    """, (f"{days} days", limit))


# ══════════════════════════════════════════════════════════════════════════════
# ANOMALY DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def get_anomalies(days: int = 7) -> List[Dict]:
    """
    Detect suspicious patterns:
      - Same record changed > 5 times in window
      - Same field changed > 10 times in window
      - Same user made > 100 changes in one day
      - High-risk fields changed outside business hours
    """
    suspicious = []

    # Frequently modified records
    hot_records = _q("""
        SELECT entity_key, COUNT(*) AS cnt, MAX(file_type) AS file_type
        FROM   field_change_log
        WHERE  changed_at >= NOW() - INTERVAL %s
        GROUP  BY entity_key
        HAVING COUNT(*) > 5
        ORDER  BY cnt DESC
        LIMIT  20
    """, (f"{days} days",))

    for r in hot_records:
        suspicious.append({
            "type":     "🔁 Frequent record edits",
            "subject":  r["entity_key"],
            "count":    r["cnt"],
            "note":     f"Edited {r['cnt']} times in {days} day(s) — verify intent",
            "severity": "warn",
        })

    # High-frequency fields
    hot_fields = _q("""
        SELECT field_name, COUNT(*) AS cnt, MAX(risk_level) AS risk
        FROM   field_change_log
        WHERE  changed_at >= NOW() - INTERVAL %s
        GROUP  BY field_name
        HAVING COUNT(*) > 10
        ORDER  BY cnt DESC
        LIMIT  10
    """, (f"{days} days",))

    for r in hot_fields:
        suspicious.append({
            "type":     "⚡ High-frequency field",
            "subject":  r["field_name"],
            "count":    r["cnt"],
            "note":     f"Changed {r['cnt']} times in {days} day(s) — risk: {r['risk']}",
            "severity": "alert" if r["risk"] == "WARNING" else "warn",
        })

    # Bulk changes in single session (>50 changes, single user, single day)
    bulk_sessions = _q("""
        SELECT changed_by, DATE(changed_at) AS day, COUNT(*) AS cnt
        FROM   field_change_log
        WHERE  changed_at >= NOW() - INTERVAL %s
        GROUP  BY changed_by, DATE(changed_at)
        HAVING COUNT(*) > 50
        ORDER  BY cnt DESC
        LIMIT  5
    """, (f"{days} days",))

    for r in bulk_sessions:
        suspicious.append({
            "type":     "📦 Bulk edit session",
            "subject":  f"{r['changed_by']} on {r['day']}",
            "count":    r["cnt"],
            "note":     f"{r['cnt']} changes in one day — normal for data loads, review if unexpected",
            "severity": "info",
        })

    return suspicious


# ══════════════════════════════════════════════════════════════════════════════
# UNDO COMMIT
# ══════════════════════════════════════════════════════════════════════════════

def undo_commit(backup_id: str, user: str = "system") -> Dict:
    """
    Wrapper around change_approver.rollback_by_backup_id.
    Returns {success, reverted, errors}.
    """
    try:
        from modules.loaders.smart.change_approver import rollback_by_backup_id
        result = rollback_by_backup_id(backup_id, user=user)
        return {
            "success":  result.success,
            "reverted": result.applied,
            "errors":   result.errors,
        }
    except Exception as e:
        return {"success": False, "reverted": 0, "errors": [str(e)]}


# ══════════════════════════════════════════════════════════════════════════════
# ERROR REPORT BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def build_error_report(report) -> "pd.DataFrame":
    """
    Build a downloadable Excel-ready DataFrame from a ChangeReport.
    Includes changes, blocked, errors, schema suggestions, critical errors.
    """
    import pandas as pd
    rows = []

    for c in getattr(report, "changes", []):
        rows.append({
            "Type":      "Change",
            "Record":    c.entity_key,
            "Field":     c.field_name,
            "DB Value":  c.old_value,
            "New Value": c.new_value,
            "Risk":      c.risk_level,
            "Message":   "",
        })

    for b in getattr(report, "blocked", []):
        rows.append({
            "Type":      "Blocked",
            "Record":    b.entity_key,
            "Field":     b.field_name,
            "DB Value":  b.old_value,
            "New Value": b.new_value,
            "Risk":      "BLOCKED",
            "Message":   "Locked field — cannot be changed",
        })

    for e in getattr(report, "errors", []):
        rows.append({"Type": "Error", "Record": "", "Field": "", "DB Value": "",
                     "New Value": "", "Risk": "", "Message": e})

    for s in getattr(report, "schema_suggestions", []):
        rows.append({"Type": "Schema", "Record": "", "Field": "", "DB Value": "",
                     "New Value": "", "Risk": "", "Message": s})

    for ce in getattr(report, "critical_errors", []):
        rows.append({"Type": "CRITICAL", "Record": "", "Field": "", "DB Value": "",
                     "New Value": "", "Risk": "BLOCKED", "Message": ce})

    return pd.DataFrame(rows) if rows else pd.DataFrame()


# ══════════════════════════════════════════════════════════════════════════════
# INTERNAL
# ══════════════════════════════════════════════════════════════════════════════

def _q(sql: str, params: tuple = ()) -> List[Dict]:
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params) or []
    except Exception:
        return []
