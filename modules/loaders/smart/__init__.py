"""
modules/loaders/smart/__init__.py
===================================
Clean exports for the smart loader subsystem.

Previously this file contained the full body of an OLD version of upload_guard.py
(without consume(), without one-time-use check, without _count_data_rows()).
Any code doing 'from modules.loaders.smart import check_upload' got the broken version.
Now fixed — all imports delegate to their correct modules.
"""

from modules.loaders.smart.upload_guard import (
    check_upload,
    get_flow_from_file,
    GuardResult,
)

from modules.loaders.smart.download_manager import (
    build_edit_download,
    build_add_template,
    make_edit_filename,
    make_add_filename,
    read_meta,
    FIELD_CONFIG,
    FINGERPRINT_EXPIRY_HOURS,
)

from modules.loaders.smart.change_detector import (
    detect_changes,
    ChangeReport,
    FieldChange,
    RISK_SAFE,
    RISK_CAUTION,
    RISK_WARNING,
    RISK_BLOCKED,
)

from modules.loaders.smart.change_approver import (
    apply_changes,
    rollback_by_backup_id,
    get_backup_list,
)

from modules.loaders.smart.ai_change_advisor import (
    advise,
    answer_question,
    Advice,
)

__all__ = [
    # upload_guard
    "check_upload", "get_flow_from_file", "GuardResult",
    # download_manager
    "build_edit_download", "build_add_template",
    "make_edit_filename", "make_add_filename",
    "read_meta", "FIELD_CONFIG", "FINGERPRINT_EXPIRY_HOURS",
    # change_detector
    "detect_changes", "ChangeReport", "FieldChange",
    "RISK_SAFE", "RISK_CAUTION", "RISK_WARNING", "RISK_BLOCKED",
    # change_approver
    "apply_changes", "rollback_by_backup_id", "get_backup_list",
    # ai_change_advisor
    "advise", "answer_question", "Advice",
]
