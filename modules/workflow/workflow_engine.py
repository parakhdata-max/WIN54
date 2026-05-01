# modules/workflow/workflow_engine.py

from .transition_rules import can_move
from .history_engine import log_history
from typing import Dict, List, Optional


class WorkflowEngine:


    def move_line(self, line, new_status, user="system"):

        current = line.get("exec_status")

        if not current:
            raise Exception("Line has no current status")

        if not can_move(current, new_status):
            raise Exception(
                f"Invalid transition: {current} → {new_status}"
            )

        log_history(line, current, new_status, user)

        line["exec_status"] = new_status

        return line


    def initialize_line(self, line, route):

        if route == "STOCK":
            line["exec_status"] = "ALLOCATED"

        elif route == "INHOUSE":
            line["exec_status"] = "JOB_CREATED"

        elif route == "EXTERNAL":
            line["exec_status"] = "LAB_ORDERED"

        else:
            line["exec_status"] = "HOLD"

        line["history"] = []

        return line

"""
Workflow Engine Patch - Auto-Apply Pricing After Allocation

This patch adds automatic pricing calculation after batch allocation.

LOCATION: modules/workflow/engine.py

ADD THIS CODE to the refresh_line_state() function AFTER allocation logic completes.
"""

# ============================================================================
# PATCH FOR refresh_line_state() function
# ============================================================================

def refresh_line_state(line: Dict) -> Dict:
    """
    Refresh allocation, route, and pricing for a line
    
    WORKFLOW:
    1. Check stock availability
    2. Allocate from stock if available
    3. Route to vendor/lab if not available
    4. ✅ NEW: Auto-apply pricing
    5. Update line state
    """
    
    # ... existing allocation logic ...
    # (all your current allocation/routing code stays here)
    
    # ============================================================================
    # ✅ ADD THIS SECTION AT THE END OF refresh_line_state()
    # ============================================================================
    
    # Auto-apply pricing after allocation
    if line.get('batch_allocation') and len(line['batch_allocation']) > 0:
        try:
            from modules.backoffice.backoffice_logic import update_line_billing
            
            # Apply pricing to allocated quantity
            line = update_line_billing(line)
            
            print(f"✅ Pricing applied: {line.get('product_name')} → ₹{line.get('billing_total', 0):.2f}")
            
        except ImportError:
            # If backoffice_logic doesn't exist yet, try direct pricing
            try:
                from modules.pricing_engine import apply_pricing_line
                line.pop('pricing_applied_at', None)  # Allow re-pricing
                line = apply_pricing_line(line)
                line['billing_total'] = line.get('total_price', 0)
                print(f"✅ Direct pricing applied: {line.get('product_name')}")
            except Exception as e:
                print(f"⚠️ Pricing skipped for {line.get('product_name')}: {e}")
        
        except Exception as e:
            print(f"❌ Pricing error for {line.get('product_name')}: {e}")
    
    else:
        # No allocation = no pricing
        line['unit_price'] = 0
        line['billing_total'] = 0
        print(f"ℹ️ No allocation for {line.get('product_name')}, pricing cleared")
    
    return line




