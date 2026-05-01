"""
Test _normalise and _values_differ for boolean comparison:
Uploaded row has IsActive='NO', DB row has is_active=True (Python bool from psycopg2 RealDictCursor).
"""

import sys
sys.path.insert(0, 'C:/Users/Vinay/Desktop/WIN54')

from modules.loaders.smart.change_detector import _normalise, _values_differ


def test_normalise_bool_from_uploaded_string():
    """_normalise converts uploaded 'NO' string to 'NO' (uppercase)."""
    assert _normalise('NO') == 'NO'
    assert _normalise('No') == 'NO'
    assert _normalise('no') == 'NO'
    assert _normalise('YES') == 'YES'
    assert _normalise('TRUE') == 'TRUE'
    assert _normalise('FALSE') == 'FALSE'


def test_normalise_bool_from_db_true():
    """_normalise converts DB bool True to 'TRUE'."""
    # psycopg2 RealDictCursor returns Python bool for boolean columns
    assert _normalise(True) == 'TRUE'
    assert _normalise(False) == 'FALSE'


def test_values_differ_no_vs_true():
    """
    Uploaded row: IsActive='NO' (string from file)
    DB row: is_active=True (Python bool from psycopg2 RealDictCursor)
    
    After normalisation:
      - uploaded 'NO' -> 'NO'
      - DB True -> 'TRUE'
    
    _values_differ should detect these as different.
    """
    uploaded_val = _normalise('NO')
    db_val = _normalise(True)  # psycopg2 returns Python bool

    assert uploaded_val == 'NO'
    assert db_val == 'TRUE'
    assert _values_differ(uploaded_val, db_val) is True


def test_values_differ_yes_vs_true():
    """Uploaded 'YES' and DB True should be considered different (not coerced)."""
    assert _values_differ(_normalise('YES'), _normalise(True)) is True


def test_values_differ_no_vs_false():
    """Uploaded 'NO' and DB False should be considered different (not coerced)."""
    assert _values_differ(_normalise('NO'), _normalise(False)) is True


def test_values_differ_yes_vs_false():
    """Uploaded 'YES' and DB False should be different."""
    assert _values_differ(_normalise('YES'), _normalise(False)) is True


def test_values_differ_same_bool_strings():
    """Same boolean strings should not differ."""
    assert _values_differ(_normalise('YES'), _normalise('YES')) is False
    assert _values_differ(_normalise('NO'), _normalise('NO')) is False
    assert _values_differ(_normalise('TRUE'), _normalise('TRUE')) is False
    assert _values_differ(_normalise('FALSE'), _normalise('FALSE')) is False


def test_values_differ_same_bool_values():
    """Same boolean values should not differ."""
    assert _values_differ(_normalise(True), _normalise(True)) is False
    assert _values_differ(_normalise(False), _normalise(False)) is False


def test_detect_changes_bool_flow():
    """
    Complete flow test: DataFrame with IsActive='NO' (after apply_column_map),
    DB row with is_active=True -> detect_changes should detect the change.
    """
    import pandas as pd
    from unittest.mock import patch, MagicMock

    from modules.loaders.smart.change_detector import detect_changes, FIELD_RISK

    # Step 1: Create DataFrame simulating uploaded file data
    # The file has header "IsActive" with value "NO" — after column map becomes is_active='NO'
    # Using column names that survive normalization in apply_column_map
    df = pd.DataFrame({
        "Product": ["Test Lens"],
        "IsActive": ["NO"],
    })

    # Step 2: Simulate column map (IsActive -> is_active)
    # Note: apply_column_map inside detect_changes will normalize column names
    # 'product_name' -> 'productname', 'is_active' -> 'isactive'
    # Then it matches 'isactive' against col_map keys and renames to 'is_active'
    from modules.loaders.universal_loader_core import apply_column_map

    df_mapped = apply_column_map(df, {"IsActive": "is_active"}, file_type="PRODUCT")
    # df_mapped now has: ['product_name', 'is_active']

    # Step 3: Mock DB lookup to return a row with is_active=True (Python bool)
    mock_db_row = {
        "_id": "test-id-001",
        "product_name": "Test Lens",
        "is_active": True,  # psycopg2 RealDictCursor returns Python bool
    }

    # Step 4: Mock FIELD_CONFIG.get for "PRODUCT" type
    # key_col must resolve to a column that exists in df after normalization
    # After apply_column_map inside detect_changes, df has ['productname', 'is_active']
    # So we use 'productname' as key_col (the normalized version of product_name)
    mock_cfg = {
        "locked_cols": [],
        "columns": ["is_active"],
        "key_col": "productname",  # Normalized version of product_name
    }

    mock_field_config = MagicMock()
    mock_field_config.get.return_value = mock_cfg

    with patch("modules.loaders.smart.download_manager.FIELD_CONFIG", mock_field_config), \
         patch("modules.loaders.smart.change_detector._build_db_lookup") as mock_lookup:

        mock_lookup.return_value = {"Test Lens": mock_db_row}

        # Step 5: Run detect_changes
        report = detect_changes(df_mapped, "PRODUCT")

    # Step 6: Assertions
    assert report.has_changes is True, f"Expected at least one change detected, got changes={len(report.changes)}, blocked={len(report.blocked)}, rows_not_found={report.rows_not_found}"
    assert len(report.changes) == 1, f"Expected exactly 1 change, got {len(report.changes)}"

    change = report.changes[0]
    assert change.field_name == "is_active"
    assert change.old_value == "TRUE"   # _normalise(True) -> 'TRUE'
    assert change.new_value == "NO"     # _normalise('NO') -> 'NO'
    assert change.risk_level == FIELD_RISK["is_active"]  # RISK_SAFE
    assert _values_differ("NO", "TRUE") is True


if __name__ == '__main__':
    test_normalise_bool_from_uploaded_string()
    test_normalise_bool_from_db_true()
    test_values_differ_no_vs_true()
    test_values_differ_yes_vs_true()
    test_values_differ_no_vs_false()
    test_values_differ_yes_vs_false()
    test_values_differ_same_bool_strings()
    test_values_differ_same_bool_values()
    test_detect_changes_bool_flow()
    print("All tests passed!")
