"""
modules/backoffice/bonzer_portal.py
=============================================
Bonzer (BonzerLenses) supplier-portal send helper.

Order page:  https://www.bonzerlenses.com/orders/add
It is behind a login wall, so we do NOT prefill via a query string (those
params are dropped on the login bounce and the portal does not read Rx from
the URL). Instead:

  Tier 1 (works today, no Bonzer cooperation needed)
    - one button opens the Bonzer order page in a new tab
    - the full order shown as a one-click copy block, laid out in the SAME
      top-to-bottom order as the Bonzer form so staff fill it in seconds
    - plus a field-by-field Rx grid

  Tier 2 (semi-auto, opt-in)
    - encodes the payload into the URL #fragment + ships a Tampermonkey
      userscript that, after the operator logs in, locates each field by its
      visible label / the Rx table structure and fills it. It NEVER clicks
      Save — staff review and submit. Works without DOM ids because the form
      is a standard labelled layout; account-specific dropdowns (Dealer,
      Master Brand, Price) are left for the operator on purpose.

Mapping is derived from the live form screenshots. Anything uncertain is
flagged in the UI so the operator can correct it.

Public entry point:
    render_bonzer_send(lines, *, order_no, patient_name, patient_mobile,
                       supplier_name, key_prefix)

Display-only: no DB writes, no pipeline-state mutation.
"""
from __future__ import annotations

import base64
import json
import urllib.parse

import streamlit as st

# ─────────────────────────────────────────────────────────────────────────────
BONZER_BASE_URL_DEFAULT = "https://www.bonzerlenses.com"
BONZER_ORDER_PATH = "/orders/add"

# Bonzer radio option labels exactly as they appear on the form.
_FRAME_OPTIONS     = ["Supra", "RIMLESS", "FULL"]
_THICKNESS_OPTIONS = ["Regular", "Thin", "Cartier Thick"]
_UNCOAT_OPTIONS    = ["No", "Yes"]


def _bonzer_order_url() -> str:
    base = str(st.session_state.get("bonzer_base_url") or BONZER_BASE_URL_DEFAULT).rstrip("/")
    return f"{base}{BONZER_ORDER_PATH}"


def _lp_of(line: dict) -> dict:
    lp = line.get("_lp") or line.get("lens_params") or {}
    if isinstance(lp, str):
        try:
            lp = json.loads(lp)
        except Exception:
            lp = {}
    return lp if isinstance(lp, dict) else {}


def _fmt_sph_cyl(v) -> str:
    if v is None or str(v).strip() in ("", "None", "null"):
        return ""
    try:
        return f"{float(v):+.2f}"
    except (ValueError, TypeError):
        return ""


def _fmt_axis(v) -> str:
    if v is None or str(v).strip() in ("", "None", "null"):
        return ""
    try:
        return str(int(float(v)))
    except (ValueError, TypeError):
        return ""


def _fmt_add(v) -> str:
    if v is None or str(v).strip() in ("", "None", "null", "0", "0.0"):
        return ""
    try:
        f = float(v)
        return f"{f:+.2f}" if f > 0 else ""
    except (ValueError, TypeError):
        return ""


def _eye_rx(line: dict) -> dict:
    """Per-eye Rx, mirroring the column→fallback→lens_params resolution used by
    _power_str in supplier_pipeline.py (so plano 0.00 survives)."""
    lp = _lp_of(line)

    def pick(*keys):
        for k in keys:
            if k in line and line[k] is not None and str(line[k]).strip() not in ("", "None"):
                return line[k]
        for k in keys:
            if k in lp and lp[k] is not None and str(lp[k]).strip() not in ("", "None"):
                return lp[k]
        return None

    return {
        "sph":  _fmt_sph_cyl(pick("sph", "sph_val")),
        "cyl":  _fmt_sph_cyl(pick("cyl", "cyl_val")),
        "axis": _fmt_axis(pick("axis", "axis_val")),
        "add":  _fmt_add(pick("add_power", "add")),
    }


