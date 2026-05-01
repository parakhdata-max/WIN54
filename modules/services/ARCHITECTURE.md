# DV ERP — Architecture Reference
# modules/billing/ARCHITECTURE.md

## Layer Separation

```
┌─────────────────────────────────────────────────────┐
│  UI Layer  (streamlit, session_state, widgets)       │
│  modules/billing/payment_collection.py  (UI only)   │
│  modules/billing/bulk_order.py          (UI only)   │
│  modules/billing/challan_invoice_manager.py (UI+svc)│
└──────────────────────┬──────────────────────────────┘
                       │ calls
┌──────────────────────▼──────────────────────────────┐
│  Service Layer  (business logic, no UI, no SQL)      │
│  modules/billing/services/payment_service.py        │
│  modules/billing/services/challan_service.py        │
│  modules/core/price_qty_governor.py                 │
│  modules/core/business_rules.py                     │
└──────────────────────┬──────────────────────────────┘
                       │ calls
┌──────────────────────▼──────────────────────────────┐
│  DB Layer  (SQL only, no business logic, no UI)      │
│  modules/billing/db/billing_queries.py              │
│  modules/sql_adapter.py  (connection, transactions) │
└─────────────────────────────────────────────────────┘
```

## Rules

### UI Layer
- May import from: services, db, core
- Must NOT: run SQL directly, contain business rules
- Must: use st.session_state for state, call service functions for actions

### Service Layer
- May import from: db, core, utils
- Must NOT: import streamlit, access session_state
- Must: return dataclasses or plain dicts, raise or return errors cleanly
- Functions are pure and testable without Streamlit running

### DB Layer
- May import from: sql_adapter only
- Must NOT: import streamlit, contain business logic
- Must: use positional %s params, return plain List[Dict], log errors silently

## Current Status (Phase 7)

### ✅ Refactored
| File | Layer | Status |
|------|-------|--------|
| `billing/db/billing_queries.py` | DB | ✅ New — pure SQL |
| `billing/services/payment_service.py` | Service | ✅ New — pure logic |
| `billing/services/challan_service.py` | Service | ✅ New — pure logic |
| `core/search_engine.py` | Service | ✅ Done |
| `core/price_qty_governor.py` | Service | ✅ Done |
| `core/business_rules.py` | Service | ✅ Done |
| `core/kb_helpers.py` | UI util | ✅ Done |

### 🔄 Partially Refactored (UI layer, calls services)
| File | Issue | Next step |
|------|-------|-----------|
| `billing/payment_collection.py` | Still has `_q`, `_allocate` inline | Swap to service imports |
| `billing/challan_invoice_manager.py` | Mixes render_* with create_* | Extract render_* to billing/ui/ |
| `billing/bulk_order.py` | Good structure, some inline SQL | Move SQL to billing_queries |

### 🔴 Needs Refactor (too large, all layers mixed)
| File | Lines | Problem |
|------|-------|---------|
| `retail_punching.py` | 6553 | DB + UI + business logic all mixed |
| `wholesale_punching.py` | 3327 | Same |
| `backoffice/backoffice_ui.py` | 4241 | UI calling DB directly |
| `sql_adapter.py` | 2355 | UI helpers mixed with DB helpers |

## Migration Pattern

### Before (mixed):
```python
# In payment_collection.py
def _open_docs(party_id):
    rows = _q("SELECT * FROM invoices WHERE ...")  # SQL in UI file
    # ... business logic ...
    return docs

def _panel(shop, ptype):
    ost = _open_docs(pid)  # calls inline function
    if st.button("Record"):
        _record(...)  # more inline SQL
```

### After (layered):
```python
# In payment_collection.py (UI only)
from modules.billing.services import get_open_docs, allocate_payment, record_payment

def _panel(shop, ptype):
    ost = get_open_docs(pid)          # service call
    allocs, excess = allocate_payment(selected_docs, amount)
    if st.button("Record"):
        result = record_payment(...)  # service call
        if result.success:
            st.success(f"Cleared: {', '.join(result.cleared_docs)}")
```

## Import Conventions

```python
# DB layer — only in service files, never in UI
from modules.billing.db import get_open_invoices_for_party, update_invoice_balance

# Service layer — in UI files and other services
from modules.billing.services import get_open_docs, record_payment, allocate_payment

# Shared utilities — anywhere
from modules.core.price_qty_governor import compute_line_gst
from modules.core.search_engine import fuzzy_search_parties
from modules.core.kb_helpers import autofocus_scan, enter_to_submit
```
