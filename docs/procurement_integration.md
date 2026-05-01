# Procurement Module - Technical Documentation

## Overview

This document describes the procurement/purchase order functionality in the optical ERP system. The procurement module enables inventory-driven purchase orders based on real stock levels and sales velocity.

---

## 1. Module Location & Navigation

### 1.1 Access Point

**Sidebar**: 🛒 Smart Purchase Module

### 1.2 Tabs Structure

```
🛒 Smart Purchase Module
├── 🎯 Smart Conversion    → Create POs from inventory analysis (DEFAULT)
├── 📋 Purchase Orders     → View/manage open POs
├── 📊 Analytics           → Procurement analytics
├── 🔄 Reorder Monitor     → Stock monitoring with power-level aggregation
├── 🔀 Supplier Override   → Supplier preferences per product
├── ⏰ Supplier Schedule   → Delivery schedules
└── 📐 Stock Minimums      → Stock threshold configuration
```

---

## 2. Database Schema

### 2.1 supplier_orders Table

Primary table for purchase orders:

```sql
CREATE TABLE supplier_orders (
    id                       SERIAL PRIMARY KEY,
    supplier_order_id        VARCHAR(50) NOT NULL UNIQUE,  -- e.g., "PO-20240407103015"
    supplier_id              UUID NOT NULL REFERENCES parties(id),
    supplier_name            VARCHAR(255) NOT NULL,
    customer_order_id        VARCHAR(100),                 -- Link to sales order if applicable
    order_date               DATE NOT NULL,
    expected_delivery_date   DATE,
    status                   VARCHAR(20) NOT NULL DEFAULT 'DRAFT',
    po_type                  VARCHAR(20),                   -- 'CONVERSION', 'REPLENISHMENT', 'EXTERNAL_LAB'
    total_value              DECIMAL(12,2) DEFAULT 0,
    total_items              INTEGER DEFAULT 0,
    total_qty                INTEGER DEFAULT 0,
    priority                 VARCHAR(20) DEFAULT 'NORMAL',
    payment_terms            VARCHAR(50),
    notes                    TEXT,
    special_instructions     TEXT,
    created_by               VARCHAR(100),
    created_at               TIMESTAMPTZ DEFAULT NOW(),
    updated_at               TIMESTAMPTZ DEFAULT NOW()
);
```

### 2.2 supplier_order_items Table

Line items for each purchase order:

```sql
CREATE TABLE supplier_order_items (
    id                   SERIAL PRIMARY KEY,
    supplier_order_id   INTEGER NOT NULL REFERENCES supplier_orders(id) ON DELETE CASCADE,
    item_no              INTEGER NOT NULL,
    product_id           UUID REFERENCES products(id),
    product_name         VARCHAR(255),
    brand                VARCHAR(100),
    eye_side             VARCHAR(10),
    sph                  NUMERIC(6,2),
    cyl                  NUMERIC(6,2),
    axis                 INTEGER,
    add_power            NUMERIC(6,2),
    ordered_qty          INTEGER NOT NULL DEFAULT 0,
    received_qty          INTEGER DEFAULT 0,
    pending_qty          INTEGER DEFAULT 0,
    unit_price           DECIMAL(12,2) DEFAULT 0,
    total_price          DECIMAL(12,2) DEFAULT 0,
    customer_line_id     UUID,
    item_status          VARCHAR(20) DEFAULT 'PENDING'  -- 'PENDING', 'PARTIAL', 'RECEIVED'
);
```

### 2.3 supplier_order_status_history Table

Audit trail for PO status changes:

```sql
CREATE TABLE supplier_order_status_history (
    id                  SERIAL PRIMARY KEY,
    supplier_order_id  INTEGER NOT NULL REFERENCES supplier_orders(id) ON DELETE CASCADE,
    status              VARCHAR(20) NOT NULL,
    timestamp           TIMESTAMPTZ DEFAULT NOW(),
    notes               TEXT,
    changed_by          VARCHAR(100)
);
```

---

## 3. Key Functions

### 3.1 create_purchase_order()

**File**: `modules/procurement/purchase_ui.py`  
**Lines**: 275-344

