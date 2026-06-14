"""
modules/printing/print_opener.py
Utility: write HTML to a safe temp file and open in the default browser.
"""
import os
import re
import tempfile
import webbrowser


def open_html_print(html: str, filename: str = "print.html") -> str:
    """
    Write html to a temp file and open in browser.

    filename is sanitised — slashes and illegal Windows chars replaced with '-'.
    This means invoice_no or challan_no values like "INV/2627/0015" are safe.
    """
    # Remove ALL path separators and Windows-illegal chars
    safe_name = re.sub(r'[/\\:*?"<>|]', '-', str(filename))
    safe_name = safe_name.strip('. ') or 'print.html'

    tmp_path = os.path.join(tempfile.gettempdir(), safe_name)

    with open(tmp_path, 'w', encoding='utf-8') as fh:
        fh.write(html)

    # file:///C:/... format works on all platforms
    uri = 'file:///' + tmp_path.replace('\\', '/')
    webbrowser.open(uri)
    return tmp_path
