import streamlit as st
import copy
import json

# Keys that are safe and useful to snapshot (skip large/unserializable blobs)
_SNAPSHOT_KEYS = {
    "retail_patient_id", "retail_patient_name", "retail_patient_mobile",
    "retail_case_no", "retail_selected_visit_id",
    "retail_new_rx_r", "retail_new_rx_l",
    "retail_old_rx_r", "retail_old_rx_l",
    "retail_selected_product",
    "retail_pending_eyes",
    "retail_show_batch_editor",
}

_MAX_LOG_ENTRIES = 10  # keep only last 10 steps, not unbounded history

def record_step(tag="step"):
    log = st.session_state.setdefault("_replay_log", [])

    # Snapshot only lightweight known-safe keys — never the whole session_state
    snapshot = {}
    for k in _SNAPSHOT_KEYS:
        v = st.session_state.get(k)
        if v is None:
            continue
        try:
            snapshot[k] = copy.deepcopy(v)
        except Exception:
            snapshot[k] = str(v)

    log.append({"tag": tag, "state": snapshot})

    # Trim to avoid unbounded growth
    if len(log) > _MAX_LOG_ENTRIES:
        del log[:-_MAX_LOG_ENTRIES]

def export_replay():
    data = st.session_state.get("_replay_log", [])
    return json.dumps(data, default=str)

def import_replay(json_data):
    try:
        steps = json.loads(json_data)
        if steps:
            st.session_state.update(steps[-1]["state"])
    except:
        pass