```python
def create_purchase_order(selected_products: List[dict],
                          supplier_id: str,
                          supplier_name: str,
                          notes: str = "") -> bool:
    """
    Build and persist a supplier_order using your existing save_supplier_order().
    
    Args:
        selected_products: List of product dicts with product_id, product_name, 
                          unit_cost, current_stock, min_stock, etc.
        supplier_id: UUID of supplier from parties table
        supplier_name: Display name of supplier
        notes: Optional notes for the PO
    
    Returns:
        True if successful, False on error
    """
    if not DB_CONNECTED:
        return True   # demo mode

    now = datetime.now()
    po_id = f"PO-{now.strftime('%Y%m%d%H%M%S')}"

    items = []
    total_qty   = 0
    total_value = 0.0

    for idx, p in enumerate(selected_products, start=1):
        qty  = calculate_suggested_quantity(p)
        cost = float(p.get("unit_cost") or 0)
        items.append({
            "item_no":      idx,
            "product_id":   str(p.get("product_id", "")),
            "product_name": p.get("product_name", "Unknown"),
            "brand":        p.get("brand", ""),
            "eye_side":     None,
            "sph": None, "cyl": None, "axis": None, "add_power": None,
            "ordered_qty":  qty,
            "received_qty": 0,
            "pending_qty":  qty,
            "unit_price":   cost,
            "total_price":  qty * cost,
            "customer_line_id": None,
            "item_status":  "PENDING",
        })
        total_qty   += qty
        total_value += qty * cost

    supplier_order = {
        "supplier_order_id":       po_id,
        "supplier_id":             supplier_id,
        "supplier_name":           supplier_name,
        "customer_order_id":       None,
        "order_date":              now,
        "expected_delivery_date":  now + timedelta(days=14),
        "priority":                "NORMAL",
        "payment_terms":           "NET30",
        "special_instructions":    notes,
        "status":                  "DRAFT",
        "total_items":             len(items),
        "total_qty":               total_qty,
        "total_value":             total_value,
        "created_by":              "purchase_module",
        "created_at":              now,
        "updated_at":              now,
        "items":                   items,
        "status_history": [{
            "status":     "DRAFT",
            "timestamp":  now,
            "notes":      "Created via Smart Purchase Module",
            "changed_by": "purchase_module",
        }],
    }

    try:
        save_supplier_order(supplier_order)
        return True
    except Exception as e:
        st.error(f"PO save failed: {e}")
        return False
```

---

### 3.2 save_supplier_order()

**File**: `modules/sql_adapter.py`  
**Lines**: 1327-1430

