from config.validation_config import VALIDATION_CONFIG, is_party_blocked

# Test 1: Print config
print("Validation Config Loaded:")
print(f"Blocked Parties: {VALIDATION_CONFIG['BLOCKED_PARTIES']}")
print(f"High Value Threshold: ₹{VALIDATION_CONFIG['THRESHOLDS']['HIGH_VALUE_ORDER']:,}")

# Test 2: Test helper function
print(f"\nIs 'Test' blocked? {is_party_blocked('Test')}")

print("\n✅ Config loaded successfully!")