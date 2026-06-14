"""
Internal LAN print configuration.

Browser/HTML print remains as standby. These constants define the shop-machine
defaults used by direct/local print helpers and print CSS defaults.
"""

import os


CANON_DOCUMENT_PRINTER = os.getenv("DV_CANON_PRINTER", "Canon LBP6000/LBP6018")
TSC_LABEL_PRINTER = os.getenv("DV_TSC_PRINTER", "TSC TTP-244 Pro")
EVOLIS_CARD_PRINTER = os.getenv("DV_EVOLIS_PRINTER", "Evolis Primacy")

# Physical formats used in the shop.
FRAME_STICKER_W_MM = 80
FRAME_STICKER_H_MM = 12

TSC_LABEL_W_MM = 75
TSC_LABEL_H_MM = 50

CR80_W_MM = 85.6
CR80_H_MM = 54

CANON_DEFAULT_PAPER = "A5"
CANON_EXPANDED_PAPER = "A4"


def css_size(width_mm: float, height_mm: float) -> str:
    return f"{width_mm:g}mm {height_mm:g}mm"


def tsc_label_size_cmd() -> str:
    return f"SIZE {TSC_LABEL_W_MM} mm, {TSC_LABEL_H_MM} mm"


def frame_sticker_size_cmd() -> str:
    return f"SIZE {FRAME_STICKER_W_MM} mm,{FRAME_STICKER_H_MM} mm"