```python
def save_supplier_order(supplier_order: dict):
    """
    Save or update supplier order with items + history
    ✅ NaN-safe with automatic sanitization
    
    Args:
        supplier_order: Dict containing:
            - supplier_order_id: str
            - supplier_id: UUID
            - supplier_name: str
            - customer_order_id: str (optional)
            - order_date: datetime
            - expected_delivery_date: datetime
            - status: str (DRAFT, SENT, RECEIVED, etc.)
            - priority: str
            - payment_terms: str
            - special_instructions: str
            - total_items: int
            - total_qty: int
            - total_value: float
            - created_by: str
            - created_at: datetime
            - updated_at: datetime
            - items: list of dicts
            - status_history: list of dicts
    """
    conn = None
    cursor = None

    try:
        conn = get_transaction_connection()
        cursor = conn.cursor()

        so_id = supplier_order['supplier_order_id']

        # Check if exists (UPDATE) or new (INSERT)
        cursor.execute(
            "SELECT 1 FROM supplier_orders WHERE supplier_order_id = %s",
            (so_id,)
        )
        exists = cursor.fetchone()

        if exists:
            # UPDATE existing
            cursor.execute("""
                UPDATE supplier_orders SET
                    supplier_id = %s, order_date = %s, status = %s,
                    expected_delivery = %s, notes = %s, total_amount = %s,
                    updated_at = NOW()
                WHERE supplier_order_id = %s
            """, (
                supplier_order.get('supplier_id'),
                supplier_order.get('order_date'),
                supplier_order.get('status', 'PENDING'),
                supplier_order.get('expected_delivery'),
                supplier_order.get('notes'),
                supplier_order.get('total_amount', 0),
                so_id
            ))
            # Delete old items before re-insert
            cursor.execute(
                "DELETE FROM supplier_order_items WHERE supplier_order_id = %s",
                (so_id,)
            )
        else:
            # INSERT new
            cursor.execute("""
                INSERT INTO supplier_orders (
                    supplier_order_id, supplier_id, order_date, status,
                    expected_delivery, notes, total_amount, created_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                so_id,
                supplier_order.get('supplier_id'),
                supplier_order.get('order_date'),
                supplier_order.get('status', 'PENDING'),
                supplier_order.get('expected_delivery'),
                supplier_order.get('notes'),
                supplier_order.get('total_amount', 0),
                supplier_order.get('created_by', 'system')
            ))

        # Insert items
        for item in supplier_order.get('items', []):
            cursor.execute("""
                INSERT INTO supplier_order_items (
                    supplier_order_id, item_no, product_id, product_name,
                    brand, eye_side, sph, cyl, axis, add_power,
                    ordered_qty, received_qty, pending_qty,
                    unit_price, total_price, customer_line_id, item_status
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                so_id, item.get('item_no'), item.get('product_id'),
                item.get('product_name'), item.get('brand'), item.get('eye_side'),
                item.get('sph'), item.get('cyl'), item.get('axis'),
                item.get('add_power'), item.get('ordered_qty', 0),
                item.get('received_qty', 0), item.get('pending_qty', 0),
                item.get('unit_price', 0), item.get('total_price', 0),
                item.get('customer_line_id'), item.get('item_status', 'PENDING'),
            ))

        # Insert status history
        for h in supplier_order.get('status_history', []):
            cursor.execute("""
                INSERT INTO supplier_order_status_history (
                    supplier_order_id, status, timestamp, notes, changed_by
                ) VALUES (%s,%s,%s,%s,%s)
            """, (
                so_id, h.get('status'), h.get('timestamp'),
                h.get('notes'), h.get('changed_by'),
            ))

        conn.commit()

    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"save_supplier_order failed: {e}")
        raise QueryError(e)
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
```

---

### 3.3 fetch_supplier_orders()

**File**: `modules/sql_adapter.py`  
**Lines**: 1489-1564

```python
def fetch_supplier_orders():
    """
    Fetch all supplier orders with items + history
    ✅ NaN-safe results
    
    Returns:
        List of dicts, each containing:
        - All supplier_orders columns
        - items: list of supplier_order_items dicts
        - status_history: list of status_history dicts
    """
    conn = None
    cursor = None

    try:
        conn = get_transaction_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        # Fetch all orders
        cursor.execute("""
            SELECT * FROM supplier_orders ORDER BY order_date DESC
        """)
        orders = cursor.fetchall()

        results = []
        for order in orders:
            # Use integer PK (id) for joining items/history
            so_pk = int(order['id'])

            # Fetch items
            cursor.execute("""
                SELECT * FROM supplier_order_items
                WHERE supplier_order_id = %s ORDER BY item_no
            """, (so_pk,))
            items = cursor.fetchall()

            # Fetch history
            cursor.execute("""
                SELECT * FROM supplier_order_status_history
                WHERE supplier_order_id = %s ORDER BY timestamp DESC
            """, (so_pk,))
            history = cursor.fetchall()

            # Sanitize nested data
            order['items'] = [
                {k: _sanitize_value(v) for k, v in dict(item).items()}
                for item in items
            ]
            order['status_history'] = [
                {k: _sanitize_value(v) for k, v in dict(h).items()}
                for h in history
            ]

            results.append(dict(order))

        return results

    except Exception as e:
        logger.error(f"fetch_supplier_orders failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
```

---

### 3.4 load_open_purchase_orders()

**File**: `modules/procurement/purchase_ui.py`  
**Lines**: 190-198

