"""
start_scanner.py
================
Starts the Flask scanner PWA on port 8502 as a background thread.
Run this INSTEAD of scanner_app.py directly when you want both
Streamlit (8501) and Scanner (8502) running together.

Usage:
    Add to your app.py startup:
        from start_scanner import start_scanner_bg
        start_scanner_bg()

    OR run standalone:
        python start_scanner.py
"""
import threading, sys, os
import socket

ERP_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ERP_ROOT)


def lan_ips() -> list[str]:
    ips = []
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and not ip.startswith("169.254.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127.") and ip not in ips:
            ips.insert(0, ip)
    except Exception:
        pass
    return ips or ["127.0.0.1"]


def lan_urls(port: int = 8502) -> list[str]:
    return [f"http://{ip}:{port}" for ip in lan_ips()]


def start_scanner_bg(port: int = 8502):
    """Start Flask scanner in a background daemon thread."""
    def _run():
        try:
            from scanner_app import app
            app.run(host="0.0.0.0", port=port,
                    debug=False, use_reloader=False, threaded=True)
        except Exception as e:
            print(f"[Scanner] Failed to start: {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    print("[Scanner] Running at:")
    for url in lan_urls(port):
        print(f"  {url}  | LAN check: {url}/lan")
    return t


if __name__ == "__main__":
    print("=" * 55)
    print("  Parakh Scanner - Standalone")
    print("=" * 55)
    for url in lan_urls(8502):
        print(f"  Scanner:   {url}")
        print(f"  Admin:     {url}/admin")
        print(f"  LAN check: {url}/lan")
    print()
    print("  First mobile test: open /health or /lan")
    print("  If mobile fails but PC works: run Fix_Scanner_Firewall_Admin.bat as Administrator.")
    print("  Press Ctrl+C to stop")
    print("=" * 55)

    from scanner_app import app
    app.run(host="0.0.0.0", port=8502, debug=False, threaded=True)
