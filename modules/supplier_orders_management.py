"""
Supplier Orders Management System
Complete workflow for managing orders to suppliers (vendors)

Features:
1. Create supplier orders from vendor-routed order lines
2. Track supplier order status
3. Receive supplier orders
4. Update inventory upon receipt
5. Link supplier orders to customer orders
6. Supplier performance tracking
"""

import streamlit as st
import pandas as pd
import datetime
import uuid
from typing import Dict, List, Optional
from enum import Enum


# ============================================================================
# SAFE TYPE CONVERSION HELPERS (Fix for None values)
# ============================================================================

def safe_float(value, default=0.0):
    """Safely convert to float, handling None"""
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default

def safe_int(value, default=0):
    """Safely convert to int, handling None"""
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default

def safe_axis(value):
    """Safely format axis value"""
    if value is None:
        return "N/A"
    try:
        return f"{int(value)}°"
    except (ValueError, TypeError):
        return "N/A"


# ============================================================================
# ENUMS & CONSTANTS
# ============================================================================

class SupplierOrderStatus(Enum):
    """Supplier order status lifecycle"""
    DRAFT = "DRAFT"
    PENDING = "PENDING"
    SENT = "SENT"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    IN_TRANSIT = "IN_TRANSIT"
    PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED"
    RECEIVED = "RECEIVED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class SupplierType(Enum):
    """Types of suppliers"""
    LENS_MANUFACTURER = "LENS_MANUFACTURER"
    FRAME_SUPPLIER = "FRAME_SUPPLIER"
    ACCESSORY_SUPPLIER = "ACCESSORY_SUPPLIER"
    COATING_LAB = "COATING_LAB"
    MIXED_SUPPLIER = "MIXED_SUPPLIER"


# ============================================================================
# SESSION STATE INITIALIZATION
# ============================================================================

def init_supplier_orders_state():
    """Initialize session state for supplier orders"""
    
    if 'supplier_orders' not in st.session_state:
        st.session_state.supplier_orders = []
    
    if 'supplier_view_mode' not in st.session_state:
        st.session_state.supplier_view_mode = 'dashboard'  # dashboard, create, detail, receive
    
    if 'selected_supplier_order_id' not in st.session_state:
        st.session_state.selected_supplier_order_id = None
    
    if 'suppliers_master' not in st.session_state:
        # Sample supplier data - replace with database call
        st.session_state.suppliers_master = [
            {
                'supplier_id': 'SUP001',
                'supplier_name': 'Vision Lens India',
                'supplier_type': SupplierType.LENS_MANUFACTURER.value,
                'contact_person': 'Rajesh Kumar',
                'mobile': '+91-9876543210',
                'email': 'rajesh@visionlens.in',
                'lead_time_days': 7,
                'payment_terms': 'NET 30',
                'active': True
            },
            {
                'supplier_id': 'SUP002',
                'supplier_name': 'Optical Supply Co.',
                'supplier_type': SupplierType.MIXED_SUPPLIER.value,
                'contact_person': 'Priya Sharma',
                'mobile': '+91-9988776655',
                'email': 'priya@opticalsupply.co.in',
                'lead_time_days': 5,
                'payment_terms': 'NET 15',
                'active': True
            },
            {
                'supplier_id': 'SUP003',
                'supplier_name': 'Premium Coatings Lab',
                'supplier_type': SupplierType.COATING_LAB.value,
                'contact_person': 'Amit Patel',
                'mobile': '+91-9123456789',
                'email': 'amit@premiumcoatings.com',
                'lead_time_days': 3,
                'payment_terms': 'NET 7',
                'active': True
            }
        ]


# ============================================================================
# SUPPLIER ORDER CREATION
# ============================================================================

