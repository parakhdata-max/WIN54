"""
scanner_app.py
==============
Lightweight Flask PWA for mobile barcode scanning.
Run: python scanner_app.py
Accessible at: http://192.168.1.10:8502

Install as PWA:
  Chrome / Edge → ⋮ → Add to Home Screen
  Safari        → Share → Add to Home Screen

Features:
  - Attendance: scan staff card + IN/OUT barcode
  - Production stage: scan staff card + order no + stage barcode
  - No login required — barcode IS the identity
  - Works on any WiFi-connected phone
  - Offline-capable after first load (service worker)
"""

import sys, os
import socket

# Add ERP root to path so we can import modules
ERP_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ERP_ROOT)

from flask import Flask, request, jsonify, render_template_string
import json, datetime, re

app = Flask(__name__)
app.secret_key = "parakh_scanner_2026"
APP_VERSION = "2026-06-13-staff-report-v11"


def _lan_ips() -> list[str]:
    """Return likely LAN IPv4 addresses for mobile access."""
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


def _lan_urls(port: int = 8502) -> list[str]:
    return [f"http://{ip}:{port}" for ip in _lan_ips()]


def _safe_print(text: str = "") -> None:
    """Print only when a console is available; ignore Windows console encoding/no-console issues."""
    try:
        print(text)
    except Exception:
        pass


@app.after_request
def add_no_cache_headers(response):
    if request.path == "/" or request.path.startswith("/api/") or request.path in ("/sw.js", "/manifest.json"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# ── Load ERP modules ───────────────────────────────────────────────────────────
def _eng():
    from modules.hr.hr_scanner_engine import (
        ensure_scanner_schema,
        get_employee_by_barcode,
        process_scan,
        process_stage_scan,
        get_today_stage_log,
        get_unclosed_staff,
        admin_clear_unclosed,
        PRODUCTION_STAGES,
        STAGE_MAP,
    )
    ensure_scanner_schema()
    return {
        "get_emp":          get_employee_by_barcode,
        "process_scan":     process_scan,
        "process_stage":    process_stage_scan,
        "today_log":        get_today_stage_log,
        "unclosed":         get_unclosed_staff,
        "clear_unclosed":   admin_clear_unclosed,
        "stages":           PRODUCTION_STAGES,
        "stage_map":        STAGE_MAP,
    }


# ══════════════════════════════════════════════════════════════════════════════
# PWA MANIFEST + SERVICE WORKER
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name":             "Parakh Scanner",
        "short_name":       "Scanner",
        "description":      "Staff attendance & production stage scanner",
        "start_url":        f"/?v={APP_VERSION}",
        "display":          "standalone",
        "background_color": "#0a0f1e",
        "theme_color":      "#3b82f6",
        "orientation":      "portrait",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ]
    })


@app.route("/sw.js")
def service_worker():
    """Service worker — network-first so mobile gets scanner updates."""
    js = f"""
const CACHE = 'parakh-scanner-{APP_VERSION}';

self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => {{
    e.waitUntil(
        caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    );
    self.clients.claim();
}});

self.addEventListener('fetch', e => {{
    if (e.request.url.includes('/api/') || e.request.url.includes('/sw.js')) return;
    e.respondWith(
        fetch(e.request, {{cache: 'no-store'}}).catch(() => caches.match(e.request))
    );
}});
"""
    from flask import Response
    return Response(js, mimetype="application/javascript")


