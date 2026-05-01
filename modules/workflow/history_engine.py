# modules/workflow/history_engine.py

import datetime


def log_history(obj, from_state, to_state, user="system"):

    if "history" not in obj:
        obj["history"] = []

    obj["history"].append({
        "time": datetime.datetime.now().isoformat(),
        "from": from_state,
        "to": to_state,
        "by": user
    })