def create_supplier_order_from_lines(customer_order: Dict, vendor_lines: List[Dict]):
    """
    Create a supplier order in the DB from vendor-routed lines.
    Saves directly via sql_adapter — no session-state, no string PK.
    The DB auto-generates the integer id; supplier_order_id varchar is set
    from the returned integer id so supplier_panel.py can track it.
    """
    import datetime
    from modules.sql_adapter import run_query, run_write

    order_no = customer_order.get("order_no") or str(customer_order.get("id", ""))

    # Guard: already has a PO for this customer order?
    existing = run_query(
        "SELECT id FROM supplier_orders WHERE customer_order_id = %(ono)s LIMIT 1",
        {"ono": order_no}
    ) or []
    if existing:
        st.warning("⚠️ A supplier order already exists for this order.")
        return

    # Resolve supplier from DB (parties table)
    suppliers = run_query(
        "SELECT id, party_name FROM parties WHERE party_type='Supplier' AND is_active=TRUE ORDER BY party_name LIMIT 1",
        {}
    ) or []
    supplier_id   = suppliers[0]["id"]   if suppliers else None
    supplier_name = suppliers[0]["party_name"] if suppliers else "Unknown Supplier"

    now = datetime.datetime.now()
    expected = (now + datetime.timedelta(days=7)).date()
    total_qty = sum(int(l.get("billing_qty") or l.get("order_qty") or 1) for l in vendor_lines)

    # INSERT supplier_orders — let DB generate integer id
    # supplier_order_id varchar is NOT NULL so we use a temp placeholder,
    # then immediately update it to SO-{id} once we have the real PK.
    new_id = run_query("""
        INSERT INTO supplier_orders (
            supplier_order_id,
            supplier_id, supplier_name, customer_order_id,
            order_date, expected_delivery_date, status,
            total_items, total_qty, total_value,
            created_by, created_at, updated_at
        ) VALUES (
            'SO-PENDING',
            %(sid)s, %(sname)s, %(ono)s,
            NOW(), %(exp)s, 'DRAFT',
            %(ti)s, %(tq)s, 0,
            'backoffice', NOW(), NOW()
        ) RETURNING id
    """, {
        "sid":   supplier_id,
        "sname": supplier_name,
        "ono":   order_no,
        "exp":   expected,
        "ti":    len(vendor_lines),
        "tq":    total_qty,
    }) or []

    if not new_id:
        st.error("❌ Failed to create supplier order in DB.")
        return

    po_int_id = int(new_id[0]["id"])

    # Set varchar reference code from the integer id
    run_write(
        "UPDATE supplier_orders SET supplier_order_id = %(ref)s WHERE id = %(id)s",
        {"ref": f"SO-{po_int_id}", "id": po_int_id}
    )

    # INSERT line items
    for idx, line in enumerate(vendor_lines, 1):
        qty = int(line.get("billing_qty") or line.get("order_qty") or 1)
        allocated = int(line.get("allocated_qty") or 0)
        pending = max(0, qty - allocated)

        import math as _m
        def _sf(v):
            if v is None: return None
            try:
                f = float(v)
                return None if _m.isnan(f) else f
            except: return None

        run_write("""
            INSERT INTO supplier_order_items (
                supplier_order_id, item_no, product_id, product_name,
                brand, eye_side, sph, cyl, axis, add_power,
                ordered_qty, received_qty, pending_qty,
                unit_price, total_price, customer_line_id, item_status
            ) VALUES (
                %(po)s, %(ino)s, %(pid)s, %(pname)s,
                %(brand)s, %(eye)s, %(sph)s, %(cyl)s, %(axis)s, %(add)s,
                %(oqty)s, 0, %(pqty)s,
                %(up)s, %(tp)s, %(clid)s, 'PENDING'
            )
        """, {
            "po":    po_int_id,
            "ino":   idx,
            "pid":   str(line.get("product_id") or ""),
            "pname": str(line.get("product_name") or "N/A"),
            "brand": str(line.get("brand") or ""),
            "eye":   str(line.get("eye_side") or ""),
            "sph":   _sf(line.get("sph") or line.get("sph_out")),
            "cyl":   _sf(line.get("cyl") or line.get("cyl_out")),
            "axis":  _sf(line.get("axis") or line.get("axis_out")),
            "add":   _sf(line.get("add_power")),
            "oqty":  pending,
            "pqty":  pending,
            "up":    float(line.get("unit_price") or 0),
            "tp":    float(line.get("unit_price") or 0) * pending,
            "clid":  str(line.get("line_id") or ""),
        })

        # Mark line as sent to supplier
        line["supplier_order_id"] = f"SO-{po_int_id}"
        line["supplier_order_status"] = "DRAFT"

    # Initial status history entry
    run_write("""
        INSERT INTO supplier_order_status_history (
            supplier_order_id, status, timestamp, notes, changed_by
        ) VALUES (%(po)s, 'DRAFT', NOW(), 'Created from backoffice', 'backoffice')
    """, {"po": po_int_id})

    st.success(f"✅ Supplier Order SO-{po_int_id} created — proceed below to send to supplier.")
    st.rerun()


