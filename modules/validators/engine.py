"""
modules/validators/engine.py
=============================
Validation Engine — runs all enabled rule validators.
"""
from .registry import RULE_REGISTRY
from .config_loader import load_validation_config


class ValidationEngine:

    def __init__(self):
        self.config = load_validation_config()

    def run(self, order_data: dict) -> list:
        results = []
        enabled = self.config.get("enabled_rules", {})

        for rule_name, is_enabled in enabled.items():
            if not is_enabled:
                continue
            validator_cls = RULE_REGISTRY.get(rule_name)
            if not validator_cls:
                continue
            try:
                # Some validators take config in __init__, others take no args.
                # Try with config first, fall back to no-args if that fails.
                try:
                    validator = validator_cls(self.config)
                except TypeError:
                    validator = validator_cls()
                result    = validator.validate(order_data)
                if hasattr(result, "to_dict"):
                    results.append(result.to_dict())
                elif isinstance(result, dict):
                    results.append(result)
                elif isinstance(result, list):
                    results.extend(r.to_dict() if hasattr(r, "to_dict") else r for r in result)
            except Exception as exc:
                results.append({
                    "rule": rule_name, "passed": False,
                    "severity": "CRITICAL",
                    "message": f"Validator {rule_name} crashed: {exc}",
                    "details": {"exception": str(exc)},
                })
        return results

    def run_structured(self, order_data: dict) -> dict:
        results  = self.run(order_data)
        errors   = []
        warnings = []
        is_valid = True
        for r in results:
            if not r.get("passed"):
                sev = str(r.get("severity", "")).upper()
                if sev in ("CRITICAL", "ERROR"):
                    is_valid = False
                    errors.append(r["message"])
                elif sev == "WARNING":
                    warnings.append(r["message"])
        return {
            "is_valid":     is_valid,
            "has_warnings": bool(warnings),
            "errors":       errors,
            "warnings":     warnings,
            "results":      results,
        }