# ── value → Bonzer option mappers ────────────────────────────────────────────
def _map_frame(raw: str) -> str:
    s = (raw or "").strip().lower()
    if not s:
        return ""
    if "rim" in s and "less" in s:        # "Rimless", "rim less"
        return "RIMLESS"
    if "supra" in s or "half" in s:       # "Supra", "Half rim"
        return "Supra"
    if "full" in s:
        return "FULL"
    return ""                              # unknown → let operator pick


def _map_thickness(raw: str) -> str:
    s = (raw or "").strip().lower()
    if not s:
        return ""
    if "cartier" in s:
        return "Cartier Thick"
    if "thin" in s:
        return "Thin"
    if "regular" in s or "normal" in s or "standard" in s:
        return "Regular"
    if "thick" in s:                       # generic "thick" → Cartier Thick
        return "Cartier Thick"
    return ""


def _map_uncoat(lp: dict) -> str:
    """Only set Uncoat=Yes when clearly uncoated; else leave Bonzer default (No)."""
    blob = " ".join(
        str(lp.get(k, "")) for k in ("coating", "coating_name", "coating_type",
                                     "treatment", "uncoat")
    ).lower()
    if "uncoat" in blob or "uncoated" in blob:
        return "Yes"
    if str(lp.get("uncoat", "")).strip().lower() in ("yes", "true", "1"):
        return "Yes"
    return ""                              # leave default


def build_bonzer_payload(lines: list, order_no: str, patient_name: str,
                         patient_mobile: str, supplier_name: str,
                         order_type: str = "RETAIL",
                         party_name: str = "",
                         wholesale_customer: str = "") -> dict:
    """Structured payload following the Bonzer form. Pure data.

    order_type         : 'RETAIL' or 'WHOLESALE'
    party_name         : wholesale party/shop name → Dealer ID
    wholesale_customer : customer name entered during punching (authenticity card)
    """
    eyes = {"R": None, "L": None}
    items = []
    frame = thickness = dia = ed = brand = uncoat = mobile = ""

    for ln in lines:
        side = str(ln.get("eye_side", "")).upper()
        side = "R" if side in ("R", "RIGHT") else ("L" if side in ("L", "LEFT") else side)
        lp = _lp_of(ln)
        rx = _eye_rx(ln)
        prod = (
            ln.get("supplier_product_name")
            or ln.get("supplier_product_code")
            or ln.get("our_product_name")
            or ln.get("product_name")
            or ""
        ).split(" | ")[0].strip()
        rec = {
            "eye": side, "product": prod, "qty": int(ln.get("quantity") or 1),
            "sph": rx["sph"], "cyl": rx["cyl"], "axis": rx["axis"], "add": rx["add"],
        }
        if side in eyes and eyes[side] is None:
            eyes[side] = rec
        items.append(rec)

        frame     = frame     or _map_frame(str(lp.get("frame_type", "")))
        thickness = thickness or _map_thickness(str(lp.get("thickness", "")))
        dia       = dia       or str(lp.get("diameter", "")).strip()
        ed        = ed        or str(lp.get("ed", "") or lp.get("ed_mm", "")
                                     or lp.get("fitting_height", "")).strip()
        uncoat    = uncoat    or _map_uncoat(lp)
        brand     = brand     or str(lp.get("brand", "")
                                     or (prod.split()[0] if prod else "")).strip()
        mobile    = mobile    or str(ln.get("patient_mobile", "")
                                     or lp.get("customer_mobile", "")
                                     or lp.get("mobile", "")).strip()

    # Issue 3 — Dealer ID:
    #   Wholesale → party_name (the shop/distributor placing the order)
    #   Retail    → "Parakh Eye Care" (our own shop)
    is_wholesale = str(order_type or "").upper() == "WHOLESALE"
    dealer_id = party_name.strip() if is_wholesale and party_name.strip() else "Parakh Eye Care"

    # Issue 4 — Customer name:
    #   Retail    → patient_name (the end customer)
    #   Wholesale → wholesale_customer (name entered during punching for
    #               authenticity card); fall back to party_name
    if is_wholesale:
        customer_name = (wholesale_customer or party_name or patient_name or "").strip()
    else:
        customer_name = (patient_name or "").strip()

    return {
        "order_no":        order_no or "",
        "customer_name":   customer_name,
        "customer_mobile": (patient_mobile or mobile or "").strip(),
        "supplier":        supplier_name or "",
        "dealer_id":       dealer_id,
        "master_brand":    brand,
        "frame":           frame,
        "thickness":       thickness if thickness in _THICKNESS_OPTIONS else "",
        "uncoat":          uncoat if uncoat in _UNCOAT_OPTIONS else "",
        "dia":             dia,
        "ed":              ed,
        "right":           eyes["R"],
        "left":            eyes["L"],
        "items":           items,
        "notes":           str(_lp_of(lines[0]).get("instructions", "")).strip() if lines else "",
    }