# ============================================================================
# SUPPLIER ORDER DETAIL VIEW
# ============================================================================

def render_supplier_order_detail(supplier_order: Dict):
    """Display detailed view of a supplier order"""
    
    # Header
    col_back, col_title = st.columns([1, 5])
    
    with col_back:
        if st.button("⬅️ Back", use_container_width=True):
            st.session_state.supplier_view_mode = 'dashboard'
            st.session_state.selected_supplier_order_id = None
            st.rerun()
    
    with col_title:
        st.markdown(f"### 📋 Supplier Order: {supplier_order['supplier_order_id']}")
    
    st.markdown("---")
    
    # Status badge
    status = supplier_order['status']
    status_colors = {
        'DRAFT': '🟡',
        'PENDING': '🟡',
        'SENT': '🟦',
        'ACKNOWLEDGED': '🟢',
        'IN_TRANSIT': '🔵',
        'PARTIALLY_RECEIVED': '🟠',
        'RECEIVED': '🟢',
        'CLOSED': '⚫',
        'CANCELLED': '🔴'
    }
    
    st.markdown(f"**Status:** {status_colors.get(status, '⚪')} {status}")
    
    # Order information
    st.markdown("#### Order Information")
    
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.metric("Supplier", supplier_order['supplier_name'])
    
    with col2:
        order_date = supplier_order.get('order_date', '')
        if order_date:
            date_str = str(order_date)[:10]
        else:
            date_str = 'N/A'
        st.metric("Order Date", date_str)
    
    with col3:
        expected_date = supplier_order.get('expected_delivery_date', '')
        if expected_date:
            exp_str = str(expected_date)[:10]
        else:
            exp_str = 'N/A'
        st.metric("Expected Delivery", exp_str)
    
    with col4:
        st.metric("Priority", supplier_order.get('priority', 'NORMAL'))
    
    st.markdown("---")
    
    # Linked customer order
    if supplier_order.get('customer_order_id'):
        st.info(f"🔗 Linked to Customer Order: {supplier_order['customer_order_id']}")
    
    # Items table
    st.markdown("#### Order Items")
    
    items = supplier_order.get('items', [])
    
    if items:
        items_display = []
        for item in items:
            sph = item.get('sph', 0)
            cyl = item.get('cyl', 0)
            axis = item.get('axis', 0)
            
            items_display.append({
                'Item #': item.get('item_no', ''),
                'Product': item.get('product_name', 'N/A'),
                'Brand': item.get('brand', 'N/A'),
                'Eye': item.get('eye_side', 'N/A'),
                'Power': f"SPH {sph:+.2f} CYL {cyl:+.2f} @ {axis}°",
                'Ordered': item.get('ordered_qty', 0),
                'Received': item.get('received_qty', 0),
                'Pending': item.get('pending_qty', 0),
                'Status': item.get('item_status', 'PENDING')
            })
        
        st.dataframe(pd.DataFrame(items_display), use_container_width=True, hide_index=True)
        
        # Summary metrics
        col_items, col_qty, col_value = st.columns(3)
        
        with col_items:
            st.metric("Total Items", len(items))
        
        with col_qty:
            total_ordered = sum(item.get('ordered_qty', 0) for item in items)
            total_received = sum(item.get('received_qty', 0) for item in items)
            st.metric("Quantity", f"{total_received}/{total_ordered}")
        
        with col_value:
            st.metric("Order Value", f"₹{supplier_order.get('total_value', 0):,.2f}")
    
    else:
        st.warning("⚠️ No items in this order")
    
    st.markdown("---")
    
    # Special instructions
    if supplier_order.get('special_instructions'):
        with st.expander("📝 Special Instructions"):
            st.text(supplier_order['special_instructions'])
    
    # Status history
    if supplier_order.get('status_history'):
        with st.expander("📜 Status History"):
            for entry in reversed(supplier_order['status_history']):
                timestamp = entry.get('timestamp', '')
                if timestamp:
                    time_str = str(timestamp)[:19].replace('T', ' ')
                else:
                    time_str = 'N/A'
                
                st.markdown(f"**{entry.get('status')}** - {time_str}")
                if entry.get('notes'):
                    st.text(f"  {entry['notes']}")
                st.markdown("---")
    
    # Action buttons
    st.markdown("#### Actions")
    
    col_receive, col_update, col_cancel = st.columns(3)
    
    with col_receive:
        if status in ['SENT', 'ACKNOWLEDGED', 'IN_TRANSIT', 'PARTIALLY_RECEIVED']:
            if st.button("📥 Receive Items", type="primary", use_container_width=True):
                st.session_state.supplier_view_mode = 'receive'
                st.rerun()
    
    with col_update:
        if st.button("✏️ Update Status", use_container_width=True):
            st.session_state.show_status_update = True
            st.rerun()
    
    with col_cancel:
        if status not in ['RECEIVED', 'CLOSED', 'CANCELLED']:
            if st.button("🚫 Cancel Order", use_container_width=True):
                if st.session_state.get('confirm_cancel'):
                    # Cancel the order
                    supplier_order['status'] = SupplierOrderStatus.CANCELLED.value
                    supplier_order['updated_at'] = datetime.datetime.now().isoformat()
                    supplier_order['status_history'].append({
                        'status': SupplierOrderStatus.CANCELLED.value,
                        'timestamp': datetime.datetime.now().isoformat(),
                        'notes': 'Order cancelled by user'
                    })
                    st.success("✅ Order cancelled")
                    st.session_state.confirm_cancel = False
                    st.rerun()
                else:
                    st.session_state.confirm_cancel = True
                    st.warning("⚠️ Click again to confirm cancellation")
    
    # Status update modal
    if st.session_state.get('show_status_update'):
        st.markdown("---")
        st.markdown("### Update Order Status")
        
        current_status = supplier_order['status']
        
        new_status = st.selectbox(
            "New Status",
            [s.value for s in SupplierOrderStatus],
            index=[s.value for s in SupplierOrderStatus].index(current_status),
            key="supplier_status_update"
        )
        
        notes = st.text_area(
            "Update Notes",
            key="supplier_status_notes"
        )
        
        col_save, col_cancel_update = st.columns(2)
        
        with col_save:
            if st.button("✅ Update", type="primary", use_container_width=True):
                supplier_order['status'] = new_status
                supplier_order['updated_at'] = datetime.datetime.now().isoformat()
                supplier_order['status_history'].append({
                    'status': new_status,
                    'timestamp': datetime.datetime.now().isoformat(),
                    'notes': notes or 'Status updated'
                })
                st.success(f"✅ Status updated to: {new_status}")
                st.session_state.show_status_update = False
                st.rerun()
        
        with col_cancel_update:
            if st.button("Cancel", use_container_width=True):
                st.session_state.show_status_update = False
                st.rerun()