```python
@st.cache_data(ttl=120)
def load_open_purchase_orders() -> List[dict]:
    """
    Fetch non-closed supplier orders for the PO tab.
    Cached for 2 minutes to reduce DB load.
    """
    if not DB_CONNECTED:
        return []
    try:
        orders = fetch_supplier_orders()
        # Filter out closed/completed POs
        return [o for o in orders if o.get("status") not in ("RECEIVED", "CLOSED", "CANCELLED")]
    except Exception:
        return []
```

---

## 4. UI Flow

### 4.1 Smart Conversion Tab

**File**: `modules/procurement/purchase_ui.py`  
**Lines**: 506-677

The Smart Conversion tab works as follows:

1. **Load candidates** - Fetch inventory summary and sales velocity
2. **Analyze inventory** - Flag products needing attention based on:
   - Days of stock left (current_stock / avg_daily_sales)
   - Urgency classification: critical, high, trending, seasonal
3. **Display grouped view** - Show products by urgency category
4. **User selection** - Checkbox selection of products to order
5. **Supplier selection** - Dropdown to choose supplier from parties table
6. **Create PO** - Button triggers `create_purchase_order()`

```python
# Key UI elements
col_left, col_right = st.columns([3, 1])
with col_left:
    total_flagged = len(candidates)
    st.markdown(f"### Engine analysed inventory — **{total_flagged} products** need attention")
with col_right:
    view = st.selectbox("View", ["Categories", "Individual Items"])

# Category cards with Select All checkbox
for urgency_key, (label, css_class, subtitle) in group_meta.items():
    group = groups[urgency_key]
    if group.empty:
        continue
    # ... display logic

# Supplier selection
st.session_state.pm_supplier_id = st.selectbox(
    "Select Supplier", 
    options=[''] + list(suppliers.keys()),
    format_func=lambda x: suppliers.get(x, x)
)

# Create button
if st.button("Create Purchase Order →", type="primary"):
    success = create_purchase_order(
        selected_list,
        st.session_state.pm_supplier_id or "S0",
        st.session_state.pm_supplier_name or "Demo Supplier",
        st.session_state.pm_po_notes,
    )
```

---

### 4.2 Purchase Orders Tab

**File**: `modules/procurement/purchase_ui.py`  
**Lines**: 684-750

Displays all open (non-received/closed/cancelled) POs:

```python
with tab2:
    st.markdown("### 📋 Open Purchase Orders")

    if st.button("🔄 Refresh POs"):
        load_open_purchase_orders.clear()

    open_pos = load_open_purchase_orders()

    if not open_pos:
        st.info("No open purchase orders found. Create one from the Smart Conversion tab.")
    else:
        for po in open_pos:
            with st.expander(f"🗂️ {po.get('supplier_order_id')} — {po.get('supplier_name')} — {po.get('status')}"):
                col1, col2, col3 = st.columns(3)
                col1.metric("Items", po.get("total_items", 0))
                col2.metric("Total Qty", po.get("total_qty", 0))
                col3.metric("Value", f"₹{float(po.get('total_value', 0)):,.0f}")

                st.markdown(f"**Order Date:** {po.get('order_date', '—')}  |  "
                            f"**Expected:** {po.get('expected_delivery_date', '—')}")

                items = po.get("items", [])
                if items:
                    items_df = pd.DataFrame(items)[
                        ["item_no", "product_name", "ordered_qty", "received_qty",
                         "pending_qty", "unit_price", "total_price", "item_status"]
                    ]
                    st.dataframe(items_df, use_container_width=True)

                # Action buttons for each PO
                _po_id = str(po.get("id") or po.get("supplier_order_id") or "")
                _po_st = str(po.get("status") or "Draft")
                # ... Send/Receive/Cancel buttons
```

---

## 5. Status Values

Valid PO statuses:

| Status | Description |
|--------|-------------|
| `DRAFT` | Initial status when PO is created |
| `SENT` | PO sent to supplier |
| `ACKNOWLEDGED` | Supplier acknowledged the PO |
| `PARTIAL` | Partial delivery received |
| `RECEIVED` | Full delivery received |
| `CLOSED` | PO closed after billing |
| `CANCELLED` | PO cancelled |

**Filter logic** (Purchase Orders tab):
```python
[o for o in orders if o.get("status") not in ("RECEIVED", "CLOSED", "CANCELLED")]
```

