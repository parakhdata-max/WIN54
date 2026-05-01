"""
modules/backoffice/batch_manager.py
────────────────────────────────────
Compatibility shim — re-exports everything from the canonical
modules/batch_manager.py so that old imports like:

    from modules.backoffice.batch_manager import get_batches_fifo

continue to work without crashing.

All real logic lives in modules/batch_manager.py (one level up).
"""
from modules.batch_manager import *          # noqa: F401,F403
from modules.batch_manager import (          # explicit for IDE / linters
    get_product_type,
    get_available_stock,
    get_contact_lens_stock,
    get_ophthalmic_lens_stock,
    get_solution_stock,
    get_frame_stock,
    get_batches_fifo,
    allocate_batches_fifo,
    get_batch_allocation_summary,
    check_stock_availability,
    update_batch_allocation,
    eye_side_matches,
    _force_float_df,
    _normalise_prices,
)
