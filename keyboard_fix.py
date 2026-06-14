"""
keyboard_fix.py  —  install_keyboard_fix()
Fixes for Streamlit UX:
  1. Arrow/Page keys scroll the page
  2. Arrow keys navigate open dropdown
  3. Enter commits the highlighted dropdown option
  4. Touchpad/mouse press on an option commits before BaseWeb blur collapses it

Usage — one line in app.py after set_page_config():
    from keyboard_fix import install_keyboard_fix
    install_keyboard_fix()
"""
import streamlit.components.v1 as stc

_VERSION = "kbfix-v13-2026-06-14"

_JS = """
<script>
(function(){
  var V = '""" + _VERSION + """';
  var P = window.parent !== window ? window.parent : window;
  var D = P.document;
  if (!D || D['__' + V]) return;
  D['__' + V] = true;

  /* ── Helpers ─────────────────────────────────────────────── */
  function vis(el) {
    if (!el) return false;
    var r = el.getBoundingClientRect();
    if (r.width<=0||r.height<=0) return false;
    try { var s=P.getComputedStyle(el); return s.display!=='none'&&s.visibility!=='hidden'; }
    catch(e) { return true; }
  }

  function isTyping() {
    var el = D.activeElement;
    if (!el) return false;
    var t = (el.tagName||'').toLowerCase();
    return t==='input'||t==='textarea'||t==='select'||el.isContentEditable||
           (el.closest&&!!el.closest('[contenteditable="true"]'));
  }

  function openListbox() {
    /* Check parent doc + all child iframes */
    var docs=[D];
    try {
      var fr=D.querySelectorAll('iframe');
      for (var i=0;i<fr.length;i++) {
        try { if(fr[i].contentDocument) docs.push(fr[i].contentDocument); } catch(e){}
      }
    } catch(e){}
    if (document!==D) docs.push(document);
    for (var d=0;d<docs.length;d++) {
      var lbs=docs[d].querySelectorAll('[role="listbox"]');
      for (var i=lbs.length-1;i>=0;i--) { if(vis(lbs[i])) return lbs[i]; }
    }
    return null;
  }

  function visOpts(lb) {
    return lb ? Array.prototype.filter.call(lb.querySelectorAll('[role="option"]'),vis) : [];
  }

  var activeOpt = new WeakMap();
  var pendingMouseOpt = null;
  var pendingMouseAt = 0;

  function curIdx(os) {
    var lb = os.length ? os[0].closest('[role="listbox"]') : null;
    var active = lb ? activeOpt.get(lb) : null;
    if (active) {
      for (var a=0;a<os.length;a++) {
        if (os[a] === active) return a;
      }
    }
    for (var i=0;i<os.length;i++) {
      if (os[i].getAttribute('aria-selected')==='true'||
          os[i].getAttribute('data-highlighted')==='true') return i;
    }
    return 0;
  }

  /* Keyboard-only highlight — scroll into view, fire mouseover for BaseWeb */
  function kbHighlight(opt) {
    if (!opt) return;
    try {
      var lb=opt.closest('[role="listbox"]');
      if (lb) {
        activeOpt.set(lb,opt);
        Array.prototype.forEach.call(lb.querySelectorAll('[role="option"].kbfix-active'),function(x){
          x.classList.remove('kbfix-active');
        });
      }
      opt.classList.add('kbfix-active');
    } catch(e){}
    try { opt.scrollIntoView({block:'nearest'}); } catch(e){}
    /* Fire native hover events so BaseWeb also highlights the option */
    try {
      ['mouseover','mouseenter','mousemove'].forEach(function(t){
        opt.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:P}));
      });
    } catch(e){}
  }

  /* Keyboard-only commit */
  function kbCommit(opt) {
    if (!opt) return;
    kbHighlight(opt);
    try { opt.click(); } catch(e){}
    /* Fallback: send Enter to the combobox input */
    try {
      var doc=opt.ownerDocument, lb=opt.closest('[role="listbox"]');
      var inp=lb&&(doc.querySelector('[role="combobox"]')||doc.querySelector('[data-baseweb="select"] input'));
      if (inp) {
        ['keydown','keyup'].forEach(function(t){
          inp.dispatchEvent(new KeyboardEvent(t,{key:'Enter',code:'Enter',keyCode:13,
            which:13,bubbles:true,cancelable:true,composed:true}));
        });
      }
    } catch(e){}
  }

  function nearestOption(t) {
    return t && t.closest ? t.closest('[role="option"]') : null;
  }

  function isScrollbarGesture(e) {
    var lb = e.target && e.target.closest ? e.target.closest('[role="listbox"]') : null;
    if (!lb) return false;
    var r = lb.getBoundingClientRect();
    return e.clientX >= (r.right - 18);
  }

  /*
    Touchpad/mouse selection fix:
    BaseWeb sometimes closes the popover on press before the option click reaches
    React. Keep focus in place, then commit on release. We skip the scrollbar
    edge so inner dropdown scrolling still works.
  */
  function onOptionDown(e) {
    if (e.button !== undefined && e.button !== 0) return;
    if (isScrollbarGesture(e)) return;
    var opt = nearestOption(e.target);
    if (!opt || !vis(opt)) return;
    pendingMouseOpt = opt;
    pendingMouseAt = Date.now();
    kbHighlight(opt);
    e.preventDefault();
    e.stopPropagation();
  }

  function onOptionUp(e) {
    if (!pendingMouseOpt) return;
    if (isScrollbarGesture(e)) { pendingMouseOpt=null; return; }
    var opt = nearestOption(e.target) || pendingMouseOpt;
    var age = Date.now() - pendingMouseAt;
    pendingMouseOpt = null;
    if (!opt || !vis(opt) || age > 2500) return;
    e.preventDefault();
    e.stopPropagation();
    P.setTimeout(function(){ kbCommit(opt); },0);
  }

  /* Page scroll */
  function pageScroller() {
    var c=[D.querySelector('[data-testid="stAppViewContainer"]'),
           D.querySelector('[data-testid="stMain"]'),
           D.querySelector('.main .block-container'),
           D.querySelector('section.main'),
           D.scrollingElement,D.documentElement,D.body];
    for (var i=0;i<c.length;i++) { if(c[i]&&c[i].scrollHeight>c[i].clientHeight+10) return c[i]; }
    return D.scrollingElement||D.body;
  }
  function scrollPage(delta) {
    var el=pageScroller();
    try { el.scrollBy({top:delta,behavior:'smooth'}); } catch(e){ if(el) el.scrollTop+=delta; }
  }

  /* ── 1. Keyboard handler ─────────────────────────────────── */
  function onKey(e) {
    if (e.altKey||e.ctrlKey||e.metaKey) return;
    var key=e.key, lb=openListbox(), os=lb?visOpts(lb):[];

    /* Arrow keys inside open dropdown */
    if (os.length&&['ArrowDown','ArrowUp','Home','End','Enter'].indexOf(key)>=0) {
      var cur=curIdx(os);
      if (key==='Enter') {
        e.preventDefault(); e.stopPropagation();
        P.setTimeout(function(){kbCommit(os[cur]);},0);
        return;
      }
      var next=key==='ArrowDown'?Math.min(os.length-1,cur+1):
               key==='ArrowUp'  ?Math.max(0,cur-1):
               key==='Home'     ?0:os.length-1;
      kbHighlight(os[next]);
      e.preventDefault(); e.stopPropagation();
      return;
    }

    /* Page scroll when not typing */
    if (isTyping()) return;
    var h=P.innerHeight||700;
    if (key===' '||key==='Spacebar'){scrollPage(e.shiftKey?-h*.80:h*.80);e.preventDefault();return;}
    var map={'ArrowDown':h*.12,'ArrowUp':-h*.12,'PageDown':h*.80,'PageUp':-h*.80};
    if (Object.prototype.hasOwnProperty.call(map,key)){scrollPage(map[key]);e.preventDefault();}
  }

  [P,window].forEach(function(w){ w.addEventListener('keydown',onKey,true); });
  [D,document].forEach(function(d){
    d.addEventListener('keydown',onKey,true);
    d.addEventListener('pointerdown',onOptionDown,true);
    d.addEventListener('mousedown',onOptionDown,true);
    d.addEventListener('pointerup',onOptionUp,true);
    d.addEventListener('mouseup',onOptionUp,true);
    if(d.body) d.body.tabIndex=-1;
  });

  /* ── 2. Minimal CSS — only what's needed for scrollable listbox ──────
     We deliberately avoid touching:
       - hover/focus styles (let BaseWeb handle natively)
       - pointer-events on options (BaseWeb sets these correctly)
       - any style that would fight BaseWeb's own hover handlers
  */
  function injectCSS(doc) {
    if (!doc||!doc.head||doc.__kbCSS) return;
    doc.__kbCSS = true;
    var s=doc.createElement('style');
    s.id='kbfix-style';
    s.textContent=
      /* Make listbox scrollable — BaseWeb sets overflow:hidden which breaks scroll */
      '[role="listbox"]{overflow-y:auto!important;max-height:340px!important;' +
        'overscroll-behavior:contain!important}' +
      '[role="option"].kbfix-active{background:rgba(37,99,235,.12)!important}' +
      /* Keep popover above everything else */
      '[data-baseweb="popover"]{z-index:2147483000!important}';
    doc.head.appendChild(s);
  }

  /* Fix listbox overflow — run once when dropdown opens, not on interval */
  function fixListboxes(doc) {
    if (!doc) return;
    injectCSS(doc);
    try {
      doc.querySelectorAll('[role="listbox"]').forEach(function(lb) {
        /* Only set overflow — nothing else. Let BaseWeb own everything else. */
        lb.style.overflowY='auto';
        lb.style.maxHeight='340px';
        lb.style.overscrollBehavior='contain';
      });
    } catch(e){}
  }

  /* Watch for dropdown opening (new listbox in DOM) */
  new MutationObserver(function(mutations) {
    mutations.forEach(function(m) {
      m.addedNodes&&Array.prototype.forEach.call(m.addedNodes,function(node) {
        if (!node.querySelectorAll) return;
        /* Only fix if a listbox was actually added */
        if (node.getAttribute&&node.getAttribute('role')==='listbox') {
          fixListboxes(node.ownerDocument);
        }
        var lbs=node.querySelectorAll('[role="listbox"]');
        if (lbs.length) fixListboxes(node.ownerDocument||D);
      });
    });
  }).observe(D.body||D.documentElement,{childList:true,subtree:true});

  /* Initial fix */
  fixListboxes(D);
  if (document!==D) fixListboxes(document);

})();
</script>
"""

_JS = _JS.replace('""" + _VERSION + """', _VERSION)


def install_keyboard_fix():
    """Call once in app.py after set_page_config()."""
    stc.html(_JS, height=0, scrolling=False)
