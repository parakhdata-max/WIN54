from modules.loaders.smart.change_detector import _normalise

print("str(True):", str(True))
print("repr(str(True)):", repr(str(True)))
print("type(str(True)):", type(str(True)))
print()

# Test all code paths in _normalise
test_cases = [
    # Path 1: v is None
    (None, "None input"),
    
    # Path 2: s.lower() in ("nan", "none", "nat", "")
    ("nan", "nan string"),
    ("NaN", "NaN string"),
    ("NAN", "NAN string"),
    ("none", "none string"),
    ("None", "None string"),
    ("NONE", "NONE string"),
    ("nat", "nat string"),
    ("Nat", "Nat string"),
    ("NAT", "NAT string"),
    ("", "empty string"),
    ("   ", "whitespace string"),
    
    # Path 3: Normalise numeric - integer result
    ("42", "integer string"),
    ("42.0", "float that equals integer"),
    ("42.000", "float with trailing zeros"),
    ("-42", "negative integer"),
    ("-42.0", "negative float that equals integer"),
    
    # Path 4: Normalise numeric - float result
    ("3.14", "simple float"),
    ("3.140", "float with trailing zeros"),
    ("3.14000000", "float with many trailing zeros"),
    ("0.0", "zero float"),
    ("-3.14", "negative float"),
    ("-3.140", "negative float with trailing zeros"),
    
    # Path 5: ValueError/TypeError (non-numeric strings that aren't bool-like)
    ("hello", "regular string"),
    ("YES", "YES string (will be caught by bool check)"),
    ("yes", "yes lowercase"),
    ("No", "No mixed case"),
    ("FALSE", "FALSE uppercase"),
    ("false", "false lowercase"),
    ("True", "True mixed case"),
    ("true", "true lowercase"),
    
    # Path 6: Return original string (not None, not numeric, not bool-like)
    ("product_name", "regular field name"),
    ("batch_no", "another field name"),
    ("some random text", "text with spaces"),
]

print("Testing _normalise function:")
print("=" * 50)
for test_input, description in test_cases:
    try:
        result = _normalise(test_input)
        print(f"_normalise({test_input!r:15}) -> {result!r:15} ({description})")
    except Exception as e:
        print(f"_normalise({test_input!r:15}) -> ERROR: {e} ({description})")