"""
Batch & Stock Management Module - PRICING REMOVED
====================================================
✅ CRITICAL CHANGES:
- All pricing logic removed and moved to pricing_engine.py
- resolve_price() function DELETED
- normalize_to_pcs_price() function DELETED
- create_allocation_record() NO LONGER returns 'price' field
- Stock UI display NO LONGER shows prices

📌 NEW SEPARATION OF CONCERNS:
- batch_manager.py = STOCK + BATCH ALLOCATION ONLY
- pricing_engine.py = PRICE CALCULATION ONLY

CRITICAL FIX: Eye side 'B' (Both) now properly matches L and R searches
CRITICAL FIX: UUID product_id comparison fixed
"""

import pandas as pd
import numpy as np
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# Your existing imports (keep them as is)
from modules.sql_adapter import (
    read_product_batch, 
    read_ophthalmic_stock, 
    read_solution_batch,
    read_frame_sku,
    read_product_master
)

# ============================================================================
# PRODUCT TYPE DETECTION - UUID FIXED
# ============================================================================

def get_product_type(product_id) -> str:
    """Determine product type based on main_group - UUID compatible"""
    products_df = read_product_master()
    
    if products_df.empty:
        return 'unknown'
    
    # ✅ FIXED: Convert both to string for UUID comparison
    products_df['product_id'] = products_df['product_id'].astype(str)
    product_id_str = str(product_id)
    
    product = products_df[products_df['product_id'] == product_id_str]
    
    if product.empty:
        return 'unknown'
    
    main_group = str(product.iloc[0].get('main_group', '')).lower()
    
    if 'contact' in main_group or 'cl' in main_group:
        return 'contact_lens'
    elif 'ophthalmic' in main_group or 'lens' in main_group:
        return 'ophthalmic_lens'
    elif 'solution' in main_group or 'cleaner' in main_group:
        return 'solution'
    elif 'frame' in main_group or 'sunglass' in main_group:
        return 'frame'
    else:
        return 'unknown'


# ============================================================================
# CRITICAL FIX: Eye Side Matching
# ============================================================================

def eye_side_matches(batch_eye, requested_eye) -> bool:
    """
    Check if batch eye side matches requested eye side
    
    Rules:
    - 'B' (Both) matches both 'L' and 'R'
    - 'L' only matches 'L' (and 'B')
    - 'R' only matches 'R' (and 'B')
    
    CRITICAL FIX: Handles None, NaN, and type conversions properly
    """
    # Handle None and NaN
    if batch_eye is None or requested_eye is None:
        return False
    
    # Handle pandas NaN
    if pd.isna(batch_eye) or pd.isna(requested_eye):
        return False
    
    # Convert to string and clean
    try:
        batch_eye_clean = str(batch_eye).strip().upper()
        requested_eye_clean = str(requested_eye).strip().upper()
    except:
        return False
    
    # Empty strings don't match
    if not batch_eye_clean or not requested_eye_clean:
        return False
    
    # 'B' (Both) matches everything
    if batch_eye_clean == 'B':
        return True
    
    # Otherwise, must be exact match
    return batch_eye_clean == requested_eye_clean



# ─────────────────────────────────────────────────────────────────────────────
# PRICE NORMALISER — applied to every stock function result
#
# Three tiers, always populated:
#   mrp           → retail counter / sticker price  (GST-inclusive)
#   selling_price → W/S trade price
#   purchase_rate → cost / inward price
#
# Fallback chains:
#   mrp           = mrp OR selling_price OR purchase_rate OR 0
#   selling_price = selling_price OR mrp OR purchase_rate OR 0
#   purchase_rate = purchase_rate OR cost_price OR 0
# ─────────────────────────────────────────────────────────────────────────────

