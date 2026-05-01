"""
modules/loaders/channel_detector.py
=====================================
Online / Channel Ingestion Readiness — DV ERP

Detects channel-specific columns in uploaded Excel files so the loader
can surface them in the UI without breaking standard imports.

Prepares the system for:
  - Amazon / Flipkart pricing columns
  - Shopify SKU / price fields
  - D2C portal channel pricing
  - Multi-channel inventory flags

These fields are OPTIONAL — their presence is detected and logged,
but absence never causes an import failure.
"""

from typing import List
import logging

logger = logging.getLogger(__name__)

# ── Known online / channel field names (post _normalize_header) ──────────────
# These are the canonical forms after strip/lower/no-spaces normalization.
OPTIONAL_CHANNEL_FIELDS = [
    "onlineprice",
    "amazonprice",
    "flipkartprice",
    "shopifyprice",
    "channelsku",
    "channelid",
    "channelstock",
    "b2bprice",
    "d2cprice",
    "portalsku",
    "marketplaceprice",
    "webprice",
]

# Human-readable label for each field (used in UI display)
CHANNEL_FIELD_LABELS = {
    "onlineprice":      "Online Price",
    "amazonprice":      "Amazon Price",
    "flipkartprice":    "Flipkart Price",
    "shopifyprice":     "Shopify Price",
    "channelsku":       "Channel SKU",
    "channelid":        "Channel ID",
    "channelstock":     "Channel Stock",
    "b2bprice":         "B2B Price",
    "d2cprice":         "D2C Price",
    "portalsku":        "Portal SKU",
    "marketplaceprice": "Marketplace Price",
    "webprice":         "Web Price",
}


def detect_channel_columns(df) -> List[str]:
    """
    Returns a list of channel/online column names found in the DataFrame.

    Input df should have already passed through _normalize_header()
    so columns are stripped, lowercased, and have no spaces.

    Example:
        cols = detect_channel_columns(df)
        # → ["amazonprice", "channelsku"]
    """
    found = [c for c in df.columns if c in OPTIONAL_CHANNEL_FIELDS]

    if found:
        logger.info(f"[CHANNEL] Detected channel fields: {found}")

    return found


def get_channel_summary(df) -> dict:
    """
    Returns a summary dict of detected channel fields with their
    human-readable labels and non-null counts.

    Useful for displaying in loader UI after upload.
    """
    found = detect_channel_columns(df)

    summary = {}
    for col in found:
        non_null = int(df[col].notna().sum())
        summary[col] = {
            "label":    CHANNEL_FIELD_LABELS.get(col, col),
            "non_null": non_null,
            "total":    len(df),
        }

    return summary


def log_channel_columns_to_import(import_id: str, channel_cols: List[str]) -> None:
    """
    Optionally logs detected channel columns to the audit table.
    Silent on failure — channel detection must never block an import.

    Requires loader_import_log to have a `channel_fields` TEXT column.
    Run this migration if needed:
        ALTER TABLE loader_import_log
        ADD COLUMN IF NOT EXISTS channel_fields TEXT;
    """
    if not channel_cols:
        return

    try:
        from modules.sql_adapter import run_write
        run_write(
            """
            UPDATE loader_import_log
            SET channel_fields = %s
            WHERE import_id = %s
            """,
            (",".join(channel_cols), import_id),
        )
    except Exception as e:
        logger.warning(f"[CHANNEL] Could not log channel fields: {e}")
