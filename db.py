import psycopg2
from psycopg2.extras import RealDictCursor
import streamlit as st

@st.cache_resource(hash_funcs={str: lambda x: x})
def get_connection(db_url: str):
    return psycopg2.connect(db_url)

def execute_query(query, params=None, fetch=False):
    db_url = st.session_state.get("DB_URL")
    if not db_url:
        st.error("DB_URL not set in session state.")
        return None

    conn = get_connection(db_url)

    # Reconnect if connection is closed
    if conn.closed:
        get_connection.clear()
        conn = get_connection(db_url)

    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        cur.execute(query, params)
        if fetch:
            result = cur.fetchall()
        else:
            result = None
        conn.commit()
        return result
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cur.close()