def _normalise_prices(df: pd.DataFrame) -> pd.DataFrame:
    """
    Ensure mrp / selling_price / purchase_rate are always present and non-zero
    (where data exists) in any stock DataFrame before it leaves batch_manager.

    Call at the end of every get_*_stock() function.
    """
    if df.empty:
        return df

    def _col(name):
        """Return column as numeric series, or zeros if missing."""
        if name in df.columns:
            return pd.to_numeric(df[name], errors='coerce').fillna(0.0)
        return pd.Series(0.0, index=df.index)

    sp  = _col('selling_price')
    mrp = _col('mrp')
    pr  = _col('purchase_rate')
    cp  = _col('cost_price')      # legacy alias

    # purchase_rate: prefer purchase_rate → cost_price → 0
    df['purchase_rate'] = pr.where(pr > 0, cp)

    # selling_price: prefer selling_price → mrp → purchase_rate → 0
    df['selling_price'] = sp.where(sp > 0, mrp.where(mrp > 0, df['purchase_rate']))

    # mrp: prefer mrp → selling_price → purchase_rate → 0
    df['mrp'] = mrp.where(mrp > 0, df['selling_price'].where(df['selling_price'] > 0, df['purchase_rate']))

    return df


def get_contact_lens_stock(product_id,  # ✅ REMOVED int type hint
                          sph: float = None, 
                          cyl: float = None, 
                          axis: float = None, 
                          add_power: float = None, 
                          eye_side: str = None) -> pd.DataFrame:
    """
    Get contact lens stock from inventory_stock table
    FIXED: UUID product_id handling + toric lenses (CYL/AXIS)
    """
    batch_df = read_ophthalmic_stock()
    
    if batch_df.empty:
        print("DEBUG: inventory_stock table is empty")
        return pd.DataFrame()
    
    print(f"\n{'='*80}")
    print(f"DEBUG get_contact_lens_stock: Total rows in inventory_stock: {len(batch_df)}")
    print(f"DEBUG: Searching for product_id={product_id} (type: {type(product_id)})")
    print(f"DEBUG: Power requested: SPH={sph}, CYL={cyl}, AXIS={axis}, ADD={add_power}, EYE={eye_side}")
    
    # ✅ CRITICAL FIX: Convert product_id column to string for comparison
    print(f"DEBUG: product_id column type before: {batch_df['product_id'].dtype}")
    batch_df['product_id'] = batch_df['product_id'].astype(str)
    product_id_str = str(product_id)
    print(f"DEBUG: Comparing with product_id_str={product_id_str}")
    
    # ================= NORMALIZE NUMERIC COLUMNS =================
    num_cols = ['sph', 'cyl', 'axis', 'add_power', 'quantity']

    for col in num_cols:
        if col in batch_df.columns:
            # Remove spaces
            batch_df[col] = batch_df[col].astype(str).str.strip()
            # Replace empty strings
            batch_df[col] = batch_df[col].replace(['', 'None', 'nan', 'NaN'], None)
            # Convert to numeric — also handles decimal.Decimal from PostgreSQL
            batch_df[col] = pd.to_numeric(batch_df[col], errors='coerce').astype('float64')
    
    # Show what product_ids exist
    available_products = batch_df['product_id'].unique()
    print(f"DEBUG: Available product_ids in batch table (first 5): {list(available_products[:5])}")
    
    # ✅ FIX: Clean eye_side column — 'NONE' string and empty both treated as B (any eye)
    if 'eye_side' in batch_df.columns:
        batch_df['eye_side'] = batch_df['eye_side'].astype(str).str.strip().str.upper()
        batch_df['eye_side'] = batch_df['eye_side'].replace({'': 'B', 'NONE': 'B', 'NAN': 'B', 'NAT': 'B'})
        print(f"DEBUG: Unique eye_side values: {batch_df['eye_side'].unique()}")
    
    # ================= DEBUG AFTER NORMALIZATION =================
    print("\nDEBUG: After normalization (sample 5 rows):")
    print(
        batch_df[
            ['product_id', 'sph', 'cyl', 'axis', 'add_power', 'eye_side', 'quantity']
        ]
        .head(5)
        .to_string()
    )

    # ✅ FIXED: Use string comparison for product_id
    product_rows = batch_df[batch_df['product_id'] == product_id_str]

    print("\nDEBUG: Available powers for this product:")
    print("SPH :", sorted(product_rows['sph'].dropna().unique()) if not product_rows.empty else "[]")
    print("CYL :", sorted(product_rows['cyl'].dropna().unique()) if not product_rows.empty else "[]")
    print("AXIS:", sorted(product_rows['axis'].dropna().unique()) if not product_rows.empty else "[]")
    print("EYE :", list(product_rows['eye_side'].dropna().unique()) if not product_rows.empty else "[]")

    # ✅ FIXED: Filter by product using string comparison
    mask = (batch_df['product_id'] == product_id_str) & (batch_df['quantity'] > 0)

    # Base filtered dataset (always defined)
    filtered = batch_df[mask].copy()

    print(f"DEBUG: After product_id filter: {mask.sum()} rows")
    
    if mask.sum() == 0:
        print(f"❌ ERROR: No batches found for product_id={product_id_str}")
        print(f"   Available product_ids (first 10): {list(available_products[:10])}")
        return pd.DataFrame()

    # ================= DETECT LENS DESIGN (FIXED) =================

    # Check only rows matching SPH first
    design_check = filtered.copy()

    if sph is not None:
        _sph_f = float(sph)  # ensure float, not Decimal
        design_check = design_check[
            np.isclose(design_check['sph'].astype(float), _sph_f, atol=0.01)
        ]

    has_toric = (
        design_check['cyl'].notna().any() and
        design_check['axis'].notna().any()
    )

    has_add = design_check['add_power'].notna().any()

    if has_toric:
        lens_design = "TORIC"
    elif has_add:
        lens_design = "MULTIFOCAL"
    else:
        lens_design = "SPHERICAL"

    print(f"DEBUG: Lens design detected = {lens_design}")

    # ================= SAMPLE DEBUG =================

    print("\nDEBUG: Sample after product filter:")
    print(
        filtered[['id','sph','cyl','axis','add_power','eye_side','quantity']]
        .head(5)
        .to_string()
    )

    # ================= DESIGN ISOLATION =================

    # TORIC
    if cyl not in (None, 0):
        # For STOCK lenses: axis may be NULL in DB (stocked without specific axis)
        # For RX lenses: axis must match

        print("DEBUG: Enforcing TORIC rows only")

        filtered = filtered[filtered['cyl'].notna()]

        # Only require axis in DB if the DB row actually has an axis set
        # Stock SV lenses stored without axis → match any axis punched by user
        # (axis filter applied below but only when DB row has axis)

    # SPHERICAL — match rows where cyl is NULL or 0, and axis is NULL or 0
    elif cyl in (None, 0):

        print("DEBUG: Enforcing SPH-only rows")

        filtered = filtered[
            (filtered['cyl'].isna() | (filtered['cyl'] == 0.0)) &
            (filtered['axis'].isna() | (filtered['axis'] == 0.0) | (filtered['axis'] == 0))
        ]

    # MULTIFOCAL
    if add_power not in (None, 0):

        print("DEBUG: Enforcing MULTIFOCAL rows")

        filtered = filtered[
            filtered['add_power'].notna()
        ]


    # ================= POWER MATCH =================

    # SPH
    if sph is not None:
        filtered = filtered[
            np.isclose(filtered['sph'].astype(float), float(sph), atol=0.01)
        ]

    # CYL
    if cyl not in (None, 0):
        filtered = filtered[
            np.isclose(filtered['cyl'].astype(float), float(cyl), atol=0.01)
        ]

    # AXIS — only filter by axis if the DB rows have axis set (RX stock)
    # Stock SV lenses have NULL axis → match any axis the user enters
    if axis not in (None, 0):
        _has_axis_in_db = filtered['axis'].notna().any()
        if _has_axis_in_db:
            # Some rows have axis (RX-specific) — filter to matching axis
            # Rows with NULL axis (stock lenses) remain available to any axis
            filtered = filtered[
                filtered['axis'].isna() |
                np.isclose(filtered['axis'].fillna(-9999), float(axis), atol=1)
            ]
        # else: all rows have NULL axis (pure stock) — no axis filtering needed

    # ADD
    if add_power not in (None, 0):
        filtered = filtered[
            np.isclose(filtered['add_power'].astype(float), float(add_power), atol=0.01)
        ]


    print("DEBUG: Final matched rows =", len(filtered))
    print(filtered[['sph','cyl','axis','add_power','quantity']].head())

    if filtered.empty:
        return pd.DataFrame()


    # ================= EYE SIDE FILTER =================
    
    if eye_side and eye_side.upper() in ['L', 'R']:
        print(f"\nDEBUG: Applying eye_side={eye_side} filter...")
        before = len(filtered)
        
        eye_side_upper = str(eye_side).strip().upper()
        
        # Apply eye side matching using the fixed function
        eye_match_mask = filtered.apply(
            lambda row: eye_side_matches(row['eye_side'], eye_side_upper), 
            axis=1
        )
        
        filtered = filtered[eye_match_mask]
        
        print(f"   Before: {before} rows, After: {len(filtered)} rows")
        
        if filtered.empty:
            available_eyes = batch_df[batch_df['product_id'] == product_id_str]['eye_side'].dropna().unique()
            print(f"   ❌ No matches for eye_side={eye_side}")
            print(f"   Available eye_side values: {list(available_eyes)}")
            return pd.DataFrame()


    # ================= FINAL RESULT =================

    print(f"\n✅ FINAL MATCHED BATCHES: {len(filtered)}")
    if not filtered.empty:
        print(
            filtered[['id','batch_no','sph','cyl','axis','eye_side','quantity']]
            .head(10)
            .to_string()
        )
        
        # Add required columns
        filtered['source'] = 'contact_lens'
        filtered['available_qty'] = filtered['quantity']

        # ── Normalise all three price tiers ───────────────────────────────
        filtered = _normalise_prices(filtered)

        if 'expiry_date' in filtered.columns:
            filtered['expiry_date'] = pd.to_datetime(filtered['expiry_date'], errors='coerce')

        print(f"✅ Total quantity: {filtered['available_qty'].sum()}")
    else:
        print("❌ NO MATCHES FOUND after all filters")
    
    print(f"{'='*80}\n")

    return filtered