def _payload_to_text(p: dict) -> str:
    """Copy block laid out in the SAME order as the Bonzer form."""
    L = []
    L.append(f"Order No.: {p['order_no'] or '—'}")
    L.append(f"Dealer ID: {p.get('dealer_id') or '(pick on page)'}")
    L.append("Dealer: (pick on page)")
    L.append(f"Master Brand Name: {p['master_brand'] or '(pick on page)'}")
    L.append("")
    L.append("Lenses Prescription")
    L.append(f"{'':<6}{'SPH':>8}{'CYL':>8}{'AXIS':>6}{'ADD':>8}")
    for side, lbl in (("right", "Right"), ("left", "Left")):
        r = p[side]
        if not r:
            L.append(f"{lbl:<6}{'—':>8}{'—':>8}{'—':>6}{'—':>8}")
        else:
            L.append(f"{lbl:<6}{(r['sph'] or '—'):>8}{(r['cyl'] or '—'):>8}"
                     f"{(r['axis'] or '—'):>6}{(r['add'] or '—'):>8}")
    L.append("")
    L.append(f"Customer Name: {p['customer_name'] or '—'}")
    L.append(f"Customer Mobile: {p['customer_mobile'] or '—'}")
    L.append(f"Frame: {p['frame'] or '(select)'}")
    L.append(f"Dia: {p['dia'] or '—'}")
    L.append(f"Thickness: {p['thickness'] or '(select)'}")
    L.append(f"Uncoat: {p['uncoat'] or 'No'}")
    L.append(f"ED: {p['ed'] or '—'} mm")
    L.append("Price: (Select item on page)")
    desc = []
    if p["right"]:
        desc.append(f"R {p['right']['product']} x{p['right']['qty']}")
    if p["left"]:
        desc.append(f"L {p['left']['product']} x{p['left']['qty']}")
    if p["notes"]:
        desc.append(p["notes"])
    if p["order_no"]:
        desc.append(f"Ref {p['order_no']}")
    L.append(f"Description: {' | '.join(desc) if desc else '—'}")
    return "\n".join(L)


