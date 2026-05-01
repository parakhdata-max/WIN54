"""
Quantity Engine
===============

Central quantity logic for ERP.

• Decides input mode from product
• Generates UI schema
• Normalizes to PCS
• Validates quantity
• Works for Retail / Wholesale / Backoffice
"""

from typing import Dict


class QuantityEngine:


    def __init__(self, product: Dict):

        self.product = product

        self.unit = (product.get("unit") or "PCS").upper()
        val = product.get("allow_loose")

        # Safe normalize from DB / pandas / numpy
        if val in [True, "t", "true", "True", 1]:
            self.allow_loose = True
        else:
            self.allow_loose = False

        self.box_size = int(product.get("box_size") or 1)

        self.mode = self._detect_mode()


    # ----------------------------
    # MODE DETECTION
    # ----------------------------

    def _detect_mode(self) -> str:

        if self.unit == "BOX" and not self.allow_loose:
            return "BOX_ONLY"

        if self.unit == "BOX" and self.allow_loose:
            return "FLEX"

        if self.unit == "PCS":
            return "PCS_ONLY"

        if self.unit == "PAIR" and not self.allow_loose:
            return "PAIR_ONLY"

        if self.unit == "PAIR" and self.allow_loose:
            return "PAIR_FLEX"

        if self.unit == "NO":
            return "NO_ONLY"

        return "PCS_ONLY"


    # ----------------------------
    # UI SCHEMA
    # ----------------------------

    def get_ui_schema(self) -> Dict:
        """
        Tells UI what inputs to show
        """

        if self.mode == "BOX_ONLY":

            return {
                "box": True,
                "pcs": False,
                "pair": False,
                "box_step": 1,
                "label": "BOX only"
            }


        if self.mode == "PCS_ONLY":

            return {
                "box": False,
                "pcs": True,
                "pair": False,
                "pcs_step": 1,
                "label": "PCS only"
            }


        # -------- PAIR ONLY --------
        if self.mode == "PAIR_ONLY":

            return {
                "box": False,
                "pcs": False,
                "pair": True,

                "pair_step": 0.5,
                "pair_format": "%.1f",

                "label": "PAIR (0.5 = 1 Lens)"
            }


        # -------- PAIR + PCS --------
        if self.mode == "PAIR_FLEX":

            return {
                "box": False,
                "pcs": True,
                "pair": True,

                "pair_step": 0.5,
                "pcs_step": 1,
                "pair_format": "%.1f",

                "label": "PAIR + PCS"
            }


        # -------- BOX + PCS --------
        if self.mode == "FLEX":

            return {
                "box": True,
                "pcs": True,
                "pair": False,

                "box_step": 1,
                "pcs_step": 1,

                "label": "BOX + PCS"
            }


        # DEFAULT
        return {
            "box": False,
            "pcs": True,
            "pair": False,

            "pcs_step": 1,

            "label": "PCS only"
        }


    # ----------------------------
    # NORMALIZATION
    # ----------------------------

    def normalize(self, user_input: Dict) -> Dict:
        """
        Converts user input → final PCS
        """

        box = int(user_input.get("box", 0) or 0)
        pcs = int(user_input.get("pcs", 0) or 0)

        # IMPORTANT: pair must be float (for 0.5)
        pair = float(user_input.get("pair", 0) or 0)

        final_pcs = 0


        # BOX logic
        if self.mode in ["BOX_ONLY", "FLEX"]:
            final_pcs += box * self.box_size


        # PAIR logic
        if self.mode in ["PAIR_ONLY", "PAIR_FLEX"]:
            final_pcs += int(pair * 2)


        # PCS logic
        if self.mode in ["PCS_ONLY", "PAIR_FLEX", "FLEX"]:
            final_pcs += pcs


        return {
            "final_pcs": final_pcs,
            "mode": self.mode,

            "box": box,
            "pcs": pcs,
            "pair": pair,
        }


    # ----------------------------
    # VALIDATION
    # ----------------------------

    def validate(self, normalized: Dict) -> Dict:

        errors = []


        # Must be > 0
        if normalized["final_pcs"] <= 0:
            errors.append("Quantity must be greater than zero")


        # No loose in BOX ONLY
        if self.mode == "BOX_ONLY" and normalized["pcs"] > 0:
            errors.append("Loose PCS not allowed")


        # No loose in PAIR ONLY
        if self.mode == "PAIR_ONLY" and normalized["pcs"] > 0:
            errors.append("Loose PCS not allowed")


        # Validate half-pair steps
        if self.mode in ["PAIR_ONLY", "PAIR_FLEX"]:

            pair = normalized.get("pair", 0)

            # Must be multiple of 0.5
            if (pair * 2) % 1 != 0:
                errors.append("Pair quantity must be in steps of 0.5")


        return {
            "is_valid": len(errors) == 0,
            "errors": errors
        }


    # ----------------------------
    # ONE-SHOT PROCESS
    # ----------------------------

    def process(self, user_input: Dict) -> Dict:
        """
        Normalize + Validate in one call
        """

        normalized = self.normalize(user_input)

        validation = self.validate(normalized)

        return {
            **normalized,
            **validation
        }


    # ----------------------------
    # REVERSE (PCS → DISPLAY)
    # ----------------------------

    def reverse_from_pcs(self, pcs: int):

        try:
            pcs = int(pcs or 0)
        except:
            pcs = 0

        if pcs <= 0:
            return 0

        mode = self.mode

        # BOX modes
        if mode in ["BOX_ONLY", "FLEX"] and self.box_size > 0:
            return int(pcs // self.box_size)

        # PAIR modes
        if mode in ["PAIR_ONLY", "PAIR_FLEX"]:
            return float(pcs / 2)

        # PCS / NO modes
        return int(pcs)