# ============================================================================
# REST OF THE FUNCTIONS
# ============================================================================

def get_ophthalmic_lens_stock(product_id,  # ✅ REMOVED int type hint
                              sph: float = None, 
                              cyl: float = None, 
                              axis: float = None, 
                              add_power: float = None, 
                              eye_side: str = None,
                              coating: str = None) -> pd.DataFrame:
    """Get ophthalmic lens stock - FIXED eye_side matching and UUID"""
    stock_df = read_ophthalmic_stock()
    
    if stock_df.empty:
        return pd.DataFrame()
    
    # ✅ FIXED: Convert product_id to string
    stock_df['product_id'] = stock_df['product_id'].astype(str)
    product_id_str = str(product_id)
    
    # Convert numeric columns
    for col in ['sph', 'cyl', 'axis', 'add_power', 'quantity']:
        if col in stock_df.columns:
            stock_df[col] = pd.to_numeric(stock_df[col], errors='coerce')
    
    # Filter by product
    mask = (stock_df['product_id'] == product_id_str) & (stock_df['quantity'] > 0)
    
    # Apply power filters — NULL means "not specified", not "matches anything"
    # SPH: only match rows where SPH is explicitly set to the entered value
    if sph is not None:
        sph_mask = stock_df['sph'].notna() & np.isclose(stock_df['sph'], float(sph), atol=0.01)
        mask &= sph_mask

    # CYL: no CYL or 0 → spherical → match null/0 only
    #       specific CYL → exact match only (spherical stock ≠ toric)
    if cyl is None or (cyl is not None and abs(float(cyl)) < 0.01):
        cyl_mask = stock_df['cyl'].isna() | np.isclose(stock_df['cyl'].fillna(0), 0, atol=0.01)
        mask &= cyl_mask
    else:
        cyl_mask = stock_df['cyl'].notna() & np.isclose(stock_df['cyl'], float(cyl), atol=0.01)
        mask &= cyl_mask

    # AXIS: stock lenses with NULL axis fit any axis — always match
    #        only filter when stock has a specific axis value
    if axis is not None and abs(float(axis)) > 0:
        axis_mask = stock_df['axis'].isna() | np.isclose(stock_df['axis'].fillna(0), float(axis), atol=5.0)
        mask &= axis_mask

    # ADD power
    if add_power is not None and abs(float(add_power)) > 0.01:
        add_mask = stock_df['add_power'].notna() & np.isclose(stock_df['add_power'], float(add_power), atol=0.01)
        mask &= add_mask
    
    # ✅ FIXED: Eye side filter using new function
    if eye_side and eye_side.upper() in ['L', 'R']:
        eye_mask = stock_df.apply(
            lambda row: eye_side_matches(row['eye_side'], eye_side), 
            axis=1
        )
        mask &= eye_mask

    # Coating filter — only filter when coating is specified
    if coating and 'coating' in stock_df.columns:
        coat_mask = stock_df['coating'].fillna('').str.lower() == coating.lower()
        mask &= coat_mask

    result = stock_df[mask].copy()
    
    if not result.empty:
        result['source'] = 'ophthalmic_lens'
        result['available_qty'] = result['quantity']
        # ── Normalise all three price tiers ───────────────────────────────
        result = _normalise_prices(result)

    return result


