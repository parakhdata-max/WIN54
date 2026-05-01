"""
modules/printing/camera_scanner.py
=====================================
Camera barcode scanner using ZXing-js.
Works on any phone browser — no app, no extra packages.

How it works:
  1. ZXing reads camera frames and detects barcodes
  2. JS injects the scanned value into a Streamlit text_input via DOM
  3. Streamlit reads it on next rerun — standard text_input flow

Usage:
    from modules.printing.camera_scanner import render_camera_scanner
    
    code = st.text_input("Camera scan result", key="cam_out", label_visibility="collapsed")
    render_camera_scanner(target_label="Camera scan result")
    if code:
        handle_scan(code)
"""

import streamlit as st
import streamlit.components.v1 as components


def _build_scanner_html(target_label: str, auto_send: bool = True) -> str:
    """Build the camera scanner HTML — injects result into Streamlit input by label."""
    auto_ms = 800 if auto_send else 99999  # auto-send delay in ms

    return f"""
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
.sw{{background:#0f172a;border-radius:10px;overflow:hidden;font-family:Arial,sans-serif}}
.sh{{background:#1e293b;padding:8px 12px;display:flex;align-items:center;justify-content:space-between}}
.sh-lbl{{color:#94a3b8;font-size:11px;font-weight:700;letter-spacing:.05em;display:flex;align-items:center;gap:6px}}
.dot{{width:6px;height:6px;border-radius:50%;background:#64748b;
     transition:background .3s;display:inline-block}}
.dot.on{{background:#10b981;animation:blink 1.2s infinite}}
@keyframes blink{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
.sh-st{{color:#475569;font-size:10px}}
.vw{{position:relative;background:#000;width:100%}}
video{{width:100%;display:block;max-height:300px;object-fit:cover}}
.aim{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
      width:160px;height:100px;pointer-events:none}}
.c{{position:absolute;width:20px;height:20px;border-color:#22d3ee;border-style:solid}}
.tl{{top:0;left:0;border-width:2.5px 0 0 2.5px}}
.tr{{top:0;right:0;border-width:2.5px 2.5px 0 0}}
.bl{{bottom:0;left:0;border-width:0 0 2.5px 2.5px}}
.br{{bottom:0;right:0;border-width:0 2.5px 2.5px 0}}
.sl{{position:absolute;left:6px;right:6px;height:1.5px;background:#22d3ee;
     opacity:.7;animation:scan 2s ease-in-out infinite}}
@keyframes scan{{0%{{top:5%}}50%{{top:88%}}100%{{top:5%}}}}
.rb{{padding:8px 12px;display:flex;align-items:center;gap:8px;min-height:38px}}
.rc{{color:#22d3ee;font-family:monospace;font-size:13px;font-weight:700;flex:1}}
.rp{{color:#475569;font-size:11px}}
.btns{{padding:6px 12px 10px;display:flex;gap:6px}}
button{{border:none;border-radius:6px;padding:7px 12px;font-size:11px;font-weight:700;cursor:pointer;outline:none}}
#b-start{{background:#0ea5e9;color:#fff;flex:1}}
#b-stop{{background:#334155;color:#64748b;flex:1;display:none}}
.csel{{background:#1e293b;border:0.5px solid #334155;color:#94a3b8;
       border-radius:5px;padding:5px 8px;font-size:10px;
       width:calc(100% - 24px);margin:6px 12px 0;display:none}}
</style>

<div class="sw">
  <div class="sh">
    <span class="sh-lbl">Camera Scanner</span>
    <span class="sh-st">
      <span class="dot" id="dot" style="display:inline-block;vertical-align:middle"></span>
      <span id="st-txt" style="vertical-align:middle;margin-left:4px">Ready</span>
    </span>
  </div>

  <select class="csel" id="csel"></select>

  <div class="vw">
    <video id="vid" playsinline autoplay muted></video>
    <div class="aim" id="aim" style="display:none">
      <div class="c tl"></div><div class="c tr"></div>
      <div class="c bl"></div><div class="c br"></div>
      <div class="sl" id="sl"></div>
    </div>
  </div>

  <div class="rb">
    <span class="rp" id="rp">Point at barcode — hold steady 2-3cm away</span>
    <span class="rc" id="rc" style="display:none"></span>
  </div>
  <div style="padding:0 12px 4px;font-size:10px;color:#475569" id="debug-txt"></div>

  <div class="btns">
    <button id="b-start" onclick="startScan()">Start Camera</button>
    <button id="b-stop"  onclick="stopScan()">Stop</button>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/@zxing/library@latest/umd/index.min.js"></script>
<script>
var rdr=null, last=null, scanning=false, sendTimer=null;
var TARGET = {target_label!r};
var AUTO_MS = {auto_ms};

window.setStatus = function(txt, active) {{
  document.getElementById('st-txt').textContent = txt;
  var dot = document.getElementById('dot');
  dot.className = 'dot' + (active ? ' on' : '');
  dot.style.background = active ? '' : (txt.indexOf('Error') >= 0 || txt.indexOf('blocked') >= 0 ? '#f59e0b' : '#64748b');
}}

window.injectValue = function(val) {{
  try {{
    var doc = window.parent.document;
    var inputs = doc.querySelectorAll('input[type="text"], input[type="search"]');
    for (var i=0; i<inputs.length; i++) {{
      var id = inputs[i].id || '';
      var lbl = doc.querySelector('label[for="'+id+'"]');
      if (!lbl) {{
        // try aria-label
        if (inputs[i].getAttribute('aria-label') === TARGET) {{
          lbl = {{textContent: TARGET}};
        }}
      }}
      if (lbl && lbl.textContent.trim() === TARGET) {{
        var setter = Object.getOwnPropertyDescriptor(
          window.HTMLInputElement.prototype, 'value').set;
        setter.call(inputs[i], val);
        inputs[i].dispatchEvent(new Event('input', {{bubbles:true}}));
        inputs[i].dispatchEvent(new Event('change', {{bubbles:true}}));
        // Also try React's synthetic event
        inputs[i].dispatchEvent(new KeyboardEvent('keydown', {{key:'Enter',bubbles:true}}));
        return true;
      }}
    }}
  }} catch(e) {{}}
  return false;
}}

window.startScan = async function() {{
  document.getElementById('b-start').style.display='none';
  document.getElementById('b-stop').style.display='flex';
  setStatus('Starting...', false);
  try {{
    // Set hints to prioritize Code128 (frame stickers) and QR
    var hints = new Map();
    var formats = [
      ZXing.BarcodeFormat.CODE_128,
      ZXing.BarcodeFormat.CODE_39,
      ZXing.BarcodeFormat.EAN_13,
      ZXing.BarcodeFormat.EAN_8,
      ZXing.BarcodeFormat.QR_CODE,
      ZXing.BarcodeFormat.DATA_MATRIX,
    ];
    hints.set(ZXing.DecodeHintType.POSSIBLE_FORMATS, formats);
    hints.set(ZXing.DecodeHintType.TRY_HARDER, true);
    rdr = new ZXing.BrowserMultiFormatReader(hints, 300);  // scan every 300ms

    // Use native browser API — works with all ZXing versions
    var devs = [];
    try {{
      var allDevs = await navigator.mediaDevices.enumerateDevices();
      devs = allDevs.filter(function(d) {{ return d.kind === 'videoinput'; }});
    }} catch(e2) {{ devs = []; }}

    var did = undefined;
    var sel = document.getElementById('csel');

    if (devs.length > 1) {{
      sel.style.display = 'block';
      sel.innerHTML = '';
      devs.forEach(function(d, i) {{
        var o = document.createElement('option');
        o.value = d.deviceId;
        o.text  = d.label || ('Camera ' + (i+1));
        // Prefer rear/back camera on phones
        if (d.label.match(/back|rear|environment/i)) {{
          o.selected = true;
          did = d.deviceId;
        }}
        sel.appendChild(o);
      }});
      if (!did && devs.length) did = devs[devs.length-1].deviceId;
      sel.onchange = function() {{ stopScan(); setTimeout(startScan, 300); }};
    }} else if (devs.length === 1) {{
      did = devs[0].deviceId;
    }}

    scanning = true;
    document.getElementById('aim').style.display = 'block';
    setStatus('Scanning...', true);

    var attempts = 0;
    rdr.decodeFromVideoDevice(did, 'vid', function(res, err) {{
      attempts++;
      if (res && scanning) {{
        var code = res.getText();
        if (code !== last) {{ last = code; onFound(code); }}
      }} else if (attempts % 10 === 0) {{
        var dbg = document.getElementById('debug-txt');
        if (dbg) dbg.textContent = 'Scanning... attempt ' + attempts + 
          (err ? ' (' + (err.message||'no barcode').slice(0,30) + ')' : '');
      }}
    }});

  }} catch(e) {{
    var msg = e.message || String(e);
    if (msg.match(/permission|denied|NotAllowed/i)) {{
      setStatus('Camera blocked — needs HTTPS', false);
    }} else if (msg.match(/NotFound|no camera/i)) {{
      setStatus('No camera found', false);
    }} else {{
      setStatus('Error: ' + msg.slice(0,50), false);
    }}
    document.getElementById('b-start').style.display = 'flex';
    document.getElementById('b-stop').style.display  = 'none';
  }}
}}

window.stopScan = function() {{
  scanning=false;
  if(rdr){{try{{rdr.reset();}}catch(e){{}}}}
  document.getElementById('aim').style.display='none';
  document.getElementById('b-start').style.display='flex';
  document.getElementById('b-stop').style.display='none';
  setStatus('Stopped', false);
}}

window.onFound = function(code) {{
  document.getElementById('rp').style.display='none';
  document.getElementById('rc').style.display='block';
  document.getElementById('rc').textContent=code;
  setStatus('Found!', true);
  if(sendTimer) clearTimeout(sendTimer);
  sendTimer = setTimeout(function() {{
    if(last===code) {{
      var ok = injectValue(code);
      setStatus(ok ? 'Sent: '+code : 'Scan again', ok);
      document.getElementById('rc').style.color = ok ? '#10b981' : '#f59e0b';
      setTimeout(function() {{
        last=null;
        document.getElementById('rp').style.display='block';
        document.getElementById('rc').style.display='none';
        document.getElementById('rc').style.color='#22d3ee';
        if(scanning) setStatus('Scanning...', true);
      }}, 1500);
    }}
  }}, AUTO_MS);
}}
</script>
"""


