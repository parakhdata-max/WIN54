# Intelligent Supplier Order Quantity Calculator
# modules/supplier_order_calculator.py

"""
Smart supplier order quantity calculation with:
1. Customer fulfillment
2. Minimum stock threshold
3. Pending supplier orders tracking
4. Multi-power optimization
"""

import pandas as pd
from typing import Dict, List, Tuple
from datetime import datetime, timedelta


def get_pending_supplier_orders_qty(product_id: str, sph: float, cyl: float = 0, axis: int = None) -> int:
    """
    Get total quantity in pending supplier orders for this exact power
    
    Args:
        product_id: Product UUID
        sph, cyl, axis: Power specification
    
    Returns:
        Total quantity in PENDING supplier orders (not yet received)
    """
    from modules.sql_adapter import fetch_supplier_orders
    
    # Get all pending supplier orders
    pending_orders = fetch_supplier_orders()
    
    if not pending_orders:
        return 0
    
    # Filter for pending status
    pending_statuses = ['SENT', 'ACKNOWLEDGED', 'IN_TRANSIT', 'PARTIALLY_RECEIVED']
    pending_orders = [
        o for o in pending_orders 
        if o.get('status') in pending_statuses
    ]
    
    total_pending = 0
    
    for order in pending_orders:
        items = order.get('items', [])
        
        for item in items:
            # Match product and power
            if (item.get('product_id') == product_id and
                abs(float(item.get('sph_out', 0)) - sph) < 0.01 and
                abs(float(item.get('cyl_out', 0)) - cyl) < 0.01):
                
                # Add pending quantity (ordered - received)
                ordered_qty = item.get('quantity', 0)
                received_qty = item.get('received_qty', 0)
                total_pending += (ordered_qty - received_qty)
    
    return total_pending


def get_minimum_stock_level(product_id: str, sph: float = None, cyl: float = None) -> int:
    """
    Get minimum stock threshold for a product/power
    
    Priority:
    1. Power-specific minimum (from product_stock_levels table)
    2. Product default minimum (from products table)
    3. Global default (20)
    
    TODO: Implement database table for power-specific minimums
    """
    from modules.sql_adapter import read_product_master
    
    # Try to get product-specific minimum
    products_df = read_product_master()
    
    if not products_df.empty:
        product = products_df[products_df['product_id'] == product_id]
        
        if not product.empty:
            min_stock = product.iloc[0].get('minimum_stock_level')
            if min_stock and min_stock > 0:
                return int(min_stock)
    
    # Default minimum (can be configured)
    return 20


def calculate_supplier_order_qty(
    product_id: str,
    sph: float,
    cyl: float = 0,
    axis: int = None,
    customer_need: int = 0,
    current_stock: int = 0,
    already_allocated: int = 0
) -> Dict:
    """
    Calculate intelligent supplier order quantity
    
    Logic:
    SUPPLIER_ORDER_QTY = 
        Customer_Shortage 
        + Stock_Replenishment_Need
        - Pending_Supplier_Orders
    
    Where:
        Customer_Shortage = max(0, customer_need - current_stock)
        Stock_After_Customer = current_stock - customer_need
        Expected_Stock = Stock_After_Customer + Pending_Orders
        Stock_Replenishment = max(0, Minimum - Expected_Stock)
    
    Args:
        product_id: Product UUID
        sph, cyl, axis: Power specification
        customer_need: Quantity needed for current customer order
        current_stock: Available stock in warehouse
        already_allocated: Quantity already allocated from stock
    
    Returns:
        Dict with breakdown:
        {
            'supplier_order_qty': int,
            'customer_shortage': int,
            'stock_replenishment': int,
            'pending_orders': int,
            'minimum_threshold': int,
            'current_stock': int,
            'expected_stock_after_delivery': int,
            'explanation': str
        }
    """
    
    # Get minimum stock threshold
    minimum_stock = get_minimum_stock_level(product_id, sph, cyl)
    
    # Get pending supplier orders for this power
    pending_qty = get_pending_supplier_orders_qty(product_id, sph, cyl, axis)
    
    # Calculate customer shortage
    # If already allocated, customer is fulfilled, so shortage = 0
    if already_allocated >= customer_need:
        customer_shortage = 0
    else:
        customer_shortage = max(0, customer_need - current_stock)
    
    # Calculate stock after fulfilling customer
    stock_after_customer = current_stock - min(customer_need, current_stock)
    
    # Expected stock = current remaining + pending deliveries
    expected_stock = stock_after_customer + pending_qty
    
    # Stock replenishment needed to reach minimum
    stock_replenishment = max(0, minimum_stock - expected_stock)
    
    # Final supplier order quantity
    supplier_order_qty = customer_shortage + stock_replenishment
    
    # Expected stock after this order delivers
    expected_stock_after_delivery = expected_stock + supplier_order_qty
    
    # Build explanation
    explanation = f"""
    Current Stock: {current_stock}
    Customer Need: {customer_need}
    Already Allocated: {already_allocated}
    
    → Customer Shortage: {customer_shortage}
    → Stock After Customer: {stock_after_customer}
    → Pending Supplier Orders: {pending_qty}
    → Expected Stock: {expected_stock}
    → Minimum Threshold: {minimum_stock}
    → Replenishment Needed: {stock_replenishment}
    
    ✓ SUPPLIER ORDER QTY: {supplier_order_qty}
    ✓ Expected Stock After Delivery: {expected_stock_after_delivery}
    """
    
    return {
        'supplier_order_qty': supplier_order_qty,
        'customer_shortage': customer_shortage,
        'stock_replenishment': stock_replenishment,
        'pending_orders': pending_qty,
        'minimum_threshold': minimum_stock,
        'current_stock': current_stock,
        'stock_after_customer': stock_after_customer,
        'expected_stock': expected_stock,
        'expected_stock_after_delivery': expected_stock_after_delivery,
        'explanation': explanation.strip()
    }