def _userscript() -> str:
    """Tampermonkey script: reads payload from URL #fragment, fills the
    BonzerLenses form by label/table structure, never submits."""
    return r"""// ==UserScript==
// @name         BonzerLenses Rx Autofill (ERP)
// @namespace    erp.bonzer.autofill
// @match        *://www.bonzerlenses.com/orders/add*
// @match        *://bonzerlenses.com/orders/add*
// @grant        none
// @run-at       document-idle
// ==/UserScript==
(function () {
  var h = location.hash || "";
  var m = h.match(/erprx=([^&]+)/);
  if (!m) return;
  var P;
  try { P = JSON.parse(decodeURIComponent(escape(atob(decodeURIComponent(m[1]))))); }
  catch (e) { console.warn("Bonzer autofill: bad payload", e); return; }

  var $ = window.jQuery || window.$;

  function fire(el) {
    el.dispatchEvent(new Event("input",  { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new Event("blur",   { bubbles: true }));
    if ($) { try { $(el).trigger("input").trigger("change").trigger("blur"); } catch (e) {} }
  }
  function setVal(el, v) {
    if (!el || v === undefined || v === null || v === "") return false;
    el.focus(); el.value = v; fire(el); return true;
  }

  // Find the input/select/textarea that belongs to a row whose label text
  // starts with `txt` (ignores the red * and whitespace).
  function fieldByLabel(txt, tag) {
    txt = txt.toLowerCase();
    var labels = document.querySelectorAll("label, td, th, div, span");
    for (var i = 0; i < labels.length; i++) {
      var t = (labels[i].textContent || "").replace(/\*/g, "").trim().toLowerCase();
      if (t === txt || t.replace(/[:.]/g, "") === txt.replace(/[:.]/g, "")) {
        var n = labels[i];
        for (var up = 0; up < 4 && n; up++, n = n.parentElement) {
          var c = n.querySelector(tag || "input,select,textarea");
          if (c) return c;
          if (n.nextElementSibling && n.nextElementSibling.querySelector) {
            var c2 = n.nextElementSibling.querySelector(tag || "input,select,textarea");
            if (c2) return c2;
          }
        }
      }
    }
    return null;
  }

  // Pick a radio whose visible label text matches `optText`.
  function setRadio(optText) {
    if (!optText) return;
    var radios = document.querySelectorAll('input[type=radio]');
    for (var i = 0; i < radios.length; i++) {
      var r = radios[i], lbl = "";
      if (r.id) {
        var L = document.querySelector('label[for="' + r.id + '"]');
        if (L) lbl = L.textContent;
      }
      if (!lbl && r.parentElement) lbl = r.parentElement.textContent;
      if (!lbl && r.nextSibling) lbl = r.nextSibling.textContent || "";
      if ((lbl || "").trim().toLowerCase() === optText.trim().toLowerCase()) {
        r.checked = true; fire(r); return;
      }
    }
  }

  // Select an <option> by visible text contains (select2-aware via jQuery).
  function setSelectByText(sel, want) {
    if (!sel || !want) return;
    want = want.toLowerCase();
    var opts = sel.options, hit = -1;
    for (var i = 0; i < opts.length; i++) {
      if ((opts[i].text || "").toLowerCase().indexOf(want) !== -1) { hit = i; break; }
    }
    if (hit >= 0) {
      sel.selectedIndex = hit;
      if ($) { try { $(sel).val(opts[hit].value).trigger("change"); } catch (e) {} }
      fire(sel);
    }
  }

  // Fill the R/L prescription table by matching BOTH the row label (Right/Left)
  // AND each column header (SPH/CYL/AXIS/ADD) so axis never lands in add.
  // Issue 1 fix: positional fill breaks when Bonzer adds/reorders columns.
  function fillRx(rowLabel, rec) {
    if (!rec) return;
    var tables = document.querySelectorAll("table");
    for (var t = 0; t < tables.length; t++) {
      var tbl  = tables[t];
      var rows = tbl.querySelectorAll("tr");
      // Find the header row to build col→index map
      var colMap = {};
      for (var h = 0; h < rows.length; h++) {
        var cells = rows[h].querySelectorAll("td,th");
        var foundHdr = false;
        for (var c = 0; c < cells.length; c++) {
          var hdr = (cells[c].textContent || "").replace(/\*/g,"").trim().toUpperCase();
          if (hdr === "SPH" || hdr === "CYL" || hdr === "AXIS" || hdr === "ADD") {
            colMap[hdr] = c; foundHdr = true;
          }
        }
        if (foundHdr) break;
      }
      // Find the data row matching Right/Left
      for (var r = 0; r < rows.length; r++) {
        var first = (rows[r].cells && rows[r].cells[0]
                     ? rows[r].cells[0].textContent : "").trim().toLowerCase();
        if (first !== rowLabel.toLowerCase()) continue;
        var allIns = rows[r].querySelectorAll(
          "input[type=text],input[type=number],input:not([type])");
        // If we have a column map, use it; otherwise fall back to position
        if (Object.keys(colMap).length >= 3) {
          var fieldMap = { SPH: rec.sph, CYL: rec.cyl, AXIS: rec.axis, ADD: rec.add };
          for (var key in fieldMap) {
            if (colMap[key] !== undefined && fieldMap[key]) {
              // inputs in the row align with td cells; find input inside that cell
              var cell = rows[r].cells[colMap[key]];
              if (cell) {
                var inp = cell.querySelector("input");
                if (inp) setVal(inp, fieldMap[key]);
              }
            }
          }
        } else {
          // Fallback: positional
          var vals = [rec.sph, rec.cyl, rec.axis, rec.add];
          for (var k = 0; k < allIns.length && k < 4; k++) setVal(allIns[k], vals[k]);
        }
        return;
      }
    }
  }

  // Find field by id/name/placeholder/aria-label/data-label attribute.
  // Stronger than fieldByLabel for fields with hidden or missing visible labels.
  function fieldByAttr(keys, tag) {
    tag = tag || "input,select,textarea";
    var els = document.querySelectorAll(tag);
    keys = keys.map(function(k){ return k.toLowerCase().replace(/[^a-z0-9]/g, ""); });
    for (var i = 0; i < els.length; i++) {
      var blob = [
        els[i].id || "",
        els[i].name || "",
        els[i].placeholder || "",
        els[i].getAttribute("aria-label") || "",
        els[i].getAttribute("data-label") || ""
      ].join(" ").toLowerCase().replace(/[^a-z0-9]/g, "");
      for (var k = 0; k < keys.length; k++) {
        if (blob.indexOf(keys[k]) !== -1) return els[i];
      }
    }
    return null;
  }

  function run() {
    // Order No — direct hardcoded selector (id and name confirmed from Bonzer DevTools)
    var orderNoEl =
      document.getElementById("order-no-distributor") ||
      document.querySelector('input[name="order_no_distributor"]') ||
      fieldByLabel("our order no", "input");
    setVal(orderNoEl, P.order_no);

    setVal(fieldByLabel("customer name", "input"), P.customer_name);
    var mob = fieldByLabel("customer mobile", "input")
           || fieldByLabel("mobile", "input")
           || fieldByLabel("mobile no", "input")
           || fieldByLabel("customer mobile no", "input");
    setVal(mob, P.customer_mobile);

    // Issue 3 fix: fill Dealer ID from payload
    var dealerIdEl = fieldByLabel("dealer id", "input")
                  || fieldByLabel("dealerid", "input")
                  || fieldByLabel("dealer id.", "input");
    setVal(dealerIdEl, P.dealer_id);

    fillRx("Right", P.right);
    fillRx("Left",  P.left);

    setRadio(P.frame);
    setRadio(P.thickness);
    if (P.uncoat) setRadio(P.uncoat);

    setVal(fieldByLabel("dia", "input"), P.dia);
    setVal(fieldByLabel("ed",  "input"), P.ed);
    setSelectByText(fieldByLabel("master brand name", "select"), P.master_brand);

    var bits = [];
    if (P.right) bits.push("R " + P.right.product + " x" + P.right.qty);
    if (P.left)  bits.push("L " + P.left.product  + " x" + P.left.qty);
    if (P.notes) bits.push(P.notes);
    if (P.order_no) bits.push("Ref " + P.order_no);
    setVal(fieldByLabel("description", "textarea"), bits.join(" | "));

    console.log("Bonzer autofill done. Review Dealer/Brand/Price, then Save.");
  }

  var tries = 0, iv = setInterval(function () {
    tries++;
    if (document.querySelector("table input") ||
        fieldByLabel("order no", "input")) {
      clearInterval(iv); setTimeout(run, 300);
    } else if (tries > 60) {                    // ~30s
      clearInterval(iv);
      console.warn("Bonzer autofill: form never appeared.");
    }
  }, 500);
})();
"""


