from modules.loaders.smart.change_detector import _normalise

test_inputs = [True, False, "YES", "NO"]

for inp in test_inputs:
    result = _normalise(inp)
    print(f"_normalise({inp!r}) -> {result!r}")
