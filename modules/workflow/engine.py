# =====================================================
# WORKFLOW ENGINE
# Power → Allocation → Billing → Routing → Status
# =====================================================

import datetime
import streamlit as st


class WorkflowEngine:
    """
    Central workflow engine for order line processing

    Handles:
    - Power calculation
    - Stock allocation
    - Billing
    - Manufacturing routing
    - Status management
    """

    def __init__(self):
        pass

    # =====================================================
    # MAIN WORKFLOW RUNNER
    # =====================================================

    def trigger_complete_workflow(self, line: dict):
        """
        Runs full processing pipeline for one line:
        RX → Power → Allocation → Billing → Routing → Status
        """

        try:
            # Local imports (avoid circular dependency)
            from modules.backoffice_management import (
                update_manufacturing_power,
                update_batch_allocation,
                update_line_billing
            )

            # 1️⃣ Manufacturing Power
            is_cl = "contact" in str(line.get("main_group", "")).lower()

            update_manufacturing_power(line)

            # 2️⃣ Stock / Allocation
            update_batch_allocation(line)

            # 3️⃣ Billing
            update_line_billing(line)

            # 4️⃣ Manufacturing Routing
            self.route_manufacturing(line)

            # 5️⃣ Status Assignment
            self.assign_initial_status(line)

            # 6️⃣ Mark workflow complete
            line["workflow_done"] = True
            line["workflow_time"] = datetime.datetime.now().isoformat()

        except Exception as e:
            st.warning(f"⚠️ Workflow failed: {e}")

    # =====================================================
    # MANUFACTURING ROUTING
    # =====================================================

    def route_manufacturing(self, line: dict):
        """
        Decide where this line will be processed
        INTERNAL_LAB / EXTERNAL_LAB / VENDOR / STOCK
        """

        main_group = str(line.get("main_group", "")).lower()

        route = "UNKNOWN"

        if "contact" in main_group:
            route = "EXTERNAL_LAB"

        elif "ophthalmic" in main_group or "lens" in main_group:

            internal_ok = line.get("internal_capable", True)

            if internal_ok:
                route = "INTERNAL_LAB"
            else:
                route = "VENDOR"

        else:
            route = "STOCK_VENDOR"

        line["manufacturing_route"] = route
        line["routing_time"] = datetime.datetime.now().isoformat()

    # =====================================================
    # STATUS MANAGEMENT
    # =====================================================

    def assign_initial_status(self, line: dict):
        """
        Assign production status after routing
        """

        route = line.get("manufacturing_route", "UNKNOWN")
        batch_status = line.get("batch_status", "PENDING")

        if batch_status == "ALLOCATED":
            status = "READY_FOR_BILLING"

        elif route == "INTERNAL_LAB":
            status = "IN_PRODUCTION"

        elif route == "EXTERNAL_LAB":
            status = "SENT_TO_LAB"

        elif route == "VENDOR":
            status = "ORDERED_FROM_VENDOR"

        else:
            status = "PENDING"

        line["workflow_status"] = status
        line["status_time"] = datetime.datetime.now().isoformat()

    # =====================================================
    # JOB CARD PREPARATION
    # =====================================================

    def prepare_job_card_data(self, line: dict) -> dict:

        return {

            "order_id": line.get("order_id"),
            "product": line.get("product_name"),
            "brand": line.get("brand"),

            "eye": line.get("eye_side"),

            "sph": line.get("sph"),
            "cyl": line.get("cyl"),
            "axis": line.get("axis"),
            "add": line.get("add_power"),

            "sph_out": line.get("sph_out"),
            "cyl_out": line.get("cyl_out"),
            "axis_out": line.get("axis_out"),

            "lens_type": line.get("lens_type"),
            "diameter": line.get("diameter"),
            "frame_type": line.get("frame_type"),
            "fitting_height": line.get("fitting_height"),
            "base_curve": line.get("base_curve"),
            "frame_size": line.get("frame_size"),
            "corridor": line.get("corridor"),

            "qty": line.get("billing_qty"),

            "route": line.get("manufacturing_route"),

            "generated_at": datetime.datetime.now().isoformat()
        }

    # =====================================================
    # LAB ORDER PREPARATION
    # =====================================================

    def prepare_lab_order_data(self, line: dict) -> dict:

        return {

            "product": line.get("product_name"),
            "brand": line.get("brand"),
            "eye": line.get("eye_side"),

            "sph": line.get("sph_out"),
            "cyl": line.get("cyl_out"),
            "axis": line.get("axis_out"),
            "add": line.get("add_power"),

            "lens_type": line.get("lens_type"),
            "diameter": line.get("diameter"),
            "base_curve": line.get("base_curve"),

            "qty": line.get("order_qty"),

            "route": line.get("manufacturing_route"),

            "status": "LAB_PENDING",

            "created_at": datetime.datetime.now().isoformat()
        }


# =====================================================
# CENTRAL REFRESH HELPER (Single Source of Truth)
# =====================================================

# =====================================================
# DATABASE RELOAD HELPER (Single Source of Truth)
# =====================================================

def reload_order(order_no: str):
    """
    Reload full order from DB and refresh all lines
    ALWAYS uses workflow engine (single source of truth)
    """

    from modules.sql_adapter import fetch_full_order
    from modules.workflow.engine import refresh_line_state  # ✅ FIXED IMPORT

    order = fetch_full_order(order_no)

    if not order:
        return None

    for line in order.get("lines", []):
        refresh_line_state(line)

    return order

# =====================================================
# GLOBAL ENGINE INSTANCE
# =====================================================

workflow_engine = WorkflowEngine()