---

## 6. Business Logic

### 6.1 Calculate Suggested Quantity

**File**: `modules/procurement/purchase_ui.py`  
**Lines**: 226-232

```python
def calculate_suggested_quantity(product: dict) -> int:
    """
    Calculate reorder quantity based on:
    - Projected sales during supplier lead time + safety buffer
    - Current stock deficit
    - Minimum order quantity (MOQ)
    """
    SAFETY_BUFFER_DAYS = 7
    avg_daily = product.get("avg_daily_sales", 0)
    lead_days = product.get("supplier_lead_days", 7)
    current = product.get("current_stock", 0)
    min_stock = product.get("min_stock", 0)
    moq = max(product.get("moq", 10), 1)

    total_days = lead_days + SAFETY_BUFFER_DAYS
    proj_sales = avg_daily * total_days
    deficit = max(0, min_stock - current)
    needed = proj_sales + deficit

    return int(math.ceil(needed / moq) * moq)
```

---

### 6.2 Urgency Classification

**File**: `modules/procurement/purchase_ui.py`  
**Lines**: 238-255

```python
def classify(row):
    """Classify product urgency based on days of stock left."""
    days = row["days_left"]
    if days < 3:
        return "critical"
    elif days < 7:
        return "high"
    elif days < 14:
        return "trending"
    else:
        return "seasonal"
```

---

## 7. Related Files

| File | Purpose |
|------|---------|
| `modules/procurement/purchase_ui.py` | Main procurement UI - Smart Conversion, PO tabs |
| `modules/sql_adapter.py` | `save_supplier_order()`, `fetch_supplier_orders()` |
| `modules/procurement/po_engine.py` | Auto-populate supplier orders from sales |
| `modules/backoffice/procurement_consolidation.py` | Purchase invoice creation from acknowledgements |
| `modules/backoffice/supplier_panel.py` | Supplier PO management in backoffice |
| `modules/supplier_orders_management.py` | Supplier order creation utilities |

---

## 8. How to Use

### 8.1 Creating a Purchase Order via Smart Conversion

1. Navigate to **🛒 Smart Purchase Module** in sidebar
2. Default tab is **🎯 Smart Conversion**
3. Review products flagged for reordering:
   - 🔴 **Critical** (red): Stock will run out in <3 days
   - 🟡 **High Priority** (yellow): Below reorder point
   - 📈 **Trending** (blue): Fast-moving items
   - 💡 **Seasonal** (purple): Plan ahead
4. Select products using checkboxes (individual or Select All per category)
5. Choose supplier from dropdown
6. Optionally add notes
7. Click **Create Purchase Order →**
8. Success message shows PO ID and total cost

### 8.2 Viewing Open Purchase Orders

1. Navigate to **🛒 Smart Purchase Module** in sidebar
2. Click **📋 Purchase Orders** tab
3. View all open POs with:
   - Supplier name and PO ID
   - Status (Draft, Sent, etc.)
   - Items, Total Qty, Value metrics
   - Order Date and Expected Delivery
4. Expand any PO to see line items in table
5. Use action buttons to Send/Receive/Cancel

---

## 9. Troubleshooting

### 9.1 No products showing in Smart Conversion

**Cause**: Inventory data may be empty or all products adequately stocked

**Check**:
```sql
SELECT COUNT(*) FROM inventory_stock;
SELECT COUNT(*) FROM products;
```

### 9.2 No suppliers in dropdown

**Cause**: Parties table missing suppliers (party_type = 'SUPPLIER')

**Check**:
```sql
SELECT id, party_name FROM parties WHERE party_type = 'SUPPLIER';
```

**Fix**: Add suppliers to parties table:
```sql
INSERT INTO parties (party_name, party_type, is_active) 
VALUES ('Your Supplier Name', 'SUPPLIER', true);
```

### 9.3 PO not appearing in Purchase Orders tab

**Cause**: PO status may be RECEIVED/CLOSED/CANCELLED

**Check**:
```sql
SELECT supplier_order_id, status, created_at 
FROM supplier_orders 
ORDER BY created_at DESC LIMIT 10;
```

---

*Document generated: 2026-04-07*