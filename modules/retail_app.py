"""
retail_app.py

Streamlit entry point for the Retail (OPD / Counter) Order Desk.

Run with:
    streamlit run retail_app.py

Or register as a page in a multi-page app:
    pages/retail.py  →  from retail_app import main; main()

Note:
    Session initialisation, crash recovery, and cart persistence all happen
    inside RetailPlugin.header() — the engine guarantees that runs first.
"""

from modules.core.punch_engine import render_engine
from modules.plugins.retail_plugin import RetailPlugin


def main():
    render_engine(RetailPlugin())


if __name__ == "__main__":
    main()