def calculate_supplier_order_for_lines(order_lines: List[Dict]) -> List[Dict]:
    """
    Calculate supplier order quantities for multiple order lines
    Groups by product/power and calculates optimal quantities
    
    Args:
        order_lines: List of order line dictionaries
    
    Returns:
        List of lines with 'supplier_order_calculation' added
    """
    from modules.batch_manager import get_available_stock
    
    enhanced_lines = []
    
    for line in order_lines:
        product_id = line.get('product_id')
        sph = line.get('sph_out') or line.get('sph')
        cyl = line.get('cyl_out') or line.get('cyl', 0)
        axis = line.get('axis_out') or line.get('axis')
        customer_need = line.get('order_qty', 1)
        already_allocated = line.get('allocated_qty', 0)
        
        # Get current stock
        stock_df = get_available_stock(
            product_id=str(product_id),
            sph=sph,
            cyl=cyl,
            axis=axis
        )
        
        current_stock = int(stock_df['available_qty'].sum()) if not stock_df.empty else 0
        
        # Calculate supplier order qty
        calculation = calculate_supplier_order_qty(
            product_id=str(product_id),
            sph=sph,
            cyl=cyl,
            axis=axis,
            customer_need=customer_need,
            current_stock=current_stock,
            already_allocated=already_allocated
        )
        
        # Add calculation to line
        line_copy = line.copy()
        line_copy['supplier_order_calculation'] = calculation
        line_copy['supplier_order_qty'] = calculation['supplier_order_qty']
        
        enhanced_lines.append(line_copy)
    
    return enhanced_lines


# ============================================================================
# DATABASE SCHEMA FOR MINIMUM STOCK LEVELS (TODO)
# ============================================================================

"""
Recommended database table for power-specific minimum stock levels:

CREATE TABLE product_stock_levels (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    product_id UUID REFERENCES products(product_id),
    sph DECIMAL(4,2),
    cyl DECIMAL(4,2),
    axis INTEGER,
    minimum_stock_qty INTEGER NOT NULL DEFAULT 20,
    maximum_stock_qty INTEGER,
    reorder_point INTEGER,
    lead_time_days INTEGER DEFAULT 7,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    
    UNIQUE(product_id, sph, cyl, axis)
);

CREATE INDEX idx_product_stock_levels_product ON product_stock_levels(product_id);
CREATE INDEX idx_product_stock_levels_power ON product_stock_levels(product_id, sph, cyl, axis);

Example data:
INSERT INTO product_stock_levels (product_id, sph, cyl, axis, minimum_stock_qty)
VALUES 
    ('uuid-airoptix', -6.00, 0, NULL, 15),  -- High demand power
    ('uuid-airoptix', -5.00, 0, NULL, 10),  -- Medium demand
    ('uuid-airoptix', -1.00, 0, NULL, 5);   -- Low demand

This allows setting different minimums for different powers based on demand patterns.
"""


# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    'calculate_supplier_order_qty',
    'calculate_supplier_order_for_lines',
    'get_pending_supplier_orders_qty',
    'get_minimum_stock_level'
]