# ============================================================================
# RECEIVE SUPPLIER ORDER
# ============================================================================

def render_receive_supplier_order(supplier_order: Dict):
    """Interface to receive items from supplier order"""
    
    st.markdown("### 📥 Receive Supplier Order")
    st.markdown(f"**Order ID:** {supplier_order['supplier_order_id']}")
    st.markdown(f"**Supplier:** {supplier_order['supplier_name']}")
    
    st.markdown("---")
    
    # Receipt date
    receipt_date = st.date_input(
        "Receipt Date",
        value=datetime.date.today(),
        key="receipt_date"
    )
    
    # Items to receive
    st.markdown("#### Items to Receive")
    
    items = supplier_order.get('items', [])
    receive_data = []
    
    for idx, item in enumerate(items):
        if item.get('pending_qty', 0) > 0:
            st.markdown(f"**Item {item.get('item_no')}: {item.get('product_name')}**")
            
            col1, col2, col3 = st.columns([2, 1, 1])
            
            with col1:
                power_info = f"SPH {item.get('sph', 0):+.2f} CYL {item.get('cyl', 0):+.2f} @ {item.get('axis', 0)}°"
                st.text(f"Power: {power_info}")
                st.text(f"Eye: {item.get('eye_side', 'N/A')}")
            
            with col2:
                st.text(f"Ordered: {item.get('ordered_qty', 0)}")
                st.text(f"Pending: {item.get('pending_qty', 0)}")
            
            with col3:
                receive_qty = st.number_input(
                    "Receive Qty",
                    min_value=0,
                    max_value=item.get('pending_qty', 0),
                    value=item.get('pending_qty', 0),
                    key=f"receive_qty_{idx}"
                )
                receive_data.append({
                    'item_idx': idx,
                    'receive_qty': receive_qty
                })
            
            st.markdown("---")
    
    # Receipt notes
    receipt_notes = st.text_area(
        "Receipt Notes / Quality Check Comments",
        key="receipt_notes",
        height=100
    )
    
    # Action buttons
    col_receive, col_cancel = st.columns(2)
    
    with col_receive:
        if st.button("✅ Confirm Receipt", type="primary", use_container_width=True):
            # Update item quantities
            total_received = 0
            all_items_received = True
            
            for data in receive_data:
                idx = data['item_idx']
                qty = data['receive_qty']
                
                if qty > 0:
                    items[idx]['received_qty'] = items[idx].get('received_qty', 0) + qty
                    items[idx]['pending_qty'] = items[idx].get('pending_qty', 0) - qty
                    total_received += qty
                    
                    # Update item status
                    if items[idx]['pending_qty'] == 0:
                        items[idx]['item_status'] = 'RECEIVED'
                    else:
                        items[idx]['item_status'] = 'PARTIALLY_RECEIVED'
                
                if items[idx].get('pending_qty', 0) > 0:
                    all_items_received = False
            
            # Update order status
            if all_items_received:
                supplier_order['status'] = SupplierOrderStatus.RECEIVED.value
                status_note = f'All items received. Total: {total_received} units'
            else:
                supplier_order['status'] = SupplierOrderStatus.PARTIALLY_RECEIVED.value
                status_note = f'Partial receipt. Received: {total_received} units'
            
            # Add to status history
            supplier_order['status_history'].append({
                'status': supplier_order['status'],
                'timestamp': datetime.datetime.now().isoformat(),
                'notes': f"{status_note}. {receipt_notes}" if receipt_notes else status_note
            })
            
            supplier_order['updated_at'] = datetime.datetime.now().isoformat()
            supplier_order['last_receipt_date'] = receipt_date.isoformat()
            
            st.success(f"✅ Receipt Confirmed: {total_received} items received")
            # -------------------------------
            # AUTO: Insert stock + Reallocate
            # -------------------------------

            from modules.sql_adapter import insert_batch
            from modules.workflow.engine import refresh_line_state, reload_order
            from modules.sql_adapter import fetch_full_order

            # Insert received stock into batches
            for item in items:

                received = item.get("received_qty", 0)
                product_id = item.get("product_id")

                if received > 0 and product_id:

                    insert_batch(
                        product_id=product_id,
                        sph=item.get("sph"),
                        cyl=item.get("cyl"),
                        axis=item.get("axis"),
                        add_power=item.get("add_power"),
                        qty=received,
                        cost=item.get("unit_cost", 0)
                    )


            # Auto reallocate pending customer lines
            linked_orders = supplier_order.get("linked_customer_orders", [])

            for order_no in linked_orders:

                full_order = fetch_full_order(order_no)

                if not full_order:
                    continue

                for line in full_order.get("lines", []):
                    if line.get("pending_qty", 0) > 0:
                        refresh_line_state(line)

                order = reload_order(order_no)
                if not order:
                    st.warning(f"⚠️ Reload failed: {order_no}")

            st.info("📦 Inventory has been updated")
            
            # Return to detail view
            st.session_state.supplier_view_mode = 'detail'
            st.rerun()
    
    with col_cancel:
        if st.button("❌ Cancel", use_container_width=True, key="cancel_edit_supplier_order"):
            st.session_state.supplier_view_mode = 'detail'
            st.rerun()