def render_bonzer_send(lines: list, *, order_no: str = "",
                        patient_name: str = "", patient_mobile: str = "",
                        supplier_name: str = "", key_prefix: str = "bonzer",
                        order_type: str = "RETAIL",
                        party_name: str = "",
                        wholesale_customer: str = "") -> None:
    """Display-only Bonzer send panel. Call inside an expander/container."""
    rl = [l for l in lines
          if str(l.get("eye_side", "")).upper() in ("R", "L", "RIGHT", "LEFT")]
    if not rl:
        st.caption("No R/L lens lines to send to Bonzer.")
        return

    p = build_bonzer_payload(rl, order_no, patient_name, patient_mobile,
                             supplier_name, order_type=order_type,
                             party_name=party_name,
                             wholesale_customer=wholesale_customer)
    text_block = _payload_to_text(p)
    order_url = _bonzer_order_url()

    st.markdown(
        "<div style='background:#0f172a;border:1px solid #1e293b;border-radius:8px;"
        "padding:8px 12px;margin:2px 0 8px;font-size:0.78rem;color:#cbd5e1'>"
        "🌐 <b>BonzerLenses</b> — opens the order page; log in once, then the "
        "block below maps 1:1 to the form (top → bottom). You review &amp; Save."
        "</div>",
        unsafe_allow_html=True,
    )

    st.link_button("🌐 Open BonzerLenses order page", order_url,
                   use_container_width=True, type="primary")

    st.caption("Copy this whole block — it follows the Bonzer form order:")
    st.code(text_block, language=None)

    with st.container(border=True):
        st.markdown("**Prescription — field by field**")
        hdr = st.columns([1.1, 1, 1, 1, 1])
        for c, t in zip(hdr, ["", "SPH", "CYL", "Axis", "Add"]):
            c.markdown(f"<span style='font-size:0.7rem;color:#64748b'>{t}</span>",
                       unsafe_allow_html=True)
        for side, label in (("right", "Right"), ("left", "Left")):
            rec = p[side]
            row = st.columns([1.1, 1, 1, 1, 1])
            row[0].markdown(f"**{label}**")
            vals = ((rec["sph"], rec["cyl"], rec["axis"], rec["add"])
                    if rec else ("—", "—", "—", "—"))
            for cell, v in zip(row[1:], vals):
                cell.code(v or "—", language=None)
        st.caption(
            f"Frame: **{p['frame'] or '⚠ pick on page'}**  ·  "
            f"Thickness: **{p['thickness'] or '⚠ pick on page'}**  ·  "
            f"Uncoat: **{p['uncoat'] or 'No'}**  ·  "
            f"Dia: **{p['dia'] or '—'}**  ·  ED: **{p['ed'] or '—'}**"
        )
        st.caption(
            "Dealer / Dealer ID / Master Brand / Price are your Bonzer "
            "account values — choose those on the page."
        )

    with st.expander("⚙️ Semi-auto autofill (Tampermonkey, one-time setup)",
                      expanded=False):
        enc = base64.b64encode(
            json.dumps(p, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        auto_url = f"{order_url}#erprx={urllib.parse.quote(enc)}"
        st.markdown(
            "1. Install **Tampermonkey**, add the script below **once**.\n"
            "2. Use the autofill link instead of the plain one. Log in if "
            "asked — the script then fills Order No., R/L Rx, Customer, "
            "Frame, Thickness, Dia, ED and Description.\n"
            "3. You pick **Dealer / Master Brand / Price** and click **Save**."
        )
        st.link_button("🤖 Open Bonzer with autofill payload", auto_url,
                        use_container_width=True)
        st.caption("Userscript (install once):")
        st.code(_userscript(), language="javascript")
        st.caption(
            "It locates fields by their on-screen labels and the Rx table, so "
            "it survives id changes, and never clicks Save — staff always "
            "review. If a field is missed, tell me which and I'll tighten it."
        )