def get_solution_stock(product_id) -> pd.DataFrame:
    """Get solution stock - UUID fixed"""
    
    batch_df = read_solution_batch()
    
    if batch_df.empty:
        return pd.DataFrame()
    
    batch_df['product_id'] = batch_df['product_id'].astype(str)
    product_id_str = str(product_id)
    
    if 'quantity' in batch_df.columns:
        batch_df['quantity'] = pd.to_numeric(batch_df['quantity'], errors='coerce')
    
    mask = (batch_df['product_id'] == product_id_str) & (batch_df['quantity'] > 0)
    
    result = batch_df[mask].copy()
    
    if not result.empty:
        result['source'] = 'solution'
        # qty_available alias (batches table uses qty_available, not quantity)
        if 'available_qty' not in result.columns or result['available_qty'].sum() == 0:
            qty_col = next((c for c in ('qty_available', 'quantity') if c in result.columns), None)
            result['available_qty'] = pd.to_numeric(result[qty_col], errors='coerce').fillna(0) if qty_col else 0
        if 'quantity' not in result.columns:
            result['quantity'] = result['available_qty']
        # ── Normalise all three price tiers ───────────────────────────
        result = _normalise_prices(result)

        if 'expiry_date' in result.columns:
            result['expiry_date'] = pd.to_datetime(result['expiry_date'], errors='coerce')

    return result