@app.route("/reset")
def reset_mobile_cache():
    """Mobile rescue page: clear old PWA/service-worker cache and reload fresh."""
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reset Parakh Scanner</title>
<style>
body{{font-family:Arial,sans-serif;background:#0a0f1e;color:#e5e7eb;padding:22px;line-height:1.45}}
.box{{background:#111827;border:1px solid #334155;border-radius:12px;padding:16px;margin:12px 0}}
a,button{{background:#2563eb;color:white;border:0;border-radius:8px;padding:12px 14px;font-weight:700;text-decoration:none;display:inline-block}}
code{{background:#020617;padding:2px 5px;border-radius:4px}}
</style></head><body>
<h2>Resetting Scanner...</h2>
<div class="box" id="status">Clearing old mobile cache and service worker.</div>
<p><a href="/?v={APP_VERSION}&fresh=1">Open Scanner Manually</a></p>
<script>
(async function(){{
  const out = document.getElementById('status');
  try {{
    if ('serviceWorker' in navigator) {{
      const regs = await navigator.serviceWorker.getRegistrations();
      for (const r of regs) await r.unregister();
    }}
    if ('caches' in window) {{
      const keys = await caches.keys();
      for (const k of keys) await caches.delete(k);
    }}
    try {{
      localStorage.removeItem('staffId');
      localStorage.removeItem('staffName');
      localStorage.removeItem('staffRole');
      localStorage.removeItem('lastStaffBc');
    }} catch(e) {{}}
    out.innerHTML = '✅ Old cache cleared. Opening fresh scanner...';
    setTimeout(() => location.replace('/?v={APP_VERSION}&fresh=' + Date.now()), 700);
  }} catch(e) {{
    out.innerHTML = '⚠️ Reset partly failed: <code>' + String(e) + '</code><br>Tap Open Scanner Manually.';
  }}
}})();
</script>
</body></html>"""


@app.route("/icon-192.png")
@app.route("/icon-512.png")
def icon():
    """Simple SVG-based icon as PNG fallback."""
    # Returns a basic blue square with "P" — replace with real icon if desired
    import io
    try:
        from PIL import Image, ImageDraw, ImageFont
        size = 192 if "192" in request.path else 512
        img  = Image.new("RGB", (size, size), "#1e40af")
        draw = ImageDraw.Draw(img)
        font_size = size // 2
        try:
            font = ImageFont.truetype("arial.ttf", font_size)
        except Exception:
            font = ImageFont.load_default()
        draw.text((size//4, size//8), "P", fill="#ffffff", font=font)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        buf.seek(0)
        from flask import send_file
        return send_file(buf, mimetype="image/png")
    except Exception:
        from flask import Response
        # 1x1 blue PNG fallback
        data = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x1f\x00\x00\x03\x01\x00\x18\xdd\x8e\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
        return Response(data, mimetype="image/png")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN PAGE
# ══════════════════════════════════════════════════════════════════════════════

MAIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="theme-color" content="#0d1117">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Scanner">
<link rel="manifest" href="/manifest.json">
<title>Parakh Scanner</title>
<style>
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
:root{
  --bg:#0d1117;--surface:#161b22;--surface2:#21262d;
  --border:#30363d;--blue:#2f81f7;--green:#3fb950;
  --red:#f85149;--yellow:#e3b341;--orange:#fb8500;
  --text:#e6edf3;--muted:#8b949e;--radius:12px;
}
html,body{height:100%;overflow:hidden}
body{
  background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',sans-serif;
  display:flex;flex-direction:column;height:100vh;
}

/* ── Top bar ── */
.topbar{
  display:flex;align-items:center;justify-content:space-between;
  padding:12px 16px 8px;background:var(--surface);
  border-bottom:1px solid var(--border);flex-shrink:0;
}
.topbar-title{font-size:1rem;font-weight:700;color:var(--blue)}
.topbar-sub{font-size:.72rem;color:var(--muted)}
.staff-chip{
  background:var(--surface2);border:1px solid var(--border);
  border-radius:20px;padding:4px 12px;font-size:.72rem;
  color:var(--text);cursor:pointer;max-width:140px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.staff-chip.active{border-color:var(--green);color:var(--green)}
.top-actions{display:flex;align-items:center;gap:8px}
.top-refresh{
  background:var(--surface2);border:1px solid var(--border);
  border-radius:18px;color:var(--text);font-size:.8rem;
  padding:4px 9px;cursor:pointer;
}
.top-logout{
  background:#2d0f0f;border:1px solid #7f1d1d;
  border-radius:18px;color:#fca5a5;font-size:.76rem;
  padding:4px 9px;cursor:pointer;
}

/* ── Nav tabs ── */
.nav{
  display:flex;background:var(--surface);
  border-bottom:1px solid var(--border);flex-shrink:0;
}
.nav-btn{
  flex:1;padding:10px 4px;font-size:.7rem;font-weight:600;
  color:var(--muted);background:none;border:none;
  border-bottom:2px solid transparent;cursor:pointer;
  transition:all .15s;letter-spacing:.02em;
}
.nav-btn.active{color:var(--blue);border-bottom-color:var(--blue)}

/* ── Scrollable content ── */
.content{flex:1;overflow-y:auto;padding:12px 14px 24px;-webkit-overflow-scrolling:touch}

/* ── Cards ── */
.card{
  background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);padding:14px;margin-bottom:10px;
}
.card-label{
  font-size:.62rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.1em;color:var(--muted);margin-bottom:8px;
}

/* ── Scan inputs ── */
.scan-row{display:flex;gap:8px;align-items:center;margin-bottom:10px}
.scan-input{
  flex:1;background:var(--bg);border:1.5px solid var(--border);
  border-radius:10px;color:var(--text);font-size:.95rem;
  padding:13px 14px;outline:none;-webkit-appearance:none;
  transition:border-color .2s;
}
.scan-input:focus{border-color:var(--blue)}
.scan-input::placeholder{color:var(--muted);font-size:.85rem}
.scan-clear{
  background:var(--surface2);border:1.5px solid var(--border);
  border-radius:8px;color:var(--muted);padding:13px 12px;
  font-size:.85rem;cursor:pointer;flex-shrink:0;
}

/* ── Buttons ── */
.btn-row{display:flex;gap:8px;margin-bottom:8px}
.btn{
  flex:1;padding:14px;border:none;border-radius:10px;
  font-size:.9rem;font-weight:700;cursor:pointer;
  transition:opacity .15s,transform .1s;
}
.btn:active{opacity:.8;transform:scale(.97)}
.btn-blue  {background:var(--blue);  color:#fff}
.btn-green {background:var(--green); color:#000}
.btn-red   {background:var(--red);   color:#fff}
.btn-yellow{background:var(--yellow);color:#000}
.btn-ghost {background:var(--surface2);border:1.5px solid var(--border);color:var(--text)}

/* ── Stage grid ── */
.stage-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:4px}
.stage-tile{
  background:var(--surface2);border:1.5px solid var(--border);
  border-radius:10px;padding:14px 10px;text-align:center;
  cursor:pointer;transition:all .15s;
}
.stage-tile:active{background:var(--blue);border-color:var(--blue)}
.stage-tile.current{border-color:var(--green);background:#0d2818}
.stage-tile.selected{box-shadow:0 0 0 2px var(--blue)}
.stage-emoji{font-size:1.4rem;display:block;margin-bottom:4px}
.stage-name{font-size:.72rem;font-weight:600;color:var(--text);line-height:1.3}
.stage-sub {font-size:.62rem;color:var(--muted);margin-top:2px}

/* ── Toast / result ── */
.toast{
  position:fixed;bottom:20px;left:50%;transform:translateX(-50%) translateY(100px);
  min-width:280px;max-width:90vw;padding:14px 18px;
  border-radius:12px;font-size:.88rem;line-height:1.5;
  white-space:pre-line;text-align:center;
  transition:transform .3s cubic-bezier(.34,1.56,.64,1),opacity .3s;
  opacity:0;z-index:999;box-shadow:0 8px 32px rgba(0,0,0,.5);
}
.toast.show{transform:translateX(-50%) translateY(0);opacity:1}
.toast.success{background:#0d2818;border:1px solid var(--green);color:#7ee787}
.toast.error  {background:#2d0f0f;border:1px solid var(--red);  color:#ff7b72}
.toast.warning{background:#271d02;border:1px solid var(--yellow);color:#e3b341}
.toast.info   {background:#0c1a2e;border:1px solid var(--blue); color:#79c0ff}

/* ── Order card ── */
.order-pick-row{
  background:var(--surface);border:1.5px solid var(--border);
  border-radius:10px;padding:10px 12px;margin-bottom:6px;
  cursor:pointer;transition:all .15s;
}
.order-pick-row:active{border-color:var(--blue);background:var(--surface2)}
  background:var(--surface);border:1px solid var(--border);
  border-radius:10px;padding:12px;margin-bottom:8px;cursor:pointer;
  transition:border-color .15s;
}
.order-card:active{border-color:var(--blue)}
.order-card-top{display:flex;justify-content:space-between;align-items:flex-start}
.order-no{font-size:.85rem;font-weight:700;color:var(--blue)}
.order-stage{font-size:.7rem;padding:2px 8px;border-radius:10px;
             background:var(--surface2);color:var(--muted)}
.order-stage.active{background:#0d2818;color:var(--green);border:1px solid var(--green)}
.order-party{font-size:.78rem;color:var(--text);margin-top:4px}
.order-eyes {font-size:.72rem;color:var(--muted);margin-top:2px}

/* ── History ── */
.hist-item{
  display:flex;gap:10px;align-items:flex-start;
  padding:8px 0;border-bottom:1px solid var(--border);
}
.hist-item:last-child{border-bottom:none}
.hist-dot{
  width:8px;height:8px;border-radius:50%;
  background:var(--green);margin-top:5px;flex-shrink:0;
}
.hist-text{flex:1;font-size:.78rem}
.hist-time{font-size:.68rem;color:var(--muted);flex-shrink:0}
.report-controls{display:flex;gap:8px;align-items:center;margin-bottom:8px}
.report-date{
  flex:1;background:var(--bg);border:1.5px solid var(--border);
  border-radius:8px;color:var(--text);padding:10px;font-size:.82rem;
}
.report-summary{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:8px}
.report-metric{background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:8px;text-align:center}
.report-metric b{display:block;font-size:1rem;color:var(--text)}
.report-metric span{font-size:.62rem;color:var(--muted)}
.report-row{background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:8px;margin-bottom:6px}
.report-row-top{display:flex;justify-content:space-between;gap:8px;font-size:.78rem;font-weight:800;color:var(--blue)}
.report-row-sub{font-size:.68rem;color:var(--muted);margin-top:4px;line-height:1.35}
.report-repeat{color:var(--yellow);font-weight:800}

/* ── Spinner ── */
.spinner{
  border:2px solid var(--border);border-top:2px solid var(--blue);
  border-radius:50%;width:20px;height:20px;
  animation:spin .7s linear infinite;display:inline-block;
  vertical-align:middle;margin-left:8px;
}
@keyframes spin{to{transform:rotate(360deg)}}

/* ── Staff modal ── */
.modal-bg{
  position:fixed;inset:0;background:rgba(0,0,0,.8);
  display:flex;align-items:flex-end;z-index:100;
}
.modal{
  background:var(--surface);border-radius:20px 20px 0 0;
  padding:20px 16px 32px;width:100%;
}
.modal-handle{
  width:36px;height:4px;background:var(--border);
  border-radius:2px;margin:0 auto 16px;
}
.modal-title{font-size:1rem;font-weight:700;margin-bottom:14px;text-align:center}

/* ── Camera scan ── */
.cam-btn{
  background:var(--surface2);border:1.5px solid var(--border);
  border-radius:8px;color:var(--text);padding:11px 13px;
  font-size:1.1rem;cursor:pointer;flex-shrink:0;
  transition:all .15s;
}
.cam-btn:active{background:var(--blue);border-color:var(--blue)}
.cam-modal-bg{
  position:fixed;inset:0;background:rgba(0,0,0,.92);
  z-index:500;display:flex;flex-direction:column;
  align-items:center;justify-content:center;padding:16px;
}
.cam-modal{
  background:var(--surface);border-radius:16px;
  width:100%;max-width:380px;overflow:hidden;
}
.cam-header{
  display:flex;justify-content:space-between;align-items:center;
  padding:14px 16px;border-bottom:1px solid var(--border);
}
.cam-title{font-size:.95rem;font-weight:700;color:var(--text)}
.cam-close{background:none;border:none;color:var(--muted);
           font-size:1.2rem;cursor:pointer;padding:4px 8px}
#cam_video{width:100%;max-height:280px;object-fit:cover;background:#000;display:block}
.cam-overlay{
  position:relative;width:100%;
}
.cam-aim{
  position:absolute;top:50%;left:50%;
  transform:translate(-50%,-50%);
  width:200px;height:120px;
  border:2px solid var(--blue);border-radius:8px;
  box-shadow:0 0 0 2000px rgba(0,0,0,.45);
  pointer-events:none;
}
.cam-aim::before,.cam-aim::after{
  content:'';position:absolute;
  width:20px;height:20px;border-color:var(--blue);border-style:solid;
}
.cam-aim::before{top:-2px;left:-2px;border-width:3px 0 0 3px;border-radius:4px 0 0 0}
.cam-aim::after{bottom:-2px;right:-2px;border-width:0 3px 3px 0;border-radius:0 0 4px 0}
.cam-status{
  padding:10px 16px;font-size:.78rem;color:var(--muted);
  text-align:center;min-height:36px;
}
.cam-actions{
  display:flex;gap:8px;padding:10px 16px 14px;
}
/* Hidden file input for photo fallback */
#cam_file_input{display:none}
</style>
</head>
<body>

<!-- ── Top bar ── -->
<div class="topbar">
  <div>
    <div class="topbar-title">Parakh Eye Care</div>
    <div class="topbar-sub" id="time_str">--:--</div>
  </div>
  <div class="top-actions">
    <button class="top-refresh" onclick="refreshCurrentTab()" title="Refresh current screen">🔄</button>
    <button class="top-logout" onclick="logoutStaff()" title="Clear saved staff and login again">Logout</button>
    <div class="staff-chip" id="staff_chip" onclick="openStaffModal()">
      👤 Tap to login
    </div>
  </div>
</div>

<!-- ── Nav ── -->
<div class="nav">
  <button class="nav-btn active" onclick="showTab('home')"   id="nav_home">🏠 Home</button>
  <button class="nav-btn"        onclick="showTab('attend')" id="nav_attend">📍 Attend</button>
  <button class="nav-btn"        onclick="showTab('stage')"  id="nav_stage">⚙️ Stage</button>
  <button class="nav-btn"        onclick="showTab('orders')" id="nav_orders">📦 Orders</button>
</div>

<!-- ── Content ── -->
<div class="content">

  <!-- HOME TAB -->
  <div id="tab_home">
    <div class="card" id="home_status_card">
      <div class="card-label">Status</div>
      <div id="home_status" style="color:var(--muted);font-size:.85rem">
        Tap the chip above to login with your staff barcode.
      </div>
    </div>
    <div class="card" id="home_today_card" style="display:none">
      <div class="card-label">Today's Activity</div>
      <div id="home_today_log"></div>
    </div>
    <div class="card" id="home_report_card">
      <div class="card-label">Production Timing Report</div>
      <div class="report-controls">
        <input class="report-date" id="home_report_date" type="date" onchange="loadProductionReport()">
        <button class="btn btn-ghost" onclick="loadProductionReport()"
                style="flex:0 0 auto;width:auto;padding:10px 12px;font-size:.76rem">
          Refresh
        </button>
      </div>
      <div id="home_report_body">
        <div style="color:var(--muted);font-size:.78rem">Loading report...</div>
      </div>
    </div>
  </div>

  <!-- ATTEND TAB -->
  <div id="tab_attend" style="display:none">
    <div class="card">
      <div class="card-label">Check IN / OUT</div>
      <div class="scan-row">
        <input class="scan-input" id="att_bc" type="text"
               placeholder="Scan Staff barcode" autocomplete="off"
               autocorrect="off" autocapitalize="off"
               onkeydown="if(event.key==='Enter')doAttend('auto')">
        <button class="cam-btn" onclick="openCam('att_bc','📷 Scan Staff Barcode')" title="Scan barcode with camera">📷</button>
        <button class="scan-clear" onclick="document.getElementById('att_bc').value=''">✕</button>
      </div>
      <div class="btn-row">
        <button class="btn btn-green" onclick="doAttend('checkin')">📍 CHECK IN</button>
        <button class="btn btn-red"   onclick="doAttend('checkout')">🏁 CHECK OUT</button>
      </div>
      <button class="btn btn-blue" onclick="doLanVerify()"
              style="margin-bottom:8px"
              title="Use this on office WiFi to confirm physical presence">
        🏢 LAN Verify — Confirm Office Presence
      </button>
      <div style="font-size:.7rem;color:var(--muted);margin-bottom:6px;text-align:center">
        ↑ Tap this after mobile check-in, while on office WiFi
      </div>
    </div>
  </div>

  <!-- STAGE TAB -->
  <div id="tab_stage" style="display:none">

    <!-- Staff status bar — auto from login, no re-scan needed -->
    <div id="stg_status_bar" style="display:none;
         margin-bottom:8px;padding:8px 12px;
         background:#0d2818;border:1px solid var(--green);
         border-radius:10px;font-size:.78rem;color:var(--green);
         display:flex;align-items:center;justify-content:space-between">
      <span id="stg_status_name">—</span>
      <span id="stg_status_badge" style="font-size:.65rem;color:var(--muted)">—</span>
    </div>

    <!-- Hidden staff input — populated from localStorage -->
    <input type="hidden" id="stg_staff">

    <!-- SECTION 1: Order -->
    <div class="card" style="margin-bottom:10px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <div class="card-label" style="margin-bottom:0">📋 Select Order</div>
        <button onclick="refreshStageOrders()"
                style="background:none;border:none;color:var(--muted);
                       font-size:.75rem;cursor:pointer;padding:4px 8px">
          🔄 Refresh
        </button>
      </div>

      <!-- Order list — tappable rows -->
      <div id="stg_order_list">
        <div style="color:var(--muted);font-size:.78rem;text-align:center;padding:12px">
          Loading orders...
        </div>
      </div>

      <!-- Scan barcode fallback -->
      <div style="display:flex;gap:6px;margin-top:8px;align-items:center">
        <div style="flex:1;height:1px;background:var(--border)"></div>
        <span style="font-size:.62rem;color:var(--muted);white-space:nowrap">or scan barcode</span>
        <div style="flex:1;height:1px;background:var(--border)"></div>
      </div>
      <div style="display:flex;gap:6px;margin-top:8px">
        <input class="scan-input" id="stg_order" type="search"
               placeholder="📷 Scan job card"
               autocomplete="new-password" autocorrect="off"
               autocapitalize="off" inputmode="text" spellcheck="false"
               oninput="sanitiseOrderInput(this)"
               onkeydown="if(event.key==='Enter')previewOrder()"
               style="flex:1;font-size:.85rem">
        <button class="btn btn-blue" onclick="previewOrder()"
                style="flex:0 0 auto;padding:13px 16px;width:auto">🔍</button>
        <button class="scan-clear"
                onclick="clearSelectedOrder()">✕</button>
      </div>

      <!-- Order preview -->
      <div id="stg_order_preview" style="display:none;margin-top:8px"></div>
    </div>

    <!-- SECTION 2: Stage -->
    <div class="card">
      <div class="card-label" style="margin-bottom:10px">⚡ Tap Stage</div>
      <div class="scan-row" style="margin-bottom:8px">
        <input class="scan-input" id="stg_stage_bc" type="text"
               placeholder="Or scan wall barcode"
               autocomplete="off" autocorrect="off" autocapitalize="off"
               onkeydown="if(event.key==='Enter')doStageScan()"
               style="font-size:.85rem">
        <button class="cam-btn" onclick="openCam('stg_stage_bc','📷 Scan Stage Barcode')" title="Scan wall barcode">📷</button>
        <button class="scan-clear"
                onclick="document.getElementById('stg_stage_bc').value=''">✕</button>
      </div>
      <div class="stage-grid" id="stage_grid"></div>
    </div>

  </div>

  <!-- ORDERS TAB -->
  <div id="tab_orders" style="display:none">
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <div class="card-label" style="margin-bottom:0">Orders in Production</div>
        <button class="btn btn-ghost" onclick="loadOrders()"
                style="flex:0 0 auto;padding:7px 14px;font-size:.75rem;width:auto">
          🔄 Refresh
        </button>
      </div>
      <div class="scan-row" style="margin-bottom:6px">
        <input class="scan-input" id="ord_search" type="text"
               placeholder="Search order / party name"
               oninput="filterOrders(this.value)">
        <button class="scan-clear" onclick="document.getElementById('ord_search').value='';filterOrders('')">✕</button>
      </div>
    </div>
    <div id="orders_list">
      <div style="color:var(--muted);font-size:.82rem;text-align:center;padding:20px">
        Loading orders...
      </div>
    </div>
  </div>


<!-- ── Camera Scan Modal ── -->
<div class="cam-modal-bg" id="cam_modal" style="display:none">
  <div class="cam-modal">
    <div class="cam-header">
      <span class="cam-title" id="cam_modal_title">📷 Scan Barcode</span>
      <button class="cam-close" onclick="closeCamModal()">✕</button>
    </div>
    <div class="cam-overlay">
      <video id="cam_video" autoplay playsinline muted></video>
      <div class="cam-aim"></div>
    </div>
    <div class="cam-status" id="cam_status">Initialising camera...</div>
    <div class="cam-actions">
      <button class="btn btn-ghost" onclick="triggerFileInput()"
              style="flex:0 0 auto;width:auto;padding:12px 16px;font-size:.8rem">
        🖼️ Photo
      </button>
      <button class="btn btn-blue" onclick="closeCamModal()" id="cam_cancel_btn">
        Cancel
      </button>
    </div>
  </div>
  <!-- Photo fallback -->
  <input type="file" id="cam_file_input" accept="image/*" capture="environment"
         onchange="decodeFromPhoto(this)">
</div>

</div><!-- /content -->

<!-- ── Toast ── -->
<div class="toast" id="toast"></div>

<!-- ── Staff Modal ── -->
<div class="modal-bg" id="staff_modal" style="display:none" onclick="closeStaffModal(event)">
  <div class="modal">
    <div class="modal-handle"></div>
    <div class="modal-title">Login with Staff Barcode</div>
    <div class="scan-row">
      <input class="scan-input" id="modal_bc" type="text"
             placeholder="Scan your ID card barcode"
             autocomplete="off" autocorrect="off" autocapitalize="off"
             autofocus
             onkeydown="if(event.key==='Enter')loginFromModal()">
      <button class="cam-btn" onclick="openCam('modal_bc','📷 Scan Staff Barcode')"
              style="padding:13px">📷</button>
      <button class="btn btn-blue" style="flex:0 0 auto;padding:13px 16px"
              onclick="loginFromModal()">Go</button>
    </div>
    <div id="modal_result" style="color:var(--muted);font-size:.8rem;text-align:center;margin-top:8px"></div>
  </div>
</div>

<script>
// ── State ───────────────────────────────────────────────────────────────────
let staffId   = localStorage.getItem('staffId')   || '';
let staffName = localStorage.getItem('staffName') || '';
let staffRole = localStorage.getItem('staffRole') || '';
let allOrders = [];
let staffCanWork = false;
let attendanceMessage = 'Check in from office premises to unlock production.';
let stgStaffId = staffId;
let stgStaffName = staffName;

// ── PWA ──────────────────────────────────────────────────────────────────────
if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js?v={{ app_version }}').then(reg => {
        if (reg && reg.update) reg.update();
    }).catch(()=>{});
}

// ── Clock ────────────────────────────────────────────────────────────────────
function tickClock() {
    const now = new Date();
    const t = now.toLocaleTimeString('en-IN',{hour:'2-digit',minute:'2-digit'});
    const d = now.toLocaleDateString('en-IN',{weekday:'short',day:'numeric',month:'short'});
    document.getElementById('time_str').textContent = d + '  ' + t;
}
tickClock();
setInterval(tickClock, 30000);

// ── Tab switching ────────────────────────────────────────────────────────────
const TAB_IDS = ['home','attend','stage','orders'];
let currentTab = 'home';
function showTab(name) {
    currentTab = name;
    TAB_IDS.forEach(t => {
        document.getElementById('tab_'+t).style.display  = t===name ? '' : 'none';
        document.getElementById('nav_'+t).classList.toggle('active', t===name);
    });
    if (name === 'orders') loadOrders();
    if (name === 'stage')  {
        if (allOrders.length) renderStageOrders(allOrders);
        else refreshStageOrders();
    }
    if (name === 'home') {
        if (staffId) loadHomeLog();
        loadProductionReport();
    }
}

function refreshCurrentTab() {
    if (staffId) loadAttendanceStatus();
    if (currentTab === 'orders') {
        loadOrders();
        toast('success', '🔄 Orders refreshed');
    } else if (currentTab === 'home') {
        loadHomeLog();
        loadProductionReport();
        toast('success', '🔄 Home refreshed');
    } else if (currentTab === 'stage') {
        const order = document.getElementById('stg_order').value.trim()
                   .replace(/-[RrLl]$/, '')   // strip eye suffix -R / -L
                   .replace(/[-][0-9]$/, '')     // strip -1 / -2
                   .replace(/-$/, '');          // strip bare trailing dash
        if (order) previewOrder();
        toast('success', '🔄 Stage screen refreshed');
    } else {
        toast('success', '🔄 Refreshed');
    }
}

function logoutStaff() {
    try {
        localStorage.removeItem('staffId');
        localStorage.removeItem('staffName');
        localStorage.removeItem('staffRole');
        localStorage.removeItem('lastStaffBc');
    } catch(e) {}
    staffId = '';
    staffName = '';
    staffRole = '';
    staffCanWork = false;
    attendanceMessage = 'Scan staff barcode first, then check in from office premises.';
    stgStaffId = '';
    stgStaffName = '';
    const ids = ['att_bc','stg_staff','stg_order','stg_stage_bc'];
    ids.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
    });
    allOrders = [];
    updateChip();
    setStageControlsLocked(true);
    const home = document.getElementById('home_status');
    if (home) home.textContent = 'Logged out. Tap the staff chip and scan staff barcode again.';
    const modalResult = document.getElementById('modal_result');
    if (modalResult) modalResult.innerHTML = '';
    setTimeout(() => location.replace('/?v={{ app_version }}&logout=' + Date.now()), 350);
}

// Keep the mobile PWA fresh while it is sitting open on a counter/phone.
setInterval(() => {
    if (!staffId) return;
    refreshCurrentTab();
}, 5 * 60 * 1000);

// ── Staff chip ───────────────────────────────────────────────────────────────
function updateChip() {
    const chip = document.getElementById('staff_chip');
    if (staffName) {
        chip.textContent = '✅ ' + staffName.split(' ')[0];
        chip.className = 'staff-chip active';
        document.getElementById('home_status').textContent =
            `👋 Welcome, ${staffName}\nRole: ${staffRole || '—'}\nCheck IN from Attend tab.`;
        // Update stage tab status bar
        const bar  = document.getElementById('stg_status_bar');
        const name = document.getElementById('stg_status_name');
        const badge= document.getElementById('stg_status_badge');
        if (bar && name && badge) {
            name.textContent  = '✅ ' + staffName;
            badge.textContent = staffRole || '';
            bar.style.display = 'flex';
        }
        // Auto-fill hidden staff field
        const lastBc = localStorage.getItem('lastStaffBc') || '';
        if (lastBc) document.getElementById('stg_staff').value = lastBc;
        stgStaffId   = staffId;
        stgStaffName = staffName;
    } else {
        chip.textContent = '👤 Tap to login';
        chip.className = 'staff-chip';
        const bar = document.getElementById('stg_status_bar');
        if (bar) bar.style.display = 'none';
    }
}
updateChip();

function renderWorkLocked(tabName) {
    const msg = attendanceMessage || 'Office check-in required.';
    if (tabName === 'orders') {
        document.getElementById('orders_list').innerHTML =
            `<div style="color:var(--yellow);font-size:.86rem;text-align:center;padding:22px;line-height:1.45">
                🔒 ${escapeHtml(msg)}<br><br>
                Go to Attend tab → allow location → CHECK IN.
             </div>`;
    }
    if (tabName === 'stage') {
        setStageControlsLocked(true);
        const preview = document.getElementById('stg_order_preview');
        preview.style.display = 'block';
        preview.innerHTML =
            `<div style="color:var(--yellow);font-size:.82rem;padding:10px;background:var(--surface2);border-radius:8px;line-height:1.45">
                🔒 ${escapeHtml(msg)}<br>
                Stage buttons unlock after valid office check-in.
             </div>`;
    }
}

function setStageControlsLocked(locked) {
    ['stg_order','stg_stage_bc'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.disabled = !!locked;
    });
    const grid = document.getElementById('stage_grid');
    if (grid) {
        grid.style.opacity = locked ? '0.35' : '1';
        grid.style.pointerEvents = locked ? 'none' : 'auto';
    }
}

function loadAttendanceStatus() {
    if (!staffId) {
        staffCanWork = false;
        attendanceMessage = 'Scan staff barcode first, then check in from office premises.';
        return Promise.resolve(false);
    }
    return fetch('/api/attendance_status?staff_id=' + encodeURIComponent(staffId))
        .then(r => r.json())
        .then(d => {
            staffCanWork = !!d.ok;
            attendanceMessage = d.message || (staffCanWork ? 'Office check-in verified.' : 'Office check-in required.');
            setStageControlsLocked(!staffCanWork);
            updateChip();
            return staffCanWork;
        })
        .catch(() => {
            staffCanWork = false;
            attendanceMessage = 'Could not verify attendance. Check network and try again.';
            updateChip();
            return false;
        });
}

function getGeoForAttendance() {
    return new Promise((resolve) => {
        // GPS is optional — WiFi presence already confirms on-premises.
        // Chrome blocks geolocation on plain http:// (non-HTTPS, non-localhost).
        // We resolve with empty geo rather than blocking attendance.
        if (!navigator.geolocation) {
            resolve({});  // no GPS support — proceed without
            return;
        }
        navigator.geolocation.getCurrentPosition(
            pos => resolve({
                lat: pos.coords.latitude,
                lng: pos.coords.longitude,
                acc: pos.coords.accuracy || 0
            }),
            _err => resolve({}),  // GPS denied or blocked — proceed without
            {enableHighAccuracy: false, timeout: 5000, maximumAge: 60000}
        );
    });
}

function escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, ch => ({
        '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
    }[ch]));
}

// ── Staff modal ──────────────────────────────────────────────────────────────
function openStaffModal() {
    document.getElementById('staff_modal').style.display = 'flex';
    setTimeout(() => document.getElementById('modal_bc').focus(), 100);
}
function closeStaffModal(e) {
    if (e.target === document.getElementById('staff_modal')) {
        document.getElementById('staff_modal').style.display = 'none';
    }
}
function loginFromModal() {
    const bc = document.getElementById('modal_bc').value.trim();
    if (!bc) return;
    fetch('/api/lookup_staff?barcode=' + encodeURIComponent(bc))
        .then(r => r.json())
        .then(d => {
            if (d.found) {
                staffId   = d.id;
                staffName = d.name;
                staffRole = d.role;
                localStorage.setItem('staffId',   staffId);
                localStorage.setItem('staffName', staffName);
                localStorage.setItem('staffRole', staffRole);
                localStorage.setItem('lastStaffBc', bc);
                document.getElementById('staff_modal').style.display = 'none';
                document.getElementById('modal_bc').value = '';
                document.getElementById('stg_staff').value = bc;
                updateChip();
                loadAttendanceStatus().then(() => loadHomeLog());
                toast('success', '✅ Logged in as ' + staffName);
                loadMyStages();  // rebuild stage grid for this employee
            } else {
                const hint = d.hint || ('Barcode not found: ' + (d.scanned || bc));
                document.getElementById('modal_result').innerHTML =
                    '<span style="color:var(--red)">❌ ' + hint + '</span>';
            }
        });
}

// ── Attendance ───────────────────────────────────────────────────────────────
function doAttend(action) {
    const bc = document.getElementById('att_bc').value.trim() || localStorage.getItem('lastStaffBc') || '';
    if (!bc && !staffId) { toast('warning', '⚠️ Scan your staff barcode first'); return; }

    const finalAction = action === 'auto' ? 'checkin' : action;
    toast('info', finalAction === 'checkin' ? '📍 Recording check in...' : '🏁 Recording check out...');

    getGeoForAttendance()
    .then(geo => {
        const payload = { staff_barcode: bc, staff_id: staffId, action: finalAction, ...geo };
        return fetch('/api/attend', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify(payload)
        });
    })
    .then(r => r.json())
    .then(d => {
        const type = d.action === 'BLOCKED' ? 'warning' : (d.success ? 'success' : 'error');
        toast(type, d.message);
        if (d.success) {
            document.getElementById('att_bc').value = '';
            staffCanWork = d.action === 'CHECKIN';
            loadAttendanceStatus().then(() => {
                loadHomeLog();
                if (currentTab === 'orders') loadOrders();
            });
        }
    })
    .catch(err => toast('error', '❌ ' + (err.message || 'Network/location error')));
}

// ── LAN Verify ───────────────────────────────────────────────────────────────
function doLanVerify() {
    // Get barcode from field OR localStorage — don't crash if field missing
    const attEl = document.getElementById('att_bc');
    const bc    = (attEl ? attEl.value.trim() : '')
                  || localStorage.getItem('lastStaffBc') || '';
    const sid   = staffId || '';

    if (!bc && !sid) {
        toast('warning', '⚠️ Login first — tap the chip at top right');
        return;
    }

    toast('info', '🏢 Verifying...');

    fetch('/api/lan_verify', {
        method:  'POST',
        headers: {'Content-Type': 'application/json'},
        body:    JSON.stringify({ staff_barcode: bc, staff_id: sid })
    })
    .then(r => r.json())
    .then(d => {
        toast(d.success ? 'success' : 'error',
              d.message || (d.success ? '✅ LAN verified' : '❌ Verify failed'));
        if (d.success) {
            if (attEl) attEl.value = '';
            loadAttendanceStatus().then(() => loadHomeLog());
            preloadOrders();
            loadMyStages();
        }
    })
    .catch(err => toast('error', '❌ Network error: ' + (err.message || 'fetch failed')));
}

function lookupStagStaff() {
    const bc = document.getElementById('stg_staff').value.trim();
    if (!bc) return;
    fetch('/api/lookup_staff?barcode=' + encodeURIComponent(bc))
        .then(r => r.json())
        .then(d => {
            const badge = document.getElementById('stg_staff_badge');
            if (d.found) {
                stgStaffId   = d.id;
                stgStaffName = d.name;
                staffId      = d.id;
                staffName    = d.name;
                staffRole    = d.role || '';
                localStorage.setItem('staffId',   d.id);
                localStorage.setItem('staffName', d.name);
                localStorage.setItem('staffRole', d.role||'');
                localStorage.setItem('lastStaffBc', bc);
                badge.textContent = '✅ ' + d.name + ' · ' + (d.role || '');
                badge.style.display = 'block';
                badge.style.color = 'var(--green)';
                updateChip();
                loadAttendanceStatus().then(ok => {
                    if (ok) {
                        document.getElementById('stg_order').focus();
                    } else {
                        renderWorkLocked('stage');
                    }
                });
            } else {
                badge.textContent = '❌ ' + (d.hint || ('Not found: ' + (d.scanned || bc)));
                badge.style.display = 'block';
                badge.style.color = 'var(--red)';
            }
        });
}

function clearStageStaff() {
    document.getElementById('stg_staff').value = '';
    document.getElementById('stg_staff_badge').style.display = 'none';
    stgStaffId = ''; stgStaffName = '';
}

// ── Stage tab order dropdown ──────────────────────────────────────────────────
function renderStageOrders(orders) {
    const el = document.getElementById('stg_order_list');
    if (!el) return;
    if (!orders || !orders.length) {
        el.innerHTML = '<div style="color:var(--muted);font-size:.78rem;text-align:center;padding:8px">' +
            'No assigned orders. Assign blanks in ERP first, then refresh.</div>';
        return;
    }
    el.innerHTML = orders.map(o => {
        const stage   = o.current_stage_label || 'Not started';
        const stageC  = o.current_stage ? '#f59e0b' : '#64748b';
        const party   = o.party_name || '—';
        const product = o.product_summary || '';
        return `<div class="order-pick-row" onclick="selectStageOrder('${escapeHtml(o.order_no)}')"
                     data-order="${escapeHtml(o.order_no)}"
                     data-party="${escapeHtml(party.toLowerCase())}"
                     data-product="${escapeHtml(product.toLowerCase())}">
            <div style="display:flex;justify-content:space-between;align-items:center">
                <span style="font-weight:700;color:var(--blue);font-size:.88rem">${escapeHtml(o.order_no)}</span>
                <span style="font-size:.65rem;padding:2px 8px;border-radius:8px;
                             background:var(--surface);color:${stageC};border:1px solid ${stageC}">
                    ${escapeHtml(stage)}
                </span>
            </div>
            <div style="font-size:.75rem;color:var(--text);margin-top:2px">${escapeHtml(party)}</div>
            ${product ? `<div style="font-size:.7rem;color:var(--muted)">${escapeHtml(product)}</div>` : ''}
        </div>`;
    }).join('');
}

function clearSelectedOrder() {
    document.getElementById('stg_order').value = '';
    document.getElementById('stg_order_preview').style.display = 'none';
    document.querySelectorAll('.order-pick-row').forEach(r => {
        r.style.background  = '';
        r.style.borderColor = 'var(--border)';
    });
}

function selectStageOrder(order_no) {
    // Highlight selected row
    document.querySelectorAll('.order-pick-row').forEach(r => {
        const sel = r.dataset.order === order_no;
        r.style.background  = sel ? 'var(--surface2)' : '';
        r.style.borderColor = sel ? 'var(--blue)'     : 'var(--border)';
    });
    document.getElementById('stg_order').value = order_no;
    document.getElementById('stg_order_preview').style.display = 'none';
    previewOrder();
}

function filterStageOrders(q) {
    const rows = document.querySelectorAll('.order-pick-row');
    const lq   = q.toLowerCase().trim();
    rows.forEach(r => {
        const match = !lq ||
            r.dataset.order.toLowerCase().includes(lq) ||
            (r.dataset.party || '').includes(lq) ||
            (r.dataset.product || '').includes(lq);
        r.style.display = match ? '' : 'none';
    });
}

function refreshStageOrders() {
    const el = document.getElementById('stg_order_list');
    if (el) el.innerHTML = '<div style="color:var(--muted);font-size:.78rem;text-align:center;padding:8px">Refreshing...</div>';
    const url = '/api/my_orders' + (staffId ? '?staff_id=' + encodeURIComponent(staffId) : '');
    fetch(url)
        .then(r => r.json())
        .then(data => {
            const orders = Array.isArray(data) ? data : (data.orders || []);
            allOrders = orders;
            renderStageOrders(orders);
            renderOrders(orders);  // also refresh orders tab
        })
        .catch(() => {
            if (el) el.innerHTML = '<div style="color:var(--red);font-size:.78rem;text-align:center">' +
                '❌ Could not load. <button onclick="refreshStageOrders()" ' +
                'style="color:var(--blue);background:none;border:none;cursor:pointer">Retry</button></div>';
        });
}

// Search in stage order list
function stageOrderSearch(q) {
    filterStageOrders(q);
    // also use as barcode scan if it looks like an order number
    if (q.match(/^[RWrw][/][0-9]{4}[/][0-9]{4}/)) {
        const cleaned = q.replace(/-[RrLl]$/, '').replace(/[-][0-9]$/, '').replace(/-$/, '').trim();
        selectStageOrder(cleaned.toUpperCase());
    }
}


function sanitiseOrderInput(el) {
    let v = el.value;
    // If browser autofilled a URL — clear it immediately
    if (v.startsWith('http') || v.startsWith('www') || v.includes('://')) {
        el.value = '';
        toast('warning', '⚠️ Browser autofilled a URL. Type or scan the order barcode manually.');
        return;
    }
    // Remove any whitespace
    el.value = v.replace(/ /g, '').replace(/\t/g, '');
}

function previewOrder() {
    const rawOrder = document.getElementById('stg_order').value.trim();
    if (!rawOrder) return;
    // Guard: reject URLs that browser autofilled
    if (rawOrder.startsWith('http') || rawOrder.includes('://') || rawOrder.startsWith('www')) {
        document.getElementById('stg_order').value = '';
        toast('warning', '⚠️ Invalid — browser filled a URL. Scan the job card barcode.');
        return;
    }
    // Clean order number — strip eye suffix -R/-L/-
    const order = rawOrder.replace(/-[RrLl]$/, '').replace(/[-][0-9]$/, '').replace(/-$/, '').trim();
    if (order !== rawOrder) {
        document.getElementById('stg_order').value = order;
    }
    const preview = document.getElementById('stg_order_preview');
    preview.innerHTML = '<div style="color:var(--muted);font-size:.78rem">Looking up...</div>';
    preview.style.display = 'block';

    fetch('/api/order_detail?order_no=' + encodeURIComponent(order)
        + '&staff_id=' + encodeURIComponent(staffId || ''))
        .then(r => r.json())
        .then(d => {
            if (!d.found) {
                preview.innerHTML = '<div style="color:var(--red);font-size:.78rem">❌ Order not found: <b>' +
                    escapeHtml(order) + '</b>' +
                    (d.error ? '<br><span style="font-size:.7rem">' + escapeHtml(d.error) + '</span>' : '') +
                    '</div>';
                return;
            }
            const stageColor = d.current_stage ? 'var(--yellow)' : 'var(--muted)';
            preview.innerHTML = `
                <div style="padding:8px;background:var(--surface2);border-radius:8px;font-size:.78rem">
                    <div style="font-weight:700;color:var(--blue)">${escapeHtml(d.order_no)}</div>
                    <div style="color:var(--text);margin-top:2px">${escapeHtml(d.party_name || '—')}</div>
                    <div style="color:var(--muted);margin-top:2px">${escapeHtml(d.product_summary || '')}</div>
                    <div style="color:${stageColor};margin-top:4px;font-size:.72rem">
                        Current stage: ${escapeHtml(d.current_stage_label || 'Not started')}
                    </div>
                </div>`;
            document.getElementById('stg_stage_bc').focus();
        })
        .catch(() => {
            preview.innerHTML = '<div style="color:var(--red);font-size:.78rem">❌ Network error</div>';
        });
}

// ── Global busy guard — prevents double-submit ─────────────────────────────
let _busy = false;
function _setBusy(ms) {
    if (_busy) return false;
    _busy = true;
    setTimeout(() => { _busy = false; }, ms || 2500);
    return true;
}

function doStageScan() {
    if (!_setBusy()) { toast('warning', '⏳ Please wait...'); return; }
    const bc    = document.getElementById('stg_stage_bc').value.trim().toUpperCase();
    const order = document.getElementById('stg_order').value.trim();
    const sId   = stgStaffId || staffId;
    const sName = stgStaffName || staffName;

    if (!sId)  { _busy=false; toast('warning', '⚠️ Login with your staff barcode first (tap top-right chip)'); return; }
    if (!order){ _busy=false; toast('warning', '⚠️ Select or scan an order first'); return; }
    if (!bc)   { _busy=false; toast('warning', '⚠️ Tap a stage button or scan wall barcode'); return; }

    let stage_code = bc.startsWith('STAGE:') ? bc.replace('STAGE:','') : bc;

    fetch('/api/stage', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ staff_id: sId, staff_barcode: document.getElementById('stg_staff').value.trim(),
                               order_no: order, stage_code: stage_code })
    })
    .then(r => r.json())
    .then(d => {
        _busy = false;
        toast(d.success ? 'success' : 'error', d.message);
        if (d.success) {
            clearSelectedOrder();
            document.getElementById('stg_stage_bc').value = '';
            document.querySelectorAll('.stage-tile').forEach(t => t.classList.remove('selected'));
            preloadOrders();
            refreshStageOrders();
        }
    })
    .catch(() => { _busy=false; toast('error', '❌ Network error'); });
}

// ── Stage grid — built per employee, rebuilt on login ────────────────────────
const STAGE_COLORS = {
    'PRODUCTION_PICKED': '#1d4ed8', 'PRODUCTION_DONE':    '#1d4ed8',
    'INSPECTION_1':      '#0891b2', 'INSPECTION':         '#0891b2',
    'HARDCOAT_PICKED':   '#7c3aed', 'HARDCOAT_DONE':      '#7c3aed',
    'HARD_COAT':         '#7c3aed',
    'INSPECTION_AFTER_HC':'#0891b2',
    'AR_COAT':           '#059669', 'TINTING':            '#b45309',
    'FITTING':           '#be185d', 'QC':                 '#065f46',
    'READY':             '#166534',
};

function buildStageGrid(stages) {
    const grid = document.getElementById('stage_grid');
    if (!grid) return;
    grid.innerHTML = '';
    if (!stages || !stages.length) {
        // If staff not logged in yet — prompt to login
        if (!staffId) {
            grid.innerHTML = '<div style="color:var(--muted);font-size:.78rem;text-align:center;' +
                'padding:12px;grid-column:1/-1">👤 Login with your staff barcode first.</div>';
        } else {
            grid.innerHTML = '<div style="color:var(--muted);font-size:.78rem;text-align:center;' +
                'padding:12px;grid-column:1/-1">No stages assigned to your role.<br>' +
                'Ask admin: ERP → HR → Employees → set your Production Stage Codes.</div>';
        }
        return;
    }
    stages.forEach(s => {
        const div   = document.createElement('div');
        const color = STAGE_COLORS[s.code] || '#374151';
        const label = s.label.indexOf(' ') > 0
                      ? s.label.slice(s.label.indexOf(' ') + 1)
                      : s.label;
        div.className = 'stage-tile';
        div.style.borderColor = color;
        div.style.background  = color + '22';  // 13% opacity
        div.innerHTML =
            `<span class="stage-emoji">${stageEmoji(s.code)}</span>` +
            `<div class="stage-name" style="color:${color}">${label}</div>` +
            `<div style="font-size:.55rem;color:#64748b;margin-top:2px;font-family:monospace">${s.code}</div>`;
        div.onclick = () => {
            // Highlight selected
            document.querySelectorAll('.stage-tile').forEach(t => t.classList.remove('selected'));
            div.classList.add('selected');
            document.getElementById('stg_stage_bc').value = s.code;
            doStageScan();
        };
        grid.appendChild(div);
    });
}

function loadMyStages() {
    const url = '/api/my_stages' + (staffId ? '?staff_id=' + encodeURIComponent(staffId) : '');
    fetch(url)
        .then(r => r.json())
        .then(stages => buildStageGrid(stages))
        .catch(() => {
            // Fallback to all stages
            fetch('/api/stages').then(r => r.json()).then(stages => buildStageGrid(stages));
        });
}

// Load on page start
loadMyStages();

function stageEmoji(code) {
    const map = {
        'RECEIVED':'📥','SURFACING':'🔬','INSPECTION_1':'🔍',
        'HARD_COAT':'🛡️','AR_COAT':'✨','TINTING':'🎨',
        'FITTING':'🔧','QC':'✅','READY':'📦'
    };
    return map[code] || '⚙️';
}

// ── Orders tab ───────────────────────────────────────────────────────────────
function loadOrders() {
    document.getElementById('orders_list').innerHTML =
        '<div style="color:var(--muted);font-size:.82rem;text-align:center;padding:20px">Loading...</div>';
    const url = '/api/my_orders' + (staffId ? '?staff_id=' + encodeURIComponent(staffId) : '');
    fetch(url)
        .then(r => r.json())
        .then(data => {
            const orders = Array.isArray(data) ? data : (data.orders || []);
            allOrders = orders;
            renderOrders(orders);
        })
        .catch(() => {
            document.getElementById('orders_list').innerHTML =
                '<div style="color:var(--red);font-size:.82rem;text-align:center;padding:20px">' +
                '❌ Could not load. <button class="btn btn-ghost" style="padding:6px 14px;margin-top:8px" onclick="loadOrders()">🔄 Retry</button></div>';
        });
}

function preloadOrders() {
    const url = '/api/my_orders' + (staffId ? '?staff_id=' + encodeURIComponent(staffId) : '');
    fetch(url).then(r => r.json()).then(d => {
        allOrders = Array.isArray(d) ? d : (d.orders || []);
        renderStageOrders(allOrders);  // populate stage dropdown too
    }).catch(() => {});
}

function filterOrders(q) {
    if (!q) { renderOrders(allOrders); return; }
    const lq = q.toLowerCase();
    renderOrders(allOrders.filter(o =>
        (o.order_no||'').toLowerCase().includes(lq) ||
        (o.party_name||'').toLowerCase().includes(lq) ||
        (o.current_stage_label||'').toLowerCase().includes(lq)
    ));
}

function renderOrders(orders) {
    const el = document.getElementById('orders_list');
    if (!orders.length) {
        el.innerHTML = '<div style="color:var(--muted);font-size:.82rem;text-align:center;padding:20px">No active orders in production</div>';
        return;
    }
    el.innerHTML = orders.map(o => `
        <div class="order-card" onclick="selectOrder('${o.order_no}')">
            <div class="order-card-top">
                <span class="order-no">${o.order_no}</span>
                <span class="order-stage ${o.current_stage ? 'active' : ''}">
                    ${o.current_stage_label || 'Not started'}
                </span>
            </div>
            <div class="order-party">${o.party_name || '—'}</div>
            <div class="order-eyes">${o.product_summary || ''}</div>
        </div>`).join('');
}

function selectOrder(order_no) {
    // Show production detail drawer
    showOrderDetail(order_no);
}

// ── Order detail drawer ───────────────────────────────────────────────────────
function showOrderDetail(order_no) {
    // Create or reuse drawer
    let drawer = document.getElementById('order_drawer');
    if (!drawer) {
        drawer = document.createElement('div');
        drawer.id = 'order_drawer';
        drawer.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:200;display:flex;align-items:flex-end';
        drawer.onclick = e => { if(e.target===drawer) closeDrawer(); };
        document.body.appendChild(drawer);
    }
    drawer.innerHTML = `
        <div style="background:var(--surface);border-radius:20px 20px 0 0;
                    width:100%;max-height:88vh;overflow-y:auto;padding:16px 16px 32px">
            <div style="width:36px;height:4px;background:var(--border);border-radius:2px;margin:0 auto 14px"></div>
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
                <div style="font-size:1rem;font-weight:700;color:var(--blue)">${order_no}</div>
                <button onclick="closeDrawer()" style="background:var(--surface2);border:none;
                        color:var(--muted);padding:6px 12px;border-radius:8px;font-size:.8rem;cursor:pointer">
                    ✕ Close
                </button>
            </div>
            <div id="drawer_content" style="color:var(--muted);font-size:.82rem;text-align:center;padding:20px">
                Loading...
            </div>
            <div class="btn-row" style="margin-top:14px">
                <button class="btn btn-blue" onclick="useOrderForStage('${order_no}')">
                    ⚙️ Update Stage for this Order
                </button>
            </div>
        </div>`;
    drawer.style.display = 'flex';

    // Fetch detail
    fetch('/api/order_production_detail?order_no=' + encodeURIComponent(order_no)
        + '&staff_id=' + encodeURIComponent(staffId || ''))
        .then(r => r.json())
        .then(d => {
            if (d.locked) {
                document.getElementById('drawer_content').innerHTML =
                    '<div style="color:var(--yellow)">🔒 ' + escapeHtml(d.message || 'Office check-in required.') + '</div>';
                return;
            }
            if (!d.found) {
                document.getElementById('drawer_content').innerHTML =
                    '<div style="color:var(--red)">❌ Order not found</div>';
                return;
            }
            let html = '';
            d.lines.forEach(ln => {
                const eye      = ln.eye_side || '—';
                const eyeColor = eye==='R' ? '#2f81f7' : eye==='L' ? '#f0883e' : '#8b949e';

                // ── Surfacing (transposed) powers — PRIMARY ─────────────
                const sph_s = fmt(ln.sph_surf);
                const cyl_s = fmt(ln.cyl_surf);
                const ax_s  = ln.axis_surf || '—';
                const add_s = ln.add_power_selected
                              ? fmt(ln.add_power_selected)
                              : fmt(ln.add_power);
                const hasSurf = ln.sph_surf || ln.cyl_surf || ln.axis_surf;

                // ── RX powers — reference only ─────────────────────────
                const sph_rx = fmt(ln.sph);
                const cyl_rx = fmt(ln.cyl);
                const ax_rx  = ln.axis || '—';
                const add_rx = fmt(ln.add_power);

                const base  = ln.base_curve ? parseFloat(ln.base_curve).toFixed(2)+'D' : '—';
                const toolA = ln.tool_a || ln.dia_tool_a || '—';
                const toolB = ln.tool_b || ln.dia_tool_b || '—';
                const blank = [ln.blank_brand, ln.blank_material].filter(Boolean).join(' ') || '—';
                const frame = ln.frame_type || '—';
                const stage = ln.production_stage_label || ln.job_stage || 'Not started';

                html += `
                <div style="background:var(--surface2);border:1px solid var(--border);
                            border-radius:12px;padding:12px;margin-bottom:10px">

                    <!-- Eye header -->
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
                        <span style="background:${eyeColor};color:#fff;font-weight:800;
                                     padding:3px 14px;border-radius:20px;font-size:.82rem">
                            ${eye} EYE
                        </span>
                        <span style="font-size:.68rem;color:var(--muted)">${ln.product_name||''}</span>
                    </div>

                    ${hasSurf ? `
                    <!-- SURFACING POWERS — transposed, what machine uses -->
                    <div style="font-size:.58rem;font-weight:700;text-transform:uppercase;
                                letter-spacing:.1em;color:var(--yellow);margin-bottom:6px">
                        ⚙️ Surfacing Parameters (Transposed)
                    </div>
                    <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:5px;margin-bottom:8px">
                        ${surfPow('SPH', sph_s, false)}
                        ${surfPow('CYL', cyl_s, false)}
                        ${surfPow('AXIS', ax_s, true)}
                        ${surfPow('ADD', add_s, false)}
                        ${surfPow('BASE', base, false)}
                    </div>

                    <!-- RX reference — smaller -->
                    <div style="background:var(--bg);border-radius:8px;padding:6px 8px;margin-bottom:8px">
                        <div style="font-size:.55rem;color:var(--muted);margin-bottom:4px">
                            Rx (original prescription)
                        </div>
                        <div style="display:flex;gap:10px;font-size:.72rem;font-family:monospace">
                            <span>S <b>${sph_rx}</b></span>
                            <span>C <b>${cyl_rx}</b></span>
                            <span>A <b>${ax_rx}</b></span>
                            ${add_rx !== '—' ? `<span>Ad <b>${add_rx}</b></span>` : ''}
                        </div>
                    </div>
                    ` : `
                    <!-- No surfacing data yet — show RX -->
                    <div style="font-size:.58rem;font-weight:700;text-transform:uppercase;
                                letter-spacing:.1em;color:var(--muted);margin-bottom:6px">
                        📋 Prescription (RX) — surfacing not yet saved
                    </div>
                    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:5px;margin-bottom:8px">
                        ${surfPow('SPH', sph_rx, false)}
                        ${surfPow('CYL', cyl_rx, false)}
                        ${surfPow('AXIS', ax_rx, true)}
                        ${surfPow('ADD', add_rx, false)}
                    </div>
                    `}

                    <!-- Tool A / Tool B — highlighted RED -->
                    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:6px">
                        <div style="background:#3a0a0a;border:2px solid #dc2626;border-radius:8px;
                                    padding:8px 4px;text-align:center">
                            <div style="font-size:.55rem;color:#f87171;font-weight:700;letter-spacing:.08em">TOOL A</div>
                            <div style="font-size:1.1rem;font-weight:900;color:#fff;font-family:monospace;margin-top:2px">${toolA}</div>
                        </div>
                        <div style="background:#3a0a0a;border:2px solid #dc2626;border-radius:8px;
                                    padding:8px 4px;text-align:center">
                            <div style="font-size:.55rem;color:#f87171;font-weight:700;letter-spacing:.08em">TOOL B</div>
                            <div style="font-size:1.1rem;font-weight:900;color:#fff;font-family:monospace;margin-top:2px">${toolB}</div>
                        </div>
                        ${surfCell('BASE', base, '#1c3a5e')}
                    </div>

                    <!-- Blank + Frame -->
                    <div style="display:grid;grid-template-columns:2fr 1fr;gap:6px;margin-bottom:6px">
                        ${surfCell('BLANK', blank, '#1a1a2e')}
                        ${surfCell('FRAME', frame, '#1a1a2e')}
                    </div>

                    <!-- Stage -->
                    <div style="font-size:.68rem;color:var(--yellow);margin-top:4px">
                        📍 ${stage}
                        ${ln.stage_by ? '<span style="color:var(--muted)"> · ' + ln.stage_by + '</span>' : ''}
                    </div>
                </div>`;
            });

            // Stage history
            if (d.history && d.history.length) {
                html += `<div style="margin-top:4px">
                    <div style="font-size:.62rem;font-weight:700;text-transform:uppercase;
                                letter-spacing:.1em;color:var(--muted);margin-bottom:8px">Stage History</div>`;
                d.history.forEach(h => {
                    html += `<div style="display:flex;gap:8px;padding:5px 0;
                                         border-bottom:1px solid var(--border);font-size:.75rem">
                        <span style="color:var(--green);flex-shrink:0">${h.time||''}</span>
                        <span style="flex:1">${h.stage_label||''}</span>
                        <span style="color:var(--muted)">${h.emp_name||''}</span>
                    </div>`;
                });
                html += `</div>`;
            }

            document.getElementById('drawer_content').innerHTML = html;
        })
        .catch(() => {
            document.getElementById('drawer_content').innerHTML =
                '<div style="color:var(--red)">❌ Network error</div>';
        });
}

function closeDrawer() {
    const d = document.getElementById('order_drawer');
    if (d) d.style.display = 'none';
}

function useOrderForStage(order_no) {
    closeDrawer();
    document.getElementById('stg_order').value = order_no;
    showTab('stage');
    previewOrder();
    // Pre-fill staff badge if known
    if (staffId) {
        stgStaffId = staffId; stgStaffName = staffName;
        const badge = document.getElementById('stg_staff_badge');
        const bc = localStorage.getItem('lastStaffBc') || '';
        if (bc) document.getElementById('stg_staff').value = bc;
        badge.textContent = '✅ ' + staffName;
        badge.style.display = 'block';
        badge.style.color = 'var(--green)';
    }
}

function fmt(v) {
    if (v === null || v === undefined || v === '') return '—';
    const n = parseFloat(v);
    if (isNaN(n)) return String(v);
    return (n >= 0 ? '+' : '') + n.toFixed(2);
}

function surfPow(lbl, val, highlight) {
    const bg  = highlight ? '#3a2a00' : 'var(--bg)';
    const col = highlight ? '#fde047' : 'var(--text)';
    const border = highlight ? '1.5px solid #fde047' : '1px solid var(--border)';
    return `<div style="background:${bg};border:${border};border-radius:8px;
                        padding:7px 4px;text-align:center">
        <div style="font-size:.52rem;color:var(--muted);font-weight:700;letter-spacing:.08em">${lbl}</div>
        <div style="font-size:.88rem;font-weight:800;color:${col};font-family:monospace;margin-top:2px">${val}</div>
    </div>`;
}

function surfCell(lbl, val, bg) {
    return `<div style="background:${bg};border-radius:8px;padding:7px 4px;text-align:center">
        <div style="font-size:.55rem;color:var(--muted);font-weight:700;letter-spacing:.08em">${lbl}</div>
        <div style="font-size:.82rem;font-weight:700;color:var(--text);margin-top:2px">${val}</div>
    </div>`;
}

function _todayIso() {
    const d = new Date();
    d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
    return d.toISOString().slice(0,10);
}

function fmtMinutes(mins) {
    if (mins === null || mins === undefined || mins === '') return '—';
    const n = parseInt(mins, 10);
    if (isNaN(n)) return '—';
    if (n < 60) return n + ' min';
    const h = Math.floor(n / 60);
    const m = n % 60;
    return h + 'h ' + String(m).padStart(2, '0') + 'm';
}

function loadProductionReport() {
    const dateEl = document.getElementById('home_report_date');
    const body = document.getElementById('home_report_body');
    if (!body) return;
    if (dateEl && !dateEl.value) dateEl.value = _todayIso();
    const d = (dateEl && dateEl.value) ? dateEl.value : _todayIso();
    body.innerHTML = '<div style="color:var(--muted);font-size:.78rem">Loading report...</div>';
    const qs = new URLSearchParams({date: d});
    if (staffId) qs.set('staff_id', staffId);
    fetch('/api/production_timing_report?' + qs.toString())
        .then(r => {
            if (!r.ok) throw new Error('report HTTP ' + r.status);
            return r.json();
        })
        .then(rep => {
            const rows = Array.isArray(rep.rows) ? rep.rows : [];
            const summary = rep.summary || {};
            let html = `
                <div class="report-summary">
                  <div class="report-metric"><b>${summary.total_orders || 0}</b><span>${summary.staff_name ? 'My Orders' : 'Orders'}</span></div>
                  <div class="report-metric"><b>${summary.total_pairs || '0'}</b><span>${summary.staff_name ? 'My Pairs' : 'Pairs'}</span></div>
                  <div class="report-metric"><b>${summary.repeated_orders || 0}</b><span>Repeats</span></div>
                </div>`;
            if (!rows.length) {
                html += '<div style="color:var(--muted);font-size:.78rem">No production scans for this date.</div>';
            } else {
                html += rows.map(r => {
                    const repeat = r.repeat_count > 1
                        ? `<span class="report-repeat">Repeat x${r.repeat_count}</span>`
                        : 'Single pass';
                    const pairText = r.pairs ? `${r.pairs} pair` : '— pair';
                    return `
                    <div class="report-row">
                      <div class="report-row-top">
                        <span>${escapeHtml(r.order_no || '—')}</span>
                        <span>${escapeHtml(r.in_time || '—')} → ${escapeHtml(r.out_time || '—')}</span>
                      </div>
                      <div class="report-row-sub">
                        Clear in <b style="color:var(--text)">${fmtMinutes(r.clear_minutes)}</b>
                        · ${pairText}
                        · ${repeat}<br>
                        ${escapeHtml(r.first_stage || 'In')} → ${escapeHtml(r.last_stage || 'Out')}
                        ${r.emp_name ? ' · ' + escapeHtml(r.emp_name) : ''}
                      </div>
                    </div>`;
                }).join('');
            }
            body.innerHTML = html;
        })
        .catch(err => {
            body.innerHTML = '<div style="color:var(--yellow);font-size:.78rem">Report unavailable. Refresh once.</div>';
            console.warn('loadProductionReport failed', err);
        });
}

// ── Home log ─────────────────────────────────────────────────────────────────
function loadHomeLog() {
    if (!staffId) return;
    fetch('/api/today_log?emp_id=' + encodeURIComponent(staffId))
        .then(r => {
            if (!r.ok) throw new Error('today_log HTTP ' + r.status);
            return r.json();
        })
        .then(data => Array.isArray(data) ? data : [])
        .then(rows => {
            const card = document.getElementById('home_today_card');
            const el   = document.getElementById('home_today_log');
            if (!card || !el) return;
            if (!rows.length) {
                el.innerHTML = '<div style="color:var(--muted);font-size:.78rem">No activity yet today</div>';
            } else {
                el.innerHTML = rows.slice(0,8).map(r => `
                    <div class="hist-item">
                        <div class="hist-dot"></div>
                        <div class="hist-text">
                            <b>${r.stage_label || r.action || ''}</b>
                            ${r.order_no ? '<span style="color:var(--muted)"> · ' + r.order_no + '</span>' : ''}
                        </div>
                        <div class="hist-time">${(r.scanned_at||'').substring(11,16)}</div>
                    </div>`).join('');
            }
            card.style.display = 'block';
        })
        .catch(err => {
            const card = document.getElementById('home_today_card');
            const el   = document.getElementById('home_today_log');
            if (card && el) {
                el.innerHTML = '<div style="color:var(--yellow);font-size:.78rem">Today log unavailable. Refresh once.</div>';
                card.style.display = 'block';
            }
            console.warn('loadHomeLog failed', err);
        });
}
if (staffId) {
    // Auto-fill stage staff field from localStorage
    const lastBc = localStorage.getItem('lastStaffBc') || '';
    if (lastBc) {
        document.getElementById('stg_staff').value = lastBc;
        stgStaffId   = staffId;
        stgStaffName = staffName;
        const badge = document.getElementById('stg_staff_badge');
        badge.textContent = '✅ ' + staffName + ' · ' + staffRole;
        badge.style.display = 'block';
        badge.style.color = 'var(--green)';
    }
    loadMyStages();  // rebuild stage grid for this specific employee
    loadAttendanceStatus().then(() => {
        loadHomeLog();
        loadProductionReport();
        preloadOrders();
    });
}
loadProductionReport();

// ── Toast ─────────────────────────────────────────────────────────────────────
let toastTimer;
function toast(type, msg) {
    const el = document.getElementById('toast');
    el.className = 'toast ' + type;
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('show'), 4500);
}


// ══════════════════════════════════════════════════════════════════════════════
// CAMERA BARCODE SCANNER
// ══════════════════════════════════════════════════════════════════════════════
let camStream    = null;
let camTarget    = null;   // input element id to fill
let camInterval  = null;
let camDetector  = null;
let camActive    = false;
let zxingLoading = false;
let zxingCallbacks = [];

function _loadScriptOnce(url, onload, onerror) {
    const existing = document.querySelector('script[data-src="' + url + '"]');
    if (existing) {
        existing.addEventListener('load', onload, {once:true});
        existing.addEventListener('error', onerror, {once:true});
        return;
    }
    const s = document.createElement('script');
    s.src = url;
    s.async = true;
    s.dataset.src = url;
    s.onload = onload;
    s.onerror = onerror;
    document.head.appendChild(s);
}

function _loadZxing(done) {
    if (window.ZXing) {
        done();
        return;
    }
    zxingCallbacks.push(done);
    if (zxingLoading) return;
    zxingLoading = true;

    const sources = [
        '/static/vendor/zxing-browser.min.js',
        'https://unpkg.com/@zxing/library@0.21.3/umd/index.min.js',
        'https://cdn.jsdelivr.net/npm/@zxing/library@0.21.3/umd/index.min.js'
    ];
    let i = 0;
    const finish = () => {
        zxingLoading = false;
        const callbacks = zxingCallbacks.splice(0);
        callbacks.forEach(cb => {
            try { cb(); } catch(e) {}
        });
    };
    const next = () => {
        if (window.ZXing || i >= sources.length) {
            finish();
            return;
        }
        _loadScriptOnce(sources[i++], next, next);
    };
    next();
}

// Open camera modal — targetId is the input field to fill with result
function openCam(targetId, title) {
    camTarget = targetId;
    camActive = true;
    document.getElementById('cam_modal_title').textContent = title || '📷 Scan Barcode';
    document.getElementById('cam_modal').style.display = 'flex';

    // On plain HTTP (LAN), getUserMedia and BarcodeDetector are blocked by Chrome.
    // Detect this and go straight to photo file input — works on ALL phones, all browsers.
    const isHttp = location.protocol === 'http:' && !['localhost','127.0.0.1'].includes(location.hostname);

    if (isHttp) {
        // HTTP/LAN: skip live camera, go straight to file input (takes photo with camera)
        document.getElementById('cam_status').textContent =
            '📷 Camera will open. Point at barcode and take photo.';
        document.getElementById('cam_video').style.display = 'none';
        setTimeout(() => {
            document.getElementById('cam_file_input').click();
        }, 300);
        return;
    }

    // HTTPS or localhost: try live camera scanning
    document.getElementById('cam_status').textContent = 'Starting camera...';
    document.getElementById('cam_video').style.display = 'block';

    if ('BarcodeDetector' in window) {
        startNativeScanner();
    } else {
        _loadZxing(() => {
            if (window.ZXing) startZxingScanner();
            else {
                document.getElementById('cam_status').textContent =
                    'Camera scan not supported. Tap 🖼️ Photo to scan from camera.';
                triggerFileInput();
            }
        });
    }
}

function closeCamModal() {
    camActive = false;
    if (camInterval)  { clearInterval(camInterval); camInterval = null; }
    if (camStream)    { camStream.getTracks().forEach(t => t.stop()); camStream = null; }
    const vid = document.getElementById('cam_video');
    if (vid) vid.srcObject = null;
    document.getElementById('cam_modal').style.display = 'none';
}

function onBarcodeDetected(code) {
    if (!code || !camTarget) return;
    closeCamModal();
    const el = document.getElementById(camTarget);
    if (!el) return;
    el.value = code;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    // Trigger associated action
    if (camTarget === 'stg_order')   previewOrder();
    if (camTarget === 'att_bc')      { /* just fill, user taps button */ }
    if (camTarget === 'modal_bc')    loginFromModal();
    if (camTarget === 'stg_stage_bc') doStageScan();
    toast('success', '✅ Scanned: ' + code);
}

// ── Native BarcodeDetector (Chrome Android) ───────────────────────────────────
function startNativeScanner() {
    navigator.mediaDevices.getUserMedia({
        video: { facingMode: 'environment', width: {ideal:1280}, height: {ideal:720} }
    })
    .then(stream => {
        camStream = stream;
        const vid = document.getElementById('cam_video');
        vid.srcObject = stream;
        vid.play();
        document.getElementById('cam_status').textContent = 'Point at barcode...';

        camDetector = new BarcodeDetector({
            formats: ['code_128', 'code_39', 'qr_code', 'ean_13', 'ean_8',
                      'itf', 'data_matrix', 'pdf417', 'aztec']
        });
        camInterval = setInterval(() => {
            if (!camActive || !vid.readyState || vid.readyState < 2) return;
            camDetector.detect(vid).then(codes => {
                if (codes && codes.length > 0) {
                    const code = codes[0].rawValue;
                    onBarcodeDetected(code);
                }
            }).catch(() => {});
        }, 300);
    })
    .catch(err => {
        document.getElementById('cam_status').textContent =
            'Camera access denied. Tap 🖼️ Photo instead.';
        console.warn('Camera error:', err);
    });
}

// ── ZXing scanner (fallback) ──────────────────────────────────────────────────
function startZxingScanner() {
    navigator.mediaDevices.getUserMedia({video: {facingMode: 'environment'}})
    .then(stream => {
        camStream = stream;
        const vid = document.getElementById('cam_video');
        vid.srcObject = stream;
        vid.play();
        document.getElementById('cam_status').textContent = 'Point at barcode...';

        const hints = new Map();
        hints.set(ZXing.DecodeHintType.TRY_HARDER, true);
        const reader = new ZXing.BrowserMultiFormatReader(hints);
        const tick = () => {
            if (!camActive) return;
            try {
                const canvas = document.createElement('canvas');
                canvas.width  = vid.videoWidth  || 640;
                canvas.height = vid.videoHeight || 480;
                const ctx = canvas.getContext('2d');
                ctx.drawImage(vid, 0, 0);
                const imgData = ctx.getImageData(0, 0, canvas.width, canvas.height);
                const lum = new ZXing.RGBLuminanceSource(
                    new Uint8ClampedArray(imgData.data.buffer),
                    canvas.width, canvas.height);
                const bmp    = new ZXing.BinaryBitmap(new ZXing.HybridBinarizer(lum));
                const result = reader.decodeFromBitmap(bmp);
                if (result && result.text) {
                    onBarcodeDetected(result.text);
                    return;
                }
            } catch(e) { /* not found, keep scanning */ }
            if (camActive) setTimeout(tick, 250);
        };
        vid.onloadedmetadata = () => tick();
    })
    .catch(err => {
        document.getElementById('cam_status').textContent =
            'Camera error. Use 🖼️ Photo.';
    });
}

// ── Photo decode (guaranteed fallback) ───────────────────────────────────────
function triggerFileInput() {
    closeCamModal();
    // Re-open just for file input
    camActive = true;
    document.getElementById('cam_modal').style.display = 'flex';
    document.getElementById('cam_video').style.display = 'none';
    document.getElementById('cam_status').textContent = 'Taking photo...';
    document.getElementById('cam_file_input').click();
}

function decodeFromPhoto(input) {
    const file = input.files[0];
    if (!file) { closeCamModal(); return; }
    document.getElementById('cam_status').textContent = 'Decoding...';
    const img = new Image();
    img.onload = () => {
        // Try native BarcodeDetector on image
        if ('BarcodeDetector' in window) {
            const det = new BarcodeDetector();
            det.detect(img).then(codes => {
                if (codes && codes.length > 0) {
                    onBarcodeDetected(codes[0].rawValue);
                } else {
                    tryZxingOnImage(img);
                }
            }).catch(() => tryZxingOnImage(img));
        } else {
            tryZxingOnImage(img);
        }
        URL.revokeObjectURL(img.src);
    };
    img.src = URL.createObjectURL(file);
    input.value = '';
}

function tryZxingOnImage(img) {
    _loadZxing(() => {
        if (!window.ZXing) {
            document.getElementById('cam_status').textContent =
                '❌ Could not decode. Try again with better lighting.';
            return;
        }
        try {
            const canvas = document.createElement('canvas');
            canvas.width  = img.naturalWidth;
            canvas.height = img.naturalHeight;
            canvas.getContext('2d').drawImage(img, 0, 0);
            const reader = new ZXing.BrowserMultiFormatReader();
            reader.decodeFromImage(img).then(result => {
                if (result) onBarcodeDetected(result.text);
                else document.getElementById('cam_status').textContent =
                    '❌ No barcode found. Try better lighting or closer.';
            }).catch(() => {
                document.getElementById('cam_status').textContent =
                    '❌ No barcode found. Try again.';
                closeCamModal();
            });
        } catch(e) {
            closeCamModal();
        }
    });
}

// Auto-focus order input when stage tab is visible and staff is set
document.getElementById('nav_stage').addEventListener('click', () => {
    if (stgStaffId) setTimeout(()=>document.getElementById('stg_order').focus(), 200);
});
</script>
</body>
</html>
"""



# ══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════════════

def _clean_order_no(raw: str) -> str:
    """
    Normalise scanned order number.
    Job card barcodes may have eye suffix: R/2627/0019-R, R/2627/0019-L, R/2627/0019-
    Strip trailing -R / -L / - to get clean order_no.
    """
    import re
    s = (raw or "").strip().upper()
    # Remove trailing -R, -L, -1, -2 or just trailing dash
    s = re.sub(r'[-_][RL]$', '', s)   # -R or -L at end
    s = re.sub(r'[-_]\d$', '', s)     # -1 or -2 at end
    s = s.rstrip('-').rstrip('_')     # bare trailing dash
    return s.strip()


@app.route("/")
def index():
    return render_template_string(MAIN_HTML, app_version=APP_VERSION)


@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "app": "Parakh Scanner",
        "version": APP_VERSION,
        "host_seen_by_browser": request.host,
        "client_ip": request.headers.get("X-Forwarded-For", request.remote_addr),
        "try_urls": _lan_urls(8502),
        "note": "If this opens on PC but not mobile, phone and PC are not on same Wi-Fi or Windows Firewall is blocking TCP 8502.",
    })


@app.route("/lan")
def lan_check():
    urls = _lan_urls(8502)
    items = "".join(
        f"<li><a href='{u}/health'>{u}/health</a> &nbsp; <a href='{u}'>{u}</a></li>"
        for u in urls
    )
    return f"""<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Parakh Scanner LAN Check</title>
<style>
body{{font-family:Arial,sans-serif;background:#0a0f1e;color:#e5e7eb;padding:20px;line-height:1.45}}
.box{{background:#111827;border:1px solid #334155;border-radius:10px;padding:14px;margin:12px 0}}
a{{color:#7dd3fc;font-weight:700}} code{{background:#020617;padding:2px 5px;border-radius:4px}}
</style></head><body>
<h2>Parakh Scanner LAN Check</h2>
<div class="box">
<b>This computer is serving the scanner.</b><br>
Open one of these URLs on the mobile while mobile and PC are on the same Wi-Fi:
<ul>{items}</ul>
</div>
<div class="box">
If mobile does not open it:<br>
1. Run <code>Fix_Scanner_Firewall_Admin.bat</code> as Administrator.<br>
2. Confirm the phone is on office Wi-Fi, not mobile data / guest Wi-Fi.<br>
3. Restart <code>Start_Scanner_Local.bat</code>.<br>
4. Try <code>/health</code> first, then the main scanner page.
</div>
<div class="box">
Browser connected as: <code>{request.remote_addr}</code><br>
Host used: <code>{request.host}</code>
</div>
</body></html>"""


@app.route("/api/stages")
def api_stages():
    """All stages — used as fallback."""
    try:
        e = _eng()
        return jsonify([{"code": c, "label": l} for c, l in e["stages"]])
    except Exception as ex:
        return jsonify([]), 500


@app.route("/api/my_stages")
def api_my_stages():
    """Stages allowed for THIS employee — filters by role/department/production_stage_codes."""
    staff_id = request.args.get("staff_id", "").strip()
    staff_bc = request.args.get("staff_barcode", "").strip()
    try:
        from modules.hr.hr_scanner_engine import (
            get_employee_by_barcode, _employee_stage_context,
            allowed_stage_codes_for_context, PRODUCTION_STAGES
        )
        emp_ctx = {}
        if staff_bc:
            emp = get_employee_by_barcode(staff_bc)
            if emp:
                emp_ctx = _employee_stage_context(emp["id"])
        if not emp_ctx and staff_id:
            emp_ctx = _employee_stage_context(staff_id)

        allowed = allowed_stage_codes_for_context(emp_ctx) if emp_ctx else []

        # No staff identified — return empty list (grid shows "no stages assigned")
        if not emp_ctx:
            return jsonify([])

        is_admin = any(
            x in str(emp_ctx.get("role", "")).upper()
            for x in ("ADMIN", "MANAGER", "OWNER", "SUPERVISOR")
        )

        # Return all stages for admin, only allowed for others
        stages = PRODUCTION_STAGES if is_admin else [
            (code, label) for code, label in PRODUCTION_STAGES
            if code in allowed
        ]
        return jsonify([{"code": c, "label": l} for c, l in stages])
    except Exception as ex:
        # Fallback to all stages on error
        try:
            e = _eng()
            return jsonify([{"code": c, "label": l} for c, l in e["stages"]])
        except Exception:
            return jsonify([])


@app.route("/api/list_staff")
def api_list_staff():
    """Diagnostic — shows all active staff with their barcodes. Open on PC to fix assignments."""
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT emp_code, name, role, department,
                   COALESCE(staff_barcode, '') AS staff_barcode,
                   CASE WHEN staff_barcode IS NOT NULL AND staff_barcode != ''
                        THEN 'Set' ELSE 'NOT SET' END AS barcode_status
            FROM employees
            WHERE is_active = TRUE
            ORDER BY name
        """) or []
        # Return as simple HTML table for easy reading on phone
        def _row_html(r):
            bg    = "#0d2818" if r["barcode_status"] == "Set" else "#2d0f0f"
            color = "#86efac" if r["barcode_status"] == "Set" else "#fca5a5"
            return (
                f"<tr style='background:{bg}'>"
                f"<td>{r['name']}</td><td>{r['role'] or ''}</td>"
                f"<td style='font-family:monospace'>{r['staff_barcode'] or '—'}</td>"
                f"<td style='color:{color}'>{r['barcode_status']}</td></tr>"
            )
        rows_html = "".join(_row_html(r) for r in rows)
        html = f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Staff Barcodes</title>
<style>
body{{background:#0d1117;color:#e6edf3;font-family:sans-serif;padding:16px}}
h2{{color:#2f81f7;margin-bottom:12px}}
table{{width:100%;border-collapse:collapse;font-size:.85rem}}
th{{background:#161b22;color:#8b949e;padding:8px 6px;text-align:left;
   border-bottom:1px solid #30363d;font-size:.75rem;text-transform:uppercase}}
td{{padding:7px 6px;border-bottom:1px solid #21262d}}
.hint{{background:#0c1a2e;border:1px solid #2f81f7;border-radius:8px;
      padding:12px;margin-bottom:14px;font-size:.82rem;color:#79c0ff}}
</style></head><body>
<h2>Staff Barcode Status</h2>
<div class='hint'>
  If barcode status shows <b style='color:#fca5a5'>NOT SET</b> — 
  go to ERP → HR → Scanner Setup → assign barcode to that employee.<br><br>
  Staff can type their <b>name</b> in the scanner login box as a workaround.
</div>
<table>
<tr><th>Name</th><th>Role</th><th>Barcode</th><th>Status</th></tr>
{rows_html}
</table>
</body></html>"""
        from flask import Response
        return Response(html, mimetype="text/html")
    except Exception as ex:
        return jsonify({"error": str(ex)})


@app.route("/api/lookup_staff")
def api_lookup_staff():
    bc = request.args.get("barcode", "").strip()
    if not bc:
        return jsonify({"found": False, "error": "Empty barcode"})
    try:
        e   = _eng()
        emp = e["get_emp"](bc)
        if emp:
            return jsonify({
                "found":    True,
                "name":     emp["name"],
                "role":     emp.get("role",""),
                "emp_code": emp.get("emp_code",""),
                "id":       emp["id"]
            })
        # Try partial name match as last resort (for typing name on keyboard)
        from modules.sql_adapter import run_query
        name_rows = run_query("""
            SELECT id::text, emp_code, name, role, department,
                   is_active, staff_barcode
            FROM employees
            WHERE is_active = TRUE
              AND LOWER(TRIM(name)) LIKE LOWER(TRIM(%s))
            ORDER BY name LIMIT 1
        """, (f"%{bc}%",)) or []
        if name_rows:
            r = name_rows[0]
            return jsonify({
                "found":    True,
                "name":     r["name"],
                "role":     r.get("role",""),
                "emp_code": r.get("emp_code",""),
                "id":       r["id"],
                "note":     "Matched by name"
            })
        # Not found — show exactly what was scanned to help admin fix it
        return jsonify({
            "found":   False,
            "scanned": bc,
            "hint":    f"No employee with barcode or code '{bc}'. Go to ERP → HR → Scanner Setup → assign barcode to this employee."
        })
    except Exception as ex:
        return jsonify({"found": False, "error": str(ex), "scanned": bc})


def _mobile_attendance_status(emp_id: str, emp_name: str = "") -> dict:
    try:
        from modules.hr.hr_scanner_engine import _attendance_gate_for_stage
        gate = _attendance_gate_for_stage(emp_id, emp_name or "Staff")
        if gate:
            return {
                "ok": False,
                "checked_in": False,
                "message": gate.get("message") or "Check-in required.",
                "action": gate.get("action") or "BLOCKED",
            }
        return {
            "ok": True,
            "checked_in": True,
            "message": "✅ Office check-in verified.",
            "action": "OK",
        }
    except Exception as ex:
        return {
            "ok": False,
            "checked_in": False,
            "message": f"Attendance status unavailable: {ex}",
            "action": "ERROR",
        }


@app.route("/api/attendance_status")
def api_attendance_status():
    staff_id = request.args.get("staff_id", "").strip()
    staff_bc = request.args.get("staff_barcode", "").strip()
    try:
        emp = None
        if staff_bc:
            e = _eng()
            emp = e["get_emp"](staff_bc)
        elif staff_id:
            from modules.sql_adapter import run_query
            rows = run_query("""
                SELECT id::text, name
                FROM employees
                WHERE id = %s::uuid AND is_active=TRUE
                LIMIT 1
            """, (staff_id,))
            emp = rows[0] if rows else None
        if not emp:
            return jsonify({"ok": False, "checked_in": False, "message": "Staff not identified."})
        return jsonify(_mobile_attendance_status(str(emp["id"]), str(emp.get("name") or "")))
    except Exception as ex:
        return jsonify({"ok": False, "checked_in": False, "message": str(ex)})


@app.route("/api/lan_verify", methods=["POST"])
def api_lan_verify():
    """
    LAN verification — confirms physical office presence.
    Called when staff taps LAN Verify on office WiFi.
    Sets lan_verified=TRUE on today's attendance record.
    Unlocks stage scanning.
    """
    data     = request.json or {}
    staff_bc = data.get("staff_barcode","").strip()
    staff_id = data.get("staff_id","").strip()
    try:
        e   = _eng()
        emp = e["get_emp"](staff_bc) if staff_bc else None
        if not emp and staff_id:
            from modules.sql_adapter import run_query
            rows = run_query("""
                SELECT id::text, emp_code, name, phone, role, department,
                       is_active, staff_barcode
                FROM employees WHERE id=%s::uuid AND is_active=TRUE LIMIT 1
            """, (staff_id,))
            emp = rows[0] if rows else None
        if not emp:
            return jsonify({"success": False,
                            "message": "❌ Staff not identified. Scan your barcode first."})

        from modules.hr.hr_scanner_engine import _do_lan_verify
        result = _do_lan_verify(emp["id"], emp["name"])
        return jsonify(result)
    except Exception as ex:
        return jsonify({"success": False, "message": str(ex)})


@app.route("/api/attend", methods=["POST"])
def api_attend():
    data   = request.json or {}
    staff  = data.get("staff_barcode","").strip()
    staff_id = data.get("staff_id","").strip()
    action = data.get("action","").lower()   # "checkin" or "checkout"
    try:
        e   = _eng()
        emp = e["get_emp"](staff) if staff else None
        if not emp and staff_id:
            from modules.sql_adapter import run_query
            rows = run_query("""
                SELECT id::text, emp_code, name, phone, role, department,
                       is_active, staff_barcode
                FROM employees
                WHERE id = %s::uuid AND is_active=TRUE
                LIMIT 1
            """, (staff_id,))
            emp = rows[0] if rows else None
        if not emp:
            return jsonify({"success": False, "action": "ERROR",
                            "message": f"❌ Staff barcode not found: {staff or staff_id}"})

        lat = data.get("lat")
        lng = data.get("lng")
        acc = data.get("acc") or 0
        from modules.hr.hr_scanner_engine import _do_checkin, _do_checkout, has_unclosed_previous_day
        if action == "checkin":
            unclosed = has_unclosed_previous_day(emp["id"])
            if unclosed:
                return jsonify({
                    "success": False,
                    "action": "BLOCKED",
                    "message": (
                        f"⛔ {emp['name']} — Previous day not closed!\n"
                        f"Date: {unclosed['log_date']}  Check-in: {str(unclosed.get('check_in_time',''))[:16]}\n"
                        "Contact manager for clearance before logging in."
                    ),
                })
            if lat is None or lng is None:
                # No GPS (HTTP/plain LAN) — use barcode-based checkin
                # Sets both check_in_valid AND lan_verified = TRUE
                from modules.hr.hr_scanner_engine import _do_checkin
                from modules.sql_adapter import run_write
                result = _do_checkin(emp["id"], emp["name"])
                if result.get("success"):
                    try:
                        run_write("""
                            UPDATE attendance_logs
                            SET check_in_valid  = TRUE,
                                lan_verified     = TRUE,
                                lan_verified_at  = NOW()
                            WHERE employee_id = %s::uuid
                              AND log_date = CURRENT_DATE
                        """, (emp["id"],))
                        result["lan_verified"] = True
                    except Exception:
                        pass
            else:
                from modules.hr.hr_engine import check_in, get_today_attendance
                ok, msg, rec = check_in(emp["id"], float(lat), float(lng), float(acc or 0))
                if not ok and "already checked in" in str(msg).lower():
                    rec = get_today_attendance(emp["id"]) or {}
                    ok = bool(rec.get("check_in_time") and rec.get("check_in_valid") and not rec.get("check_out_time"))
                result = {
                    "success": bool(ok),
                    "action": "CHECKIN" if ok else "BLOCKED",
                    "message": msg,
                    "data": rec,
                }
        else:
            if lat is not None and lng is not None:
                try:
                    from modules.hr.hr_engine import check_out
                    ok, msg, rec = check_out(emp["id"], float(lat), float(lng), float(acc or 0))
                    result = {
                        "success": bool(ok),
                        "action": "CHECKOUT" if ok else "ERROR",
                        "message": msg,
                        "data": rec,
                    }
                except Exception:
                    result = _do_checkout(emp["id"], emp["name"])
            else:
                result = _do_checkout(emp["id"], emp["name"])
        return jsonify(result)
    except Exception as ex:
        return jsonify({"success": False, "action": "ERROR", "message": str(ex)})


@app.route("/api/stage", methods=["POST"])
def api_stage():
    data       = request.json or {}
    staff_bc   = data.get("staff_barcode","").strip()
    staff_id   = data.get("staff_id","").strip()
    order_no   = _clean_order_no(data.get("order_no","").strip())
    stage_code = data.get("stage_code","").strip()
    try:
        e = _eng()
        # Resolve employee — by barcode first, then by id
        emp = e["get_emp"](staff_bc) if staff_bc else None
        if not emp and staff_id:
            from modules.sql_adapter import run_query
            rows = run_query("""
                SELECT id::text, emp_code, name, phone, role, department,
                       is_active, staff_barcode
                FROM employees WHERE id = %s::uuid AND is_active=TRUE LIMIT 1
            """, (staff_id,))
            emp = rows[0] if rows else None
        if not emp:
            return jsonify({"success": False, "action": "ERROR",
                            "message": "❌ Staff not identified. Scan your staff barcode."})

        from modules.hr.hr_scanner_engine import _do_stage
        result = _do_stage(emp["id"], emp["name"], stage_code.upper(), order_no)
        return jsonify(result)
    except Exception as ex:
        return jsonify({"success": False, "action": "ERROR", "message": str(ex)})





@app.route("/api/clear_staff", methods=["POST"])
def api_clear_staff():
    """Admin clearance for unclosed day."""
    data     = request.json or {}
    emp_id   = data.get("emp_id","")
    log_date = data.get("log_date","")
    cleared  = data.get("cleared_by","Admin")
    reason   = data.get("reason","")
    try:
        e  = _eng()
        ok = e["clear_unclosed"](emp_id, log_date, cleared, reason)
        return jsonify({"success": ok})
    except Exception as ex:
        return jsonify({"success": False, "error": str(ex)})


def _base_order_no_for_report(order_no: str) -> str:
    val = str(order_no or "").strip()
    return re.sub(r"-(R|L|F|C|FIT|COL|COLOUR|COLOR)$", "", val, flags=re.I)


@app.route("/api/production_timing_report")
def api_production_timing_report():
    """Order-wise production timing report for mobile Home dashboard."""
    raw_date = (request.args.get("date") or "").strip()
    staff_id = (request.args.get("staff_id") or "").strip()
    try:
        report_date = datetime.date.fromisoformat(raw_date) if raw_date else datetime.date.today()
    except Exception:
        report_date = datetime.date.today()

    try:
        from modules.sql_adapter import run_query
        staff_filter = ""
        params = [report_date.isoformat()]
        staff_name = ""
        if staff_id:
            staff_filter = " AND employee_id = %s::uuid"
            params.append(staff_id)
            try:
                emp_rows = run_query("""
                    SELECT name
                    FROM employees
                    WHERE id = %s::uuid
                    LIMIT 1
                """, (staff_id,)) or []
                staff_name = str(emp_rows[0].get("name") or "") if emp_rows else ""
            except Exception:
                staff_name = ""

        logs = run_query(f"""
            SELECT order_no,
                   order_id::text AS order_id,
                   stage_code,
                   stage_label,
                   emp_name,
                   scanned_at
            FROM production_stage_log
            WHERE DATE(scanned_at) = %s::date
              {staff_filter}
            ORDER BY scanned_at ASC
        """, tuple(params)) or []

        grouped = {}
        for r in logs:
            base = _base_order_no_for_report(r.get("order_no"))
            if not base:
                continue
            g = grouped.setdefault(base, {
                "order_no": base,
                "order_ids": set(),
                "events": [],
            })
            if r.get("order_id"):
                g["order_ids"].add(str(r["order_id"]))
            g["events"].append(r)

        base_orders = list(grouped.keys())
        eye_counts = {k: 0 for k in base_orders}
        if base_orders:
            try:
                counts = run_query("""
                    SELECT o.order_no,
                           COUNT(*) FILTER (
                               WHERE UPPER(COALESCE(ol.eye_side,'')) IN ('R','L')
                           ) AS eye_count
                    FROM orders o
                    JOIN order_lines ol ON ol.order_id = o.id
                       AND COALESCE(ol.is_deleted, FALSE) = FALSE
                    WHERE o.order_no = ANY(%s)
                    GROUP BY o.order_no
                """, (base_orders,)) or []
                for c in counts:
                    eye_counts[str(c.get("order_no"))] = int(c.get("eye_count") or 0)
            except Exception:
                pass

        out = []
        total_pairs = 0.0
        repeated = 0
        for base, g in grouped.items():
            events = g["events"]
            if not events:
                continue
            first = events[0]
            last = events[-1]
            first_dt = first.get("scanned_at")
            last_dt = last.get("scanned_at")
            try:
                clear_minutes = int(round((last_dt - first_dt).total_seconds() / 60))
            except Exception:
                clear_minutes = None

            stage_counts = {}
            for ev in events:
                sc = str(ev.get("stage_code") or "")
                stage_counts[sc] = stage_counts.get(sc, 0) + 1
            repeat_count = max([1] + list(stage_counts.values()))
            if repeat_count > 1:
                repeated += 1

            eyes = int(eye_counts.get(base) or 0)
            pairs = round(eyes / 2, 2) if eyes else 0
            processed_pairs = round((pairs or 1) * repeat_count, 2)
            total_pairs += processed_pairs

            def _hhmm(v):
                try:
                    return v.strftime("%H:%M")
                except Exception:
                    return str(v or "")[11:16]

            out.append({
                "order_no": base,
                "in_time": _hhmm(first_dt),
                "out_time": _hhmm(last_dt),
                "clear_minutes": clear_minutes,
                "first_stage": first.get("stage_label") or first.get("stage_code"),
                "last_stage": last.get("stage_label") or last.get("stage_code"),
                "emp_name": last.get("emp_name") or first.get("emp_name") or "",
                "pairs": pairs,
                "processed_pairs": processed_pairs,
                "repeat_count": repeat_count,
                "scan_count": len(events),
            })

        out.sort(key=lambda x: (x.get("in_time") or "", x.get("order_no") or ""), reverse=True)
        return jsonify({
            "date": report_date.isoformat(),
            "summary": {
                "total_orders": len(out),
                "total_pairs": f"{total_pairs:g}",
                "repeated_orders": repeated,
                "staff_name": staff_name,
            },
            "rows": out,
        })
    except Exception as ex:
        return jsonify({
            "date": report_date.isoformat(),
            "summary": {"total_orders": 0, "total_pairs": "0", "repeated_orders": 0},
            "rows": [],
            "error": str(ex),
        }), 200


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN PAGE — shows unclosed staff, clearance panel
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/my_orders")
def api_my_orders():
    """
    Orders visible to THIS employee — filtered by their allowed stages.
    Attendance gate is advisory only (shows warning) — does NOT block orders.
    Staff can always see their work queue even if check-in was on desktop.
    """
    staff_id = request.args.get("staff_id","").strip()
    staff_bc = request.args.get("staff_barcode","").strip()

    try:
        from modules.sql_adapter import run_query
        from modules.hr.hr_scanner_engine import (
            get_employee_by_barcode, _employee_stage_context,
            allowed_stage_codes_for_context, PRODUCTION_STAGES
        )

        # ── Resolve employee and their allowed stages ──────────────────────
        emp_ctx = {}
        emp     = None
        if staff_bc:
            emp = get_employee_by_barcode(staff_bc)
            if emp:
                emp_ctx = _employee_stage_context(emp["id"])
        if not emp_ctx and staff_id:
            emp_ctx = _employee_stage_context(staff_id)

        # No employee resolved — show all orders (desktop/admin view)
        # Staff who logged in on desktop will have staff_id from localStorage
        if not emp_ctx and not staff_id:
            is_admin = True
            allowed  = []
        else:
            allowed  = allowed_stage_codes_for_context(emp_ctx) if emp_ctx else []
            is_admin = (not allowed) or any(
                x in str(emp_ctx.get("role","")).upper()
                for x in ("ADMIN","MANAGER","OWNER","SUPERVISOR")
            )

        # ── Build stage filter ─────────────────────────────────────────────
        stage_seq      = [code for code, _ in PRODUCTION_STAGES]
        visible_stages = set(allowed)
        include_no_stage = True  # always show newly assigned orders

        if not is_admin and allowed:
            for code in allowed:
                idx = stage_seq.index(code) if code in stage_seq else -1
                if idx > 0:
                    visible_stages.add(stage_seq[idx - 1])
            first_stage      = stage_seq[0] if stage_seq else ""
            include_no_stage = True  # always show blank-assigned not-yet-scanned

        # ── SQL ─────────────────────────────────────────────────────────────
        if is_admin or not visible_stages:
            stage_where  = "TRUE"
            stage_params = []
        else:
            placeholders = ",".join(["%s"] * len(visible_stages))
            no_stage_sql = "OR COALESCE(psl.stage_code,'') = ''" if include_no_stage else ""
            stage_where  = f"(COALESCE(psl.stage_code,'') IN ({placeholders}) {no_stage_sql})"
            stage_params = list(visible_stages)

        rows = run_query(f"""
            SELECT DISTINCT ON (o.id)
                o.order_no,
                COALESCE(o.party_name, o.patient_name, '') AS party_name,
                o.status,
                COALESCE(psl.stage_code,'')             AS current_stage,
                COALESCE(psl.stage_label,'Not started') AS current_stage_label,
                COALESCE(psl.scanned_at::text,'')       AS last_updated,
                STRING_AGG(DISTINCT
                    COALESCE(ol.lens_params->>'product_name',''),
                    ' / ' ORDER BY COALESCE(ol.lens_params->>'product_name','')
                ) FILTER (WHERE COALESCE(ol.lens_params->>'product_name','') != '')
                    AS product_summary
            FROM orders o
            JOIN order_lines ol ON ol.order_id = o.id
                AND COALESCE(ol.is_deleted, FALSE) = FALSE
            JOIN job_master jm ON jm.order_line_id = ol.id
                AND COALESCE(jm.is_closed, FALSE)     = FALSE
                AND COALESCE(jm.blank_allocated_qty,0) > 0
            LEFT JOIN LATERAL (
                SELECT stage_code, stage_label, scanned_at
                FROM production_stage_log
                WHERE order_no = o.order_no
                ORDER BY scanned_at DESC LIMIT 1
            ) psl ON TRUE
            WHERE o.status IN ('CONFIRMED','IN_PRODUCTION',
                               'UNDER_REVIEW','PROCESSING')
              AND COALESCE(ol.batch_status,'PENDING')
                  NOT IN ('CANCELLED','CLOSED','DELIVERED')
              AND {stage_where}
            GROUP BY o.id, o.order_no, o.party_name, o.patient_name,
                     o.status,
                     psl.stage_code, psl.stage_label, psl.scanned_at
            ORDER BY o.id, psl.scanned_at DESC NULLS LAST
            LIMIT 150
        """, stage_params or None) or []

        return jsonify(rows)

    except Exception as ex:
        # Return empty list with error — never block the UI
        return jsonify([])


@app.route("/api/order_production_detail")
def api_order_production_detail():
    """
    Full production detail for one order — shown when lab person taps an order.
    Returns both eyes with: Base, Axis, Tool A, Tool B, SPH/CYL/ADD, blank info.
    """
    order_no = _clean_order_no(request.args.get("order_no","").strip())
    staff_id = request.args.get("staff_id", "").strip()
    staff_bc = request.args.get("staff_barcode", "").strip()
    if not order_no:
        return jsonify({"found": False})
    try:
        from modules.sql_adapter import run_query
        emp = None
        if staff_bc:
            e = _eng()
            emp = e["get_emp"](staff_bc)
        elif staff_id:
            _emp_rows = run_query(
                "SELECT id::text, name FROM employees WHERE id=%s::uuid AND is_active=TRUE LIMIT 1",
                (staff_id,),
            ) or []
            emp = _emp_rows[0] if _emp_rows else None
        # Order detail is read-only — no gate check needed here
        # Stage submission (/api/stage) enforces attendance separately

        rows = run_query("""
            SELECT
                ol.id::text                                     AS line_id,
                COALESCE(ol.eye_side,'')                        AS eye_side,
                ol.sph, ol.cyl, ol.axis, ol.add_power,
                COALESCE(ol.lens_params->>'product_name','')    AS product_name,
                COALESCE(ol.lens_params->>'coating_type','')    AS coating,
                COALESCE(ol.lens_params->>'index_value','')     AS index_val,
                -- surfacing data (saved when blanks assigned)
                ol.lens_params->'surfacing_data'                AS surf_json,
                COALESCE(ol.lens_params->'surfacing_data'->>'blank_brand','')    AS blank_brand,
                COALESCE(ol.lens_params->'surfacing_data'->>'blank_material','') AS blank_material,
                COALESCE(ol.lens_params->'surfacing_data'->>'blank_batch','')    AS blank_batch,
                COALESCE(ol.lens_params->'surfacing_data'->>'base_curve','')     AS base_curve,
                COALESCE(ol.lens_params->'surfacing_data'->>'sph_surf','')       AS sph_surf,
                COALESCE(ol.lens_params->'surfacing_data'->>'cyl_surf','')       AS cyl_surf,
                COALESCE(ol.lens_params->'surfacing_data'->>'axis_surf','')      AS axis_surf,
                COALESCE(ol.lens_params->'surfacing_data'->>'add_power_selected','') AS add_power_selected,
                COALESCE(ol.lens_params->'surfacing_data'->>'tool_a','')         AS tool_a,
                COALESCE(ol.lens_params->'surfacing_data'->>'dia_tool_a','')     AS dia_tool_a,
                COALESCE(ol.lens_params->'surfacing_data'->>'tool_b','')         AS tool_b,
                COALESCE(ol.lens_params->'surfacing_data'->>'dia_tool_b','')     AS dia_tool_b,
                COALESCE(ol.lens_params->'surfacing_data'->>'frame_type','')     AS frame_type,
                -- current production stage
                COALESCE(ol.lens_params->>'production_stage','')       AS production_stage,
                COALESCE(ol.lens_params->>'production_stage_label','') AS production_stage_label,
                COALESCE(ol.lens_params->>'production_stage_by','')    AS stage_by,
                -- job master
                COALESCE(jm.current_stage,'')           AS job_stage,
                COALESCE(jm.blank_allocated_qty,0)      AS blank_allocated_qty,
                COALESCE(jm.blank_required_qty,0)       AS blank_required_qty
            FROM orders o
            JOIN order_lines ol ON ol.order_id = o.id
                AND COALESCE(ol.is_deleted, FALSE) = FALSE
            LEFT JOIN job_master jm ON jm.order_line_id = ol.id
                AND COALESCE(jm.is_closed, FALSE) = FALSE
            WHERE o.order_no = %s
            ORDER BY CASE UPPER(COALESCE(ol.eye_side,''))
                     WHEN 'R' THEN 0 WHEN 'L' THEN 1 ELSE 2 END
        """, (order_no,)) or []

        if not rows:
            return jsonify({"found": False})

        # Also get stage history
        history = run_query("""
            SELECT stage_label, emp_name,
                   TO_CHAR(scanned_at AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') AS time
            FROM production_stage_log
            WHERE order_no = %s
            ORDER BY scanned_at DESC LIMIT 8
        """, (order_no,)) or []

        return jsonify({
            "found":   True,
            "order_no": order_no,
            "lines":   rows,
            "history": history,
        })
    except Exception as ex:
        return jsonify({"found": False, "error": str(ex)})


@app.route("/api/order_detail")
def api_order_detail():
    """Order detail for stage preview."""
    order_no = _clean_order_no(request.args.get("order_no","").strip())
    staff_id = request.args.get("staff_id", "").strip()
    staff_bc = request.args.get("staff_barcode", "").strip()
    if not order_no:
        return jsonify({"found": False})
    try:
        from modules.sql_adapter import run_query
        emp = None
        if staff_bc:
            e = _eng()
            emp = e["get_emp"](staff_bc)
        elif staff_id:
            _emp_rows = run_query(
                "SELECT id::text, name FROM employees WHERE id=%s::uuid AND is_active=TRUE LIMIT 1",
                (staff_id,),
            ) or []
            emp = _emp_rows[0] if _emp_rows else None
        # Order preview is read-only — no attendance gate needed

        rows = run_query("""
            SELECT o.order_no,
                   COALESCE(o.party_name, o.patient_name,'') AS party_name,
                   o.status,
                   COALESCE(psl.stage_code,'')  AS current_stage,
                   COALESCE(psl.stage_label,'') AS current_stage_label,
                   STRING_AGG(DISTINCT
                       COALESCE(ol.lens_params->>'product_name',''),
                       ' / ' ORDER BY COALESCE(ol.lens_params->>'product_name','')
                   ) FILTER (WHERE COALESCE(ol.lens_params->>'product_name','') <> '')
                       AS product_summary
            FROM orders o
            JOIN order_lines ol ON ol.order_id = o.id
                AND COALESCE(ol.is_deleted, FALSE) = FALSE
            LEFT JOIN LATERAL (
                SELECT stage_code, stage_label
                FROM production_stage_log
                WHERE order_no = o.order_no
                ORDER BY scanned_at DESC LIMIT 1
            ) psl ON TRUE
            WHERE o.order_no = %s
            GROUP BY o.order_no, o.party_name, o.patient_name, o.status,
                     psl.stage_code, psl.stage_label
            LIMIT 1
        """, (order_no,)) or []
        if not rows:
            return jsonify({"found": False})
        r = rows[0]
        return jsonify({**r, "found": True})
    except Exception as ex:
        return jsonify({"found": False, "error": str(ex)})
@app.route("/api/today_log")
def api_today_log_v2():
    """Today's log — optionally filtered by emp_id."""
    emp_id = request.args.get("emp_id","").strip()
    try:
        e = _eng()
        return jsonify(e["today_log"](emp_id or None))
    except Exception:
        return jsonify([])


@app.route("/admin")
def admin():
    try:
        e    = _eng()
        rows = e["unclosed"]()
    except Exception:
        rows = []

    cards = ""
    for r in rows:
        cards += f"""
        <div class="card">
            <b>{r['name']}</b> <span style="color:#94a3b8">({r['role']})</span><br>
            <span style="color:#f59e0b">Date: {r['log_date']}  Check-in: {str(r.get('check_in_time',''))[:16]}</span><br>
            <button class="btn btn-yellow" style="margin-top:10px"
                onclick="clearStaff('{r.get('id','')}','{r['log_date']}','{r['name']}')">
                ✅ Clear &amp; Allow Login
            </button>
        </div>"""

    if not cards:
        cards = '<div class="card" style="color:#10b981">✅ All staff checked out properly.</div>'

    html = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin Clearance</title>
<link rel="stylesheet" href="data:,">
<style>
* {{margin:0;padding:0;box-sizing:border-box}}
body {{background:#0a0f1e;color:#f1f5f9;font-family:sans-serif;padding:16px}}
.card {{background:#111827;border:1px solid #1e3a5f;border-radius:12px;padding:16px;margin-bottom:12px}}
.btn {{width:100%;padding:13px;border:none;border-radius:8px;font-size:.95rem;font-weight:700;cursor:pointer}}
.btn-yellow {{background:#f59e0b;color:#000}}
h1 {{color:#3b82f6;margin-bottom:16px;font-size:1.2rem}}
</style>
</head><body>
<h1>🔑 Admin Clearance Panel</h1>
{cards}
<div id="msg" style="margin-top:10px;color:#10b981"></div>
<script>
function clearStaff(emp_id, log_date, name) {{
    if (!confirm('Clear checkout for ' + name + ' on ' + log_date + '?')) return;
    const reason = prompt('Reason (optional):', '') || 'Admin clearance';
    fetch('/api/clear_staff', {{
        method: 'POST',
        headers: {{'Content-Type':'application/json'}},
        body: JSON.stringify({{emp_id, log_date, cleared_by: 'Admin', reason}})
    }}).then(r=>r.json()).then(d=>{{
        document.getElementById('msg').textContent = d.success ? '✅ Cleared. Reload to refresh.' : '❌ Error';
        if (d.success) setTimeout(()=>location.reload(), 1500);
    }});
}}
</script>
</body></html>"""
    return html


# ══════════════════════════════════════════════════════════════════════════════
# RUN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    urls = _lan_urls(8502)
    _safe_print("=" * 60)
    _safe_print("  Parakh Scanner PWA")
    _safe_print("=" * 60)
    for url in urls:
        _safe_print(f"  Main scanner:  {url}")
        _safe_print(f"  Admin panel:   {url}/admin")
        _safe_print(f"  LAN check:     {url}/lan")
    _safe_print()
    _safe_print("  First test on mobile: open /health or /lan")
    _safe_print("  If PC opens but mobile fails: run Fix_Scanner_Firewall_Admin.bat as Administrator.")
    _safe_print("  On mobile: open the URL, then Chrome menu, then Add to Home Screen")
    _safe_print("=" * 60)

    app.run(host="0.0.0.0", port=8502, debug=False, threaded=True)
