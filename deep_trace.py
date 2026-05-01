#!/usr/bin/env python3
"""Run from WIN39 folder. Shows exact current state of all loop-related code."""
import os

def dump(filepath, search, label, before=0, after=500):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    idx = content.find(search)
    if idx == -1:
        print(f"NOT FOUND: {label}"); return
    print(f"\n###START:{label}###")
    print(repr(content[max(0,idx-before):idx+after]))
    print(f"###END:{label}###")

# 1. Current state of render_eye_power_section
dump("modules/wholesale_punching.py",
     "def render_eye_power_section",
     "POWER_SECTION", before=0, after=1200)

# 2. What writes to session_state inside render_product_selection
dump("modules/wholesale_punching.py",
     "def render_product_selection",
     "PRODUCT_SELECTION_START", before=0, after=300)

# 3. The _product_cache_refreshed block
dump("modules/wholesale_punching.py",
     "_product_cache_refreshed",
     "CACHE_REFRESH", before=0, after=400)

# 4. render_wholesale_controls - anything writing to session state
dump("modules/wholesale_punching.py",
     "def render_wholesale_controls",
     "WS_CONTROLS", before=0, after=600)

# 5. Any unconditional session_state writes in render_power_entry
dump("modules/wholesale_punching.py",
     "def render_power_entry",
     "POWER_ENTRY", before=0, after=400)