def get_frame_stock(product_id) -> pd.DataFrame:  # ✅ REMOVED int type hint
    """Get frame stock - UUID fixed"""
    sku_df = read_frame_sku()
    
    if sku_df.empty:
        return pd.DataFrame()
    
    # ✅ FIXED: Convert product_id to string
    sku_df['product_id'] = sku_df['product_id'].astype(str)
    product_id_str = str(product_id)
    
    if 'quantity' in sku_df.columns:
        sku_df['quantity'] = pd.to_numeric(sku_df['quantity'], errors='coerce')
    
    mask = (sku_df['product_id'] == product_id_str) & (sku_df['quantity'] > 0)
    
    result = sku_df[mask].copy()
    
    if not result.empty:
        result['source'] = 'frame'
        result['available_qty'] = result['quantity']
        # ── Normalise all three price tiers ───────────────────────────
        result = _normalise_prices(result)

    return result


# ============================================================================
# UNIFIED STOCK RETRIEVAL
# ============================================================================

def _force_float_df(df: pd.DataFrame) -> pd.DataFrame:
    """Final safety net — ensure no decimal.Decimal values escape batch_manager."""
    if df.empty:
        return df
    _num_cols = ['mrp', 'selling_price', 'purchase_rate', 'available_qty',
                 'quantity', 'sph', 'cyl', 'axis', 'add_power', 'allocated_qty']
    for _c in _num_cols:
        if _c in df.columns:
            df[_c] = pd.to_numeric(df[_c], errors='coerce').fillna(0.0).astype(float)
    return df