# ============================================================================
# DASHBOARD
# ============================================================================

def render_supplier_orders_dashboard():
    """Main dashboard for supplier orders"""
    
    st.markdown("### 📊 Supplier Orders Dashboard")
    
    # Action buttons
    col_create, col_refresh, col_filter = st.columns([1, 1, 2])
    
    with col_create:
        if st.button("➕ Create New Order", use_container_width=True):
            st.info("💡 Create supplier orders from Backoffice Management → Vendor Lines")
    
    with col_refresh:
        if st.button("🔄 Refresh", use_container_width=True):
            st.rerun()
    
    with col_filter:
        status_filter = st.selectbox(
            "Filter by Status",
            ['All'] + [s.value for s in SupplierOrderStatus],
            key="supplier_dashboard_filter"
        )
    
    st.markdown("---")
    
    # Summary metrics
    orders = st.session_state.supplier_orders
    
    if orders:
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            st.metric("Total Orders", len(orders))
        
        with col2:
            pending_orders = len([o for o in orders if o['status'] in ['SENT', 'ACKNOWLEDGED', 'IN_TRANSIT']])
            st.metric("Pending", pending_orders)
        
        with col3:
            received_orders = len([o for o in orders if o['status'] == 'RECEIVED'])
            st.metric("Received", received_orders)
        
        with col4:
            total_value = sum(o.get('total_value', 0) for o in orders)
            st.metric("Total Value", f"₹{total_value:,.2f}")
        
        st.markdown("---")
        
        # Filter orders
        filtered_orders = orders
        if status_filter != 'All':
            filtered_orders = [o for o in orders if o['status'] == status_filter]
        
        # Orders list
        if filtered_orders:
            st.markdown(f"**Showing {len(filtered_orders)} of {len(orders)} orders**")
            
            for order in filtered_orders:
                with st.expander(
                    f"{order['supplier_order_id']} - {order['supplier_name']} - {order['status']}",
                    expanded=False
                ):
                    col_a, col_b, col_c, col_d = st.columns(4)
                    
                    with col_a:
                        order_date = order.get('order_date', '')
                        if order_date:
                            date_str = str(order_date)[:10]
                        else:
                            date_str = 'N/A'
                        st.text(f"Date: {date_str}")
                    
                    with col_b:
                        st.text(f"Items: {order.get('total_items', 0)}")
                    
                    with col_c:
                        st.text(f"Qty: {order.get('total_qty', 0)}")
                    
                    with col_d:
                        if st.button("View Details", key=f"view_{order['supplier_order_id']}", 
                                   use_container_width=True):
                            st.session_state.selected_supplier_order_id = order['supplier_order_id']
                            st.session_state.supplier_view_mode = 'detail'
                            st.rerun()
        else:
            st.info("📭 No orders match the selected filter")
    
    else:
        st.info("📭 No supplier orders yet. Create orders from vendor-routed items in Backoffice Management.")


