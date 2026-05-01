# Tests

All test files consolidated here.

| File | Origin | Purpose |
|------|--------|---------|
| test_config.py | config/test_config.py | Validation config tests |
| test_flow.py | modules/validators/tests/ | Validator flow tests |
| test_supplier_db.py | root | Supplier DB connection tests |

Run with:
```
python -m pytest tests/
```

Note: test_flow.py imports from modules/ui/ which only exists for testing.