def get_available_stock(product_id,  # ✅ REMOVED int type hint
                       sph: float = None, 
                       cyl: float = None, 
                       axis: float = None, 
                       add_power: float = None, 
                       eye_side: str = None,
                       coating: str = None) -> pd.DataFrame:
    """Get available stock for a product based on its type"""
    product_type = get_product_type(product_id)
    
    if product_type == 'contact_lens':
        return _force_float_df(get_contact_lens_stock(product_id, sph, cyl, axis, add_power, eye_side))
    
    elif product_type == 'ophthalmic_lens':
        return _force_float_df(get_ophthalmic_lens_stock(product_id, sph, cyl, axis, add_power, eye_side, coating=coating))
    
    elif product_type == 'solution':
        return _force_float_df(get_solution_stock(product_id))
    
    elif product_type == 'frame':
        return _force_float_df(get_frame_stock(product_id))
    
    else:
        # Generic fallback — query inventory_stock directly for any other product type
        try:
            from modules.sql_adapter import run_query
            rows = run_query("""
                SELECT id, product_id, batch_no, eye_side, quantity,
                       COALESCE(mrp, selling_price, 0)       AS mrp,
                       COALESCE(selling_price, mrp, 0)       AS selling_price,
                       COALESCE(purchase_rate, 0)            AS purchase_rate,
                       location, updated_at
                FROM inventory_stock
                WHERE product_id::text = %(pid)s
                  AND quantity > 0
                  AND COALESCE(is_active, true) = true
            """, {"pid": str(product_id)})
            if rows:
                result = pd.DataFrame(rows)
                result['source']        = 'generic'
                result['available_qty'] = pd.to_numeric(result['quantity'], errors='coerce').fillna(0)
                result = _normalise_prices(result)
                return result
        except Exception:
            pass
        return pd.DataFrame()


# ============================================================================
# REST OF YOUR EXISTING FUNCTIONS
# ============================================================================

def check_stock_availability(product_id,  # ✅ REMOVED int type hint
                            sph: float = None, 
                            cyl: float = None,
                            axis: float = None, 
                            add_power: float = None, 
                            eye_side: str = None,
                            required_qty: float = 1,
                            coating: str = None) -> Dict:
    """Check if required quantity is available in stock"""
    stock_df = get_available_stock(product_id, sph, cyl, axis, add_power, eye_side, coating=coating)
    
    if stock_df.empty:
        return {
            'available': False,
            'available_qty': 0,
            'required_qty': required_qty,
            'pending_qty': required_qty,
            'source': 'none',
            'product_type': get_product_type(product_id),
            'message': 'No stock available'
        }
    
    total_available = stock_df['available_qty'].sum()
    product_type = stock_df.iloc[0]['source']
    
    if total_available >= required_qty:
        return {
            'available': True,
            'available_qty': total_available,
            'required_qty': required_qty,
            'pending_qty': 0,
            'source': product_type,
            'product_type': product_type,
            'message': f'Stock available: {total_available} units'
        }
    else:
        return {
            'available': True,
            'available_qty': total_available,
            'required_qty': required_qty,
            'pending_qty': required_qty - total_available,
            'source': product_type,
            'product_type': product_type,
            'message': f'Partial stock: {total_available}/{required_qty} available'
        }


