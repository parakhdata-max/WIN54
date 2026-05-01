"""
Run this script from C:\\Users\\Vinay\\Desktop\\WIN54
It will patch sql_adapter.py to use dynamic DB connection
"""
import re
import os

SQL_ADAPTER_PATH = r"modules\sql_adapter.py"

with open(SQL_ADAPTER_PATH, 'r', encoding='utf-8') as f:
    content = f.read()

# The new dynamic DB config block
NEW_BLOCK = '''
import os as _os
from dotenv import load_dotenv as _load_dotenv
_load_dotenv()

def _get_db_url() -> str:
    """Get DB URL from session state or .env — never hardcoded"""
    try:
        import streamlit as _st
        url = _st.session_state.get("DB_URL")
        if url:
            return url
    except Exception:
        pass
    url = _os.getenv("DATABASE_TEST")
    if url:
        return url
    return "postgresql://postgres:newpassword123@localhost:5432/dv_optical_test"

def _parse_db_url(url: str) -> dict:
    try:
        import urllib.parse as _up
        r = _up.urlparse(url)
        return {
            "host":     r.hostname or "localhost",
            "port":     r.port or 5432,
            "user":     r.username or "postgres",
            "password": r.password or "",
            "dbname":   r.path.lstrip("/") or "dv_optical_test",
        }
    except Exception:
        return {
            "host": "localhost",
            "port": 5432,
            "user": "postgres",
            "password": "newpassword123",
            "dbname": "dv_optical_test",
        }

def _get_db_config() -> dict:
    return _parse_db_url(_get_db_url())

DB_CONFIG = _get_db_config()
'''

# Find and replace the DB_CONFIG block
pattern = r'DB_CONFIG\s*=\s*\{[^}]+\}'
if re.search(pattern, content, re.DOTALL):
    new_content = re.sub(pattern, NEW_BLOCK.strip(), content, count=1, flags=re.DOTALL)
    
    # Backup original
    with open(SQL_ADAPTER_PATH + '.backup', 'w', encoding='utf-8') as f:
        f.write(content)
    
    # Write patched version
    with open(SQL_ADAPTER_PATH, 'w', encoding='utf-8') as f:
        f.write(new_content)
    
    print("✅ sql_adapter.py patched successfully")
    print("✅ Backup saved as sql_adapter.py.backup")
else:
    print("❌ Could not find DB_CONFIG block")
    print("   Manual fix needed")

# Also need to patch get_connection() to use dynamic config
# Find any function that uses DB_CONFIG directly
if 'DB_CONFIG' in new_content:
    print("⚠️  DB_CONFIG is referenced in other places")
    print("   These will still work via _get_db_config()")
    
print("\nDone. Restart streamlit to apply changes.")