# ============================================================================
# MAIN RENDER FUNCTION
# ============================================================================

def render_supplier_orders_management():
    """Main render function for supplier orders management"""
    
    init_supplier_orders_state()
    
    st.title("📦 Supplier Orders Management")
    st.markdown("Track and manage orders to suppliers/vendors")
    
    view_mode = st.session_state.supplier_view_mode
    
    if view_mode == 'dashboard':
        render_supplier_orders_dashboard()
    
    elif view_mode == 'detail':
        # Find selected order
        order_id = st.session_state.selected_supplier_order_id
        supplier_order = next(
            (o for o in st.session_state.supplier_orders if o['supplier_order_id'] == order_id),
            None
        )
        
        if supplier_order:
            render_supplier_order_detail(supplier_order)
        else:
            st.error("❌ Order not found")
            st.session_state.supplier_view_mode = 'dashboard'
            st.rerun()
    
    elif view_mode == 'receive':
        # Find selected order
        order_id = st.session_state.selected_supplier_order_id
        supplier_order = next(
            (o for o in st.session_state.supplier_orders if o['supplier_order_id'] == order_id),
            None
        )
        
        if supplier_order:
            render_receive_supplier_order(supplier_order)
        else:
            st.error("❌ Order not found")
            st.session_state.supplier_view_mode = 'dashboard'
            st.rerun()


