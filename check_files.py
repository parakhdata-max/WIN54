import os
os.chdir(r"C:\Users\Vinay\Desktop\WIN54")

files = [
    ("modules/retail_punching.py",          "def render_retail_punching"),
    ("app.py",                               "_erp_mode = st.session_state.get"),
    ("modules/consultation.py",              "_editing_consult_order_id"),
    ("modules/backoffice/order_edit_view.py","CONSULT_EDIT"),
]

for path, pattern in files:
    try:
        src = open(path, encoding="utf-8", errors="replace").read()
        ok  = pattern in src
        print(("NEW FILE ✅" if ok else "OLD FILE ❌") + "  " + path)
    except Exception as e:
        print(f"ERROR: {path} — {e}")