def get_batches_fifo(product_id,
                    sph: float = None, 
                    cyl: float = None,
                    axis: float = None, 
                    add_power: float = None,
                    eye_side: str = None) -> pd.DataFrame:
    """Get available batches sorted by FIFO (First In, First Out) for allocation"""

    stock_df = get_available_stock(product_id, sph, cyl, axis, add_power, eye_side)
    
    if stock_df.empty:
        return pd.DataFrame()
    
    product_type = stock_df.iloc[0]['source']
    
    if product_type not in ['contact_lens', 'solution']:
        return stock_df
    
    # FIFO by expiry
    if 'expiry_date' in stock_df.columns:
        stock_df = stock_df.sort_values('expiry_date')
    
    stock_df = stock_df.copy()
    stock_df['allocated_qty'] = 0.0

    return stock_df


def allocate_batches_fifo(batches_df: pd.DataFrame, required_qty: float) -> pd.DataFrame:
    """Allocate quantity across batches using FIFO"""
    if batches_df.empty or required_qty <= 0:
        return batches_df
    
    batches_df = batches_df.copy()
    batches_df['allocated_qty'] = 0.0
    
    remaining_qty = required_qty
    
    for idx, row in batches_df.iterrows():
        available_in_batch = row['available_qty']
        
        if remaining_qty <= 0:
            break
        
        if available_in_batch >= remaining_qty:
            batches_df.at[idx, 'allocated_qty'] = remaining_qty
            remaining_qty = 0
        else:
            batches_df.at[idx, 'allocated_qty'] = available_in_batch
            remaining_qty -= available_in_batch
    
    return batches_df


def get_batch_allocation_summary(batches_df: pd.DataFrame) -> Dict:
    """Get summary of batch allocation"""
    if batches_df.empty or 'allocated_qty' not in batches_df.columns:
        return {
            'allocated_qty': 0,
            'pending_qty': 0,
            'batch_count': 0,
            'batches': []
        }
    
    allocated_qty = batches_df['allocated_qty'].sum()
    
    batch_details = []
    for _, row in batches_df[batches_df['allocated_qty'] > 0].iterrows():
        batch_info = {
            'allocated_qty': row.get('allocated_qty', 0),
            'available_qty': row.get('available_qty', 0),

            # 🔽 RAW PRICE METADATA (NO CALCULATION)
            'selling_price': row.get('selling_price', 0),
            'unit': row.get('unit', 'PCS'),
            'box_size': row.get('box_size', 1),
        }
        
        if row.get('batch_no'):
            batch_info['batch_no'] = row.get('batch_no', '')
            batch_info['batch_id'] = row.get('batch_id')
        
        if row.get('expiry_date'):
            batch_info['expiry_date'] = str(row.get('expiry_date', ''))
        
        if row.get('sph') is not None:
            batch_info['sph'] = row.get('sph')
            batch_info['cyl'] = row.get('cyl')
            batch_info['axis'] = row.get('axis')
            batch_info['add_power'] = row.get('add_power')
            batch_info['eye_side'] = row.get('eye_side')
        
        batch_details.append(batch_info)
    
    return {
        'allocated_qty': allocated_qty,
        'batch_count': len(batch_details),
        'batches': batch_details
    }