# ============================================================================
# INTEGRATION HELPER FUNCTIONS
# ============================================================================

def get_vendor_routed_lines(order: Dict) -> List[Dict]:
    """
    Extract lines that need supplier procurement from an order
    
    Args:
        order: Customer order dictionary
    
    Returns:
        List of lines with manufacturing_route = 'VENDOR' or 'EXTERNAL_LAB'
    
    SUPPORTS:
    - VENDOR: Items explicitly routed to external vendors
    - EXTERNAL_LAB: Items routed to external labs that need procurement
    """
    all_lines = []
    
    # Collect all lines
    if order.get('stock_lines'):
        all_lines.extend(order['stock_lines'])
    if order.get('inhouse_lines'):
        all_lines.extend(order['inhouse_lines'])
    if order.get('lab_order_lines'):
        all_lines.extend(order['lab_order_lines'])
    
    # ✅ FIX: Filter for BOTH VENDOR and EXTERNAL_LAB routes
    vendor_lines = [
        line for line in all_lines 
        if line.get('manufacturing_route') in ['VENDOR', 'EXTERNAL_LAB'] and
        not line.get('supplier_order_id')  # Not already ordered
    ]
    
    return vendor_lines


def add_supplier_order_button_to_backoffice(order: Dict):
    """
    Add a button in backoffice management to create supplier orders
    Call this from within backoffice order detail view
    """
    vendor_lines = get_vendor_routed_lines(order)
    
    if vendor_lines:
        st.markdown("---")
        st.markdown("#### 📦 Vendor Orders")
        st.info(f"ℹ️ {len(vendor_lines)} items routed to vendor")
        
        if st.button(
            f"📤 Create Supplier Order ({len(vendor_lines)} items)",
            type="primary",
            use_container_width=True,
            key="create_supplier_order_btn"
        ):
            # Store order and lines in session state
            st.session_state.supplier_order_source = {
                'customer_order': order,
                'vendor_lines': vendor_lines
            }
            # Switch to supplier orders module
            st.session_state.supplier_view_mode = 'create'
            st.switch_page("pages/supplier_orders.py")  # Adjust path as needed


# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    'render_supplier_orders_management',
    'SupplierOrderStatus',
    'SupplierType',
    'get_vendor_routed_lines',
    'add_supplier_order_button_to_backoffice'
]