def render_camera_scanner(
    target_label: str = "Scan result",
    height: int = 350,
    auto_send: bool = True,
) -> None:
    """
    Render the camera scanner widget.
    When a barcode is detected, it injects the value into the Streamlit
    text_input whose label matches `target_label`.

    Pair with a text_input using the same label:
        code = st.text_input("Scan result", key="my_scan", label_visibility="collapsed")
        render_camera_scanner(target_label="Scan result")
    """
    components.html(
        _build_scanner_html(target_label, auto_send),
        height=height,
        scrolling=False
    )


def render_camera_scanner_section(
    key: str = "cam",
    height: int = 350,
) -> str:
    """
    Complete scanner section: toggle camera/manual + text input + camera widget.
    Returns the scanned/typed value (uppercase stripped), or empty string.
    """
    use_camera = st.toggle("Use phone camera", value=True, key=f"{key}_toggle")

    label = f"_scan_input_{key}"

    # The text input that receives the injected value
    result = st.text_input(
        label,
        key=f"{key}_input",
        placeholder="Scan with camera above, or type barcode here",
        label_visibility="collapsed" if use_camera else "visible",
    ).strip().upper()

    if use_camera:
        render_camera_scanner(target_label=label, height=height)

    # Strictly return string only — never a Streamlit component object
    return result if isinstance(result, str) else ""