def get_stock_display(product_id,  # ✅ REMOVED int type hint
                     sph: float = None, 
                     cyl: float = None,
                     axis: float = None, 
                     add_power: float = None,
                     eye_side: str = None) -> str:
    """Get formatted stock display string for UI"""
    product_type = get_product_type(product_id)
    stock_df = get_available_stock(product_id, sph, cyl, axis, add_power, eye_side)
    
    if stock_df.empty:
        return "📦 Stock: 0 | ⚠️ To Order"
    
    total_qty = stock_df['available_qty'].sum()
    
    if product_type == 'contact_lens':
        batch_count = len(stock_df)
        
        near_expiry = 0
        if 'expiry_date' in stock_df.columns:
            today = pd.Timestamp.now()
            three_months = today + pd.Timedelta(days=90)
            near_expiry = len(stock_df[stock_df['expiry_date'] <= three_months])
        
        status = f"📦 Stock: {int(total_qty)} ({batch_count} batches)"
        if near_expiry > 0:
            status += f" | ⚠️ {near_expiry} near expiry"
        else:
            status += " | ✅ Available"
        
        return status
    
    elif product_type == 'solution':
        batch_count = len(stock_df)
        return f"📦 Stock: {int(total_qty)} ({batch_count} batches) | ✅ Available"
    
    elif product_type == 'ophthalmic_lens':
        return f"📦 Stock: {int(total_qty)} | ✅ Available"
    
    elif product_type == 'frame':
        sku_count = len(stock_df)
        return f"📦 Stock: {int(total_qty)} ({sku_count} SKUs) | ✅ Available"
    
    else:
        return f"📦 Stock: {int(total_qty)} | ✅ Available"


def create_allocation_record(
    product_id,
    sph: float = None, 
    cyl: float = None, 
    axis: float = None,
    add_power: float = None, 
    eye_side: str = None, 
    required_qty: float = 1,
    pricing_mode: str = "RETAIL"   # ✅ DEPRECATED - kept for backward compatibility but not used
) -> Dict:
    """
    Create allocation record for an order line
    
    ✅ PRICING REMOVED: This function NO LONGER returns 'price' field.
    Pricing is now handled exclusively by pricing_engine.py
    
    Returns stock and batch allocation information only:
    - type: product type
    - required_qty, allocated_qty, pending_qty, billing_qty
    - status: READY/PARTIAL/PENDING
    - batches: list of allocated batches
    """
    product_type = get_product_type(product_id)
    
    print(f"\nDEBUG create_allocation_record: product_type={product_type}")
    print(f"DEBUG: product_id={product_id}, sph={sph}, cyl={cyl}, axis={axis}, add={add_power}, eye={eye_side}, qty={required_qty}")
    
    if product_type in ['contact_lens', 'solution']:
        batches_df = get_batches_fifo(product_id, sph, cyl, axis, add_power, eye_side)
        
        print(f"DEBUG: get_batches_fifo returned {len(batches_df)} batches")
        
        batches_df = allocate_batches_fifo(batches_df, required_qty)
        
        allocated_qty = batches_df['allocated_qty'].sum() if not batches_df.empty else 0
        pending_qty = max(required_qty - allocated_qty, 0)
        
        print(f"DEBUG: allocated_qty={allocated_qty}, pending_qty={pending_qty}")
        
        return {
            'type': product_type,
            'required_qty': required_qty,
            'allocated_qty': allocated_qty,
            'pending_qty': pending_qty,
            'billing_qty': allocated_qty,
            'status': 'READY' if pending_qty == 0 else ('PARTIAL' if allocated_qty > 0 else 'PENDING'),
            'batches': get_batch_allocation_summary(batches_df)['batches']
        }
    
    else:
        stock_df = get_available_stock(product_id, sph, cyl, axis, add_power, eye_side)
        available_qty = stock_df['available_qty'].sum() if not stock_df.empty else 0
        
        allocated_qty = min(available_qty, required_qty)
        pending_qty = max(required_qty - allocated_qty, 0)
        
        return {
            'type': product_type,
            'required_qty': required_qty,
            'allocated_qty': allocated_qty,
            'pending_qty': pending_qty,
            'billing_qty': allocated_qty,
            'status': 'READY' if pending_qty == 0 else ('PARTIAL' if allocated_qty > 0 else 'PENDING'),
            'batches': []
        }
