"""
modules/documents/contact_lens_converter.py
============================================
Contact Lens Power Converter — Bausch & Lomb and CooperVision

Converts spectacle Rx (SPH / CYL / AXIS) to the closest available
contact lens power for each brand and product line.

Covers:
  Bausch & Lomb  : Ultra, Ultra Toric, PureVision 2, PureVision 2 Toric,
                   SofLens 59, SofLens Toric, SofLens Daily,
                   Biotrue ONEday, Biotrue ONEday Toric,
                   Infuse, Infuse Toric
  CooperVision   : Biofinity, Biofinity Toric, Biofinity XR,
                   MyDay, MyDay Toric,
                   Avaira Vitality, Avaira Vitality Toric,
                   clariti 1 day, clariti 1 day Toric,
                   Proclear, Proclear Toric,
                   MiSight 1 day

Usage:
    from modules.documents.contact_lens_converter import (
        convert_rx_to_cl, BRAND_CATALOG, render_cl_converter_ui
    )

    result = convert_rx_to_cl(
        sph=-3.25, cyl=-1.50, axis=90,
        brand="Bausch & Lomb",
        product="Ultra Toric"
    )
"""

from __future__ import annotations
import math
from typing import Optional
from dataclasses import dataclass, field

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _nearest(value: float, steps: list[float]) -> float:
    """Return the value in `steps` closest to `value`."""
    return min(steps, key=lambda s: (abs(s - value), abs(s)))


def _round_to_quarter(v: float) -> float:
    """Round to nearest 0.25 step (standard CL SPH step)."""
    return round(round(v * 4) / 4, 2)


def _round_to_half(v: float) -> float:
    """Round to nearest 0.50 step."""
    return round(round(v * 2) / 2, 2)


def _vertex_sph(sph: float, vertex_mm: float = 12.5) -> float:
    """
    Apply vertex distance correction — spherical.
    Standard formula: F_CL = F_spec / (1 - d × F_spec)
    where d = vertex distance in metres (positive, typically 0.012–0.014m).
    Default VD = 12.5mm (clinical standard).
    """
    F = float(sph)
    d = float(vertex_mm) / 1000.0
    denom = 1.0 - (F * d)        # CORRECT sign: 1 - F*d
    if abs(denom) < 1e-12:
        return F
    return F / denom


def _vertex_toric(sph: float, cyl: float, axis: float,
                  vertex_mm: float = 12.5) -> tuple[float, float, int]:
    """
    Apply vertex distance correction — toric.
    Applies correct formula F_CL = F / (1 - d×F) independently to both
    principal meridians, then reconstructs CL SPH and CYL.
    Default VD = 12.5mm.
    """
    # Ensure minus-cyl form
    if cyl > 0:
        sph, cyl, axis = sph + cyl, -cyl, (int(axis) - 90) % 180

    mer1 = sph           # flatter meridian
    mer2 = sph + cyl     # steeper meridian
    d    = float(vertex_mm) / 1000.0

    def _eff(F):
        denom = 1.0 - (F * d)   # CORRECT sign
        return F if abs(denom) < 1e-12 else F / denom

    cl_sph  = _eff(mer1)
    cl_cyl  = _eff(mer2) - _eff(mer1)
    cl_axis = int(axis) % 180
    return cl_sph, cl_cyl, cl_axis


def _clamp(value: float, lo: float, hi: float) -> Optional[float]:
    """Return value if in [lo, hi], else None (out of range)."""
    if lo <= value <= hi:
        return value
    return None


# ─────────────────────────────────────────────────────────────────────────────
# PRODUCT CATALOG
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CLProduct:
    brand:       str
    name:        str
    lens_type:   str          # "spherical" | "toric" | "multifocal"
    replace:     str          # "daily" | "monthly" | "fortnightly" | "quarterly"
    material:    str
    water:       float        # water content %
    bc:          list[float]  # available base curves
    dia:         list[float]  # available diameters
    sph_range:   tuple[float, float]
    sph_steps:   list[float]
    cyl_steps:   list[float]  = field(default_factory=list)
    axis_steps:  list[int]    = field(default_factory=list)
    vertex_mm:   float        = 12.5   # standard clinical VD
    notes:       str          = ""


# ── Generate standard SPH step lists ─────────────────────────────────────────

def _sph_steps(lo: float, hi: float, step: float = 0.25) -> list[float]:
    steps = []
    v = lo
    while v <= hi + 1e-9:
        steps.append(round(v, 2))
        v += step
    return steps


def _axis_steps_10() -> list[int]:
    return list(range(10, 181, 10))


def _axis_steps_fine() -> list[int]:
    return list(range(1, 181))


# ── Standard CYL ranges ───────────────────────────────────────────────────────

_CYL_BL_STANDARD   = [-0.75, -1.25, -1.75, -2.25]
_CYL_CV_STANDARD   = [-0.75, -1.25, -1.75, -2.25, -2.75]
_CYL_CV_EXTENDED   = [-0.75, -1.25, -1.75, -2.25, -2.75, -3.25, -3.75]
_CYL_BL_SOFLENS    = [-0.75, -1.25, -1.75, -2.25, -2.75]
_CYL_BL_INFUSE     = [-0.75, -1.25, -1.75, -2.25, -2.75]

# ─────────────────────────────────────────────────────────────────────────────
# BAUSCH & LOMB CATALOG
# ─────────────────────────────────────────────────────────────────────────────

BL_ULTRA = CLProduct(
    brand="Bausch & Lomb", name="Ultra",
    lens_type="spherical", replace="monthly",
    material="samfilcon A", water=46.0,
    bc=[8.5], dia=[14.2],
    sph_range=(-12.0, 6.0),
    sph_steps=_sph_steps(-12.0, -6.25, 0.25) + _sph_steps(-6.0, 6.0, 0.25),
    notes="MoistureSeal technology. Extended range above ±6.00 in 0.50 steps."
)

BL_ULTRA_TORIC = CLProduct(
    brand="Bausch & Lomb", name="Ultra for Astigmatism (Toric)",
    lens_type="toric", replace="monthly",
    material="samfilcon A", water=46.0,
    bc=[8.6], dia=[14.5],
    sph_range=(-9.0, 4.0),
    sph_steps=_sph_steps(-9.0, 4.0, 0.25),
    cyl_steps=_CYL_BL_STANDARD,
    axis_steps=_axis_steps_10(),
    notes="CYL available: -0.75, -1.25, -1.75, -2.25. Axis: 10° to 180° in 10° steps."
)

BL_PUREVISION2 = CLProduct(
    brand="Bausch & Lomb", name="PureVision 2",
    lens_type="spherical", replace="monthly",
    material="balafilcon A", water=36.0,
    bc=[8.6], dia=[14.0],
    sph_range=(-12.0, 8.0),
    sph_steps=(
        _sph_steps(-12.0, -6.5, 0.50) +
        _sph_steps(-6.0,   6.0, 0.25) +
        _sph_steps(6.5,    8.0, 0.50)
    ),
    notes="High Dk/t silicone hydrogel. Extended range in 0.50 steps beyond ±6.00."
)

BL_PUREVISION2_TORIC = CLProduct(
    brand="Bausch & Lomb", name="PureVision 2 for Astigmatism (Toric)",
    lens_type="toric", replace="monthly",
    material="balafilcon A", water=36.0,
    bc=[8.9], dia=[14.5],
    sph_range=(-9.0, 4.0),
    sph_steps=_sph_steps(-9.0, 4.0, 0.25),
    cyl_steps=[-0.75, -1.25, -1.75, -2.25, -2.75],
    axis_steps=_axis_steps_10(),
    notes="Auto-align design. Axis 10°–180° in 10° steps."
)

BL_SOFLENS59 = CLProduct(
    brand="Bausch & Lomb", name="SofLens 59",
    lens_type="spherical", replace="monthly",
    material="hilafilcon B", water=59.0,
    bc=[8.6], dia=[14.2],
    sph_range=(-9.0, 4.0),
    sph_steps=_sph_steps(-9.0, 4.0, 0.25),
    notes="High water content. Daily wear."
)

BL_SOFLENS_TORIC = CLProduct(
    brand="Bausch & Lomb", name="SofLens Toric",
    lens_type="toric", replace="monthly",
    material="hilafilcon B", water=59.0,
    bc=[8.5], dia=[14.5],
    sph_range=(-9.0, 4.0),
    sph_steps=_sph_steps(-9.0, 4.0, 0.25),
    cyl_steps=_CYL_BL_SOFLENS,
    axis_steps=_axis_steps_10(),
    notes="Also available: CYL -2.75. Axis 10–180° in 10° steps."
)

BL_SOFLENS_DAILY = CLProduct(
    brand="Bausch & Lomb", name="SofLens Daily Disposable",
    lens_type="spherical", replace="daily",
    material="hilafilcon B", water=59.0,
    bc=[8.6], dia=[14.2],
    sph_range=(-9.0, 4.0),
    sph_steps=_sph_steps(-9.0, 4.0, 0.25),
    notes="1-day replacement."
)

BL_BIOTRUE = CLProduct(
    brand="Bausch & Lomb", name="Biotrue ONEday",
    lens_type="spherical", replace="daily",
    material="nesofilcon A", water=78.0,
    bc=[8.6], dia=[14.2],
    sph_range=(-12.0, 6.0),
    sph_steps=(
        _sph_steps(-12.0, -6.5, 0.50) +
        _sph_steps(-6.0,   6.0, 0.25)
    ),
    notes="78% water — mimics the eye's lipid layer. Daily replacement."
)

BL_BIOTRUE_TORIC = CLProduct(
    brand="Bausch & Lomb", name="Biotrue ONEday for Astigmatism (Toric)",
    lens_type="toric", replace="daily",
    material="nesofilcon A", water=78.0,
    bc=[8.4], dia=[14.5],
    sph_range=(-9.0, 4.0),
    sph_steps=_sph_steps(-9.0, 4.0, 0.25),
    cyl_steps=[-0.75, -1.25, -1.75, -2.25],
    axis_steps=_axis_steps_10(),
    notes="Daily toric. Axis 10–180° in 10° steps."
)

BL_INFUSE = CLProduct(
    brand="Bausch & Lomb", name="Infuse",
    lens_type="spherical", replace="daily",
    material="kalifilcon A", water=55.0,
    bc=[8.6], dia=[14.2],
    sph_range=(-12.0, 8.0),
    sph_steps=(
        _sph_steps(-12.0, -6.5, 0.50) +
        _sph_steps(-6.0,   6.0, 0.25) +
        _sph_steps(6.5,    8.0, 0.50)
    ),
    notes="ProBalance Technology. Ultra-premium daily silicone hydrogel."
)

BL_INFUSE_TORIC = CLProduct(
    brand="Bausch & Lomb", name="Infuse for Astigmatism (Toric)",
    lens_type="toric", replace="daily",
    material="kalifilcon A", water=55.0,
    bc=[8.6], dia=[14.5],
    sph_range=(-10.0, 4.0),
    sph_steps=_sph_steps(-10.0, 4.0, 0.25),
    cyl_steps=_CYL_BL_INFUSE,
    axis_steps=_axis_steps_10(),
    notes="Premium daily toric silicone hydrogel."
)

# ─────────────────────────────────────────────────────────────────────────────
# COOPERVISION CATALOG
# ─────────────────────────────────────────────────────────────────────────────

CV_BIOFINITY = CLProduct(
    brand="CooperVision", name="Biofinity",
    lens_type="spherical", replace="monthly",
    material="comfilcon A", water=48.0,
    bc=[8.6], dia=[14.0],
    sph_range=(-12.0, 8.0),
    sph_steps=(
        _sph_steps(-12.0, -6.5, 0.50) +
        _sph_steps(-6.0,   6.0, 0.25) +
        _sph_steps(6.5,    8.0, 0.50)
    ),
    notes="Aquaform technology. Extended range beyond ±6.00 in 0.50 steps."
)

CV_BIOFINITY_TORIC = CLProduct(
    brand="CooperVision", name="Biofinity Toric",
    lens_type="toric", replace="monthly",
    material="comfilcon A", water=48.0,
    bc=[8.7], dia=[14.5],
    sph_range=(-10.0, 7.5),
    sph_steps=_sph_steps(-10.0, 7.5, 0.25),
    cyl_steps=_CYL_CV_STANDARD,
    axis_steps=_axis_steps_10(),
    notes="Optimized Toric Lens Geometry. CYL up to -2.75. Axis 10–180° in 10° steps."
)

CV_BIOFINITY_XR = CLProduct(
    brand="CooperVision", name="Biofinity XR (Extended Range)",
    lens_type="spherical", replace="monthly",
    material="comfilcon A", water=48.0,
    bc=[8.6], dia=[14.0],
    sph_range=(-20.0, 20.0),
    sph_steps=(
        _sph_steps(-20.0, -12.5, 0.50) +
        _sph_steps(-12.0,  12.0, 0.25) +
        _sph_steps(12.5,   20.0, 0.50)
    ),
    notes="For high prescriptions beyond standard range."
)

CV_BIOFINITY_XR_TORIC = CLProduct(
    brand="CooperVision", name="Biofinity XR Toric",
    lens_type="toric", replace="monthly",
    material="comfilcon A", water=48.0,
    bc=[8.7], dia=[14.5],
    sph_range=(-20.0, 20.0),
    sph_steps=_sph_steps(-20.0, 20.0, 0.25),
    cyl_steps=_CYL_CV_EXTENDED,
    axis_steps=_axis_steps_10(),
    notes="High CYL up to -3.75. For complex prescriptions."
)

CV_MYDAY = CLProduct(
    brand="CooperVision", name="MyDay",
    lens_type="spherical", replace="daily",
    material="stenfilcon A", water=54.0,
    bc=[8.4], dia=[14.2],
    sph_range=(-12.0, 8.0),
    sph_steps=(
        _sph_steps(-12.0, -6.5, 0.50) +
        _sph_steps(-6.0,   6.0, 0.25) +
        _sph_steps(6.5,    8.0, 0.50)
    ),
    notes="Smart Silicone chemistry. Ultra-thin edge daily."
)

CV_MYDAY_TORIC = CLProduct(
    brand="CooperVision", name="MyDay Toric",
    lens_type="toric", replace="daily",
    material="stenfilcon A", water=54.0,
    bc=[8.4], dia=[14.5],
    sph_range=(-10.0, 4.0),
    sph_steps=_sph_steps(-10.0, 4.0, 0.25),
    cyl_steps=[-0.75, -1.25, -1.75, -2.25],
    axis_steps=_axis_steps_10(),
    notes="Daily toric. Precision Balance 8|4 design."
)

CV_AVAIRA_VITALITY = CLProduct(
    brand="CooperVision", name="Avaira Vitality",
    lens_type="spherical", replace="fortnightly",
    material="fanfilcon A", water=52.0,
    bc=[8.4], dia=[14.2],
    sph_range=(-12.0, 8.0),
    sph_steps=(
        _sph_steps(-12.0, -6.5, 0.50) +
        _sph_steps(-6.0,   6.0, 0.25) +
        _sph_steps(6.5,    8.0, 0.50)
    ),
    notes="Aquaform technology. 2-week replacement."
)

CV_AVAIRA_VITALITY_TORIC = CLProduct(
    brand="CooperVision", name="Avaira Vitality Toric",
    lens_type="toric", replace="fortnightly",
    material="fanfilcon A", water=52.0,
    bc=[8.5], dia=[14.5],
    sph_range=(-10.0, 4.0),
    sph_steps=_sph_steps(-10.0, 4.0, 0.25),
    cyl_steps=[-0.75, -1.25, -1.75, -2.25, -2.75],
    axis_steps=_axis_steps_10(),
    notes="2-week toric silicone hydrogel."
)

CV_CLARITI_1DAY = CLProduct(
    brand="CooperVision", name="clariti 1 day",
    lens_type="spherical", replace="daily",
    material="somofilcon A", water=56.0,
    bc=[8.6], dia=[14.1],
    sph_range=(-10.0, 6.0),
    sph_steps=_sph_steps(-10.0, 6.0, 0.25),
    notes="100% silicone hydrogel daily. UV blocking."
)

CV_CLARITI_1DAY_TORIC = CLProduct(
    brand="CooperVision", name="clariti 1 day Toric",
    lens_type="toric", replace="daily",
    material="somofilcon A", water=56.0,
    bc=[8.6], dia=[14.3],
    sph_range=(-10.0, 4.0),
    sph_steps=_sph_steps(-10.0, 4.0, 0.25),
    cyl_steps=[-0.75, -1.25, -1.75, -2.25],
    axis_steps=_axis_steps_10(),
    notes="Daily toric with 100% silicone hydrogel."
)

CV_PROCLEAR = CLProduct(
    brand="CooperVision", name="Proclear",
    lens_type="spherical", replace="monthly",
    material="omafilcon A", water=62.0,
    bc=[8.6], dia=[14.2],
    sph_range=(-12.0, 8.0),
    sph_steps=(
        _sph_steps(-12.0, -6.5, 0.50) +
        _sph_steps(-6.0,   6.0, 0.25) +
        _sph_steps(6.5,    8.0, 0.50)
    ),
    notes="PC Technology for dry eye prone patients. FDA-cleared."
)

CV_PROCLEAR_TORIC = CLProduct(
    brand="CooperVision", name="Proclear Toric",
    lens_type="toric", replace="monthly",
    material="omafilcon A", water=62.0,
    bc=[8.8], dia=[14.4],
    sph_range=(-10.0, 4.0),
    sph_steps=_sph_steps(-10.0, 4.0, 0.25),
    cyl_steps=[-0.75, -1.25, -1.75, -2.25, -2.75],
    axis_steps=_axis_steps_10(),
    notes="PC Technology toric for dry eye prone patients."
)

CV_MISIGHT = CLProduct(
    brand="CooperVision", name="MiSight 1 day",
    lens_type="spherical", replace="daily",
    material="omafilcon A", water=60.0,
    bc=[8.7], dia=[14.1],
    sph_range=(-10.0, -0.25),
    sph_steps=_sph_steps(-10.0, -0.25, 0.25),
    notes="Myopia control lens for children (7–12 yrs). Approved for myopia management."
)

# ─────────────────────────────────────────────────────────────────────────────
# ALCON CATALOG
# ─────────────────────────────────────────────────────────────────────────────

AL_AIROPTIX_AQUA = CLProduct(
    brand="Alcon", name="Air Optix Aqua",
    lens_type="spherical", replace="monthly",
    material="lotrafilcon B", water=33.0,
    bc=[8.6], dia=[14.2],
    sph_range=(-12.0, 8.0),
    sph_steps=(
        _sph_steps(-12.0, -6.5, 0.50) +
        _sph_steps(-6.0,   6.0, 0.25) +
        _sph_steps(6.5,    8.0, 0.50)
    ),
    notes="TriComfort Technology. Extended range in 0.50 steps beyond ±6.00."
)

AL_AIROPTIX_AQUA_TORIC = CLProduct(
    brand="Alcon", name="Air Optix Aqua for Astigmatism (Toric)",
    lens_type="toric", replace="monthly",
    material="lotrafilcon B", water=33.0,
    bc=[8.7], dia=[14.5],
    sph_range=(-10.0, 4.0),
    sph_steps=_sph_steps(-10.0, 4.0, 0.25),
    cyl_steps=[-0.75, -1.25, -1.75, -2.25, -2.75],
    axis_steps=_axis_steps_10(),
    notes="Precision Balance 8|4 design. Axis 10–180° in 10° steps."
)

AL_AIROPTIX_PLUS_HG = CLProduct(
    brand="Alcon", name="Air Optix Plus HydraGlyde",
    lens_type="spherical", replace="monthly",
    material="lotrafilcon B", water=33.0,
    bc=[8.6], dia=[14.2],
    sph_range=(-12.0, 8.0),
    sph_steps=(
        _sph_steps(-12.0, -6.5, 0.50) +
        _sph_steps(-6.0,   6.0, 0.25) +
        _sph_steps(6.5,    8.0, 0.50)
    ),
    notes="HydraGlyde moisture matrix. Extended range in 0.50 steps beyond ±6.00."
)

AL_AIROPTIX_PLUS_HG_TORIC = CLProduct(
    brand="Alcon", name="Air Optix Plus HydraGlyde for Astigmatism (Toric)",
    lens_type="toric", replace="monthly",
    material="lotrafilcon B", water=33.0,
    bc=[8.7], dia=[14.5],
    sph_range=(-10.0, 4.0),
    sph_steps=_sph_steps(-10.0, 4.0, 0.25),
    cyl_steps=[-0.75, -1.25, -1.75, -2.25, -2.75],
    axis_steps=_axis_steps_10(),
    notes="HydraGlyde + Precision Balance 8|4. Axis 10–180° in 10° steps."
)

AL_AIROPTIX_NIGHT_DAY = CLProduct(
    brand="Alcon", name="Air Optix Night & Day Aqua",
    lens_type="spherical", replace="monthly",
    material="lotrafilcon A", water=24.0,
    bc=[8.6], dia=[13.8],
    sph_range=(-10.0, 6.0),
    sph_steps=(
        _sph_steps(-10.0, -6.5, 0.50) +
        _sph_steps(-6.0,   6.0, 0.25)
    ),
    notes="Approved for up to 30 nights continuous wear. Highest Dk/t."
)

AL_AIROPTIX_COLORS = CLProduct(
    brand="Alcon", name="Air Optix Colors",
    lens_type="spherical", replace="monthly",
    material="lotrafilcon B", water=33.0,
    bc=[8.6], dia=[14.2],
    sph_range=(-12.0, 6.0),
    sph_steps=(
        _sph_steps(-12.0, -6.5, 0.50) +
        _sph_steps(-6.0,   6.0, 0.25)
    ),
    notes="15 available colors. Also available in Plano (0.00)."
)

AL_TOTAL30 = CLProduct(
    brand="Alcon", name="Total30",
    lens_type="spherical", replace="monthly",
    material="lehfilcon A", water=55.0,
    bc=[8.4], dia=[14.2],
    sph_range=(-12.0, 8.0),
    sph_steps=(
        _sph_steps(-12.0, -6.5, 0.50) +
        _sph_steps(-6.0,   6.0, 0.25) +
        _sph_steps(6.5,    8.0, 0.50)
    ),
    notes="Water Gradient technology — outermost layer >99% water. Premium monthly."
)

AL_TOTAL30_TORIC = CLProduct(
    brand="Alcon", name="Total30 for Astigmatism (Toric)",
    lens_type="toric", replace="monthly",
    material="lehfilcon A", water=55.0,
    bc=[8.6], dia=[14.5],
    sph_range=(-10.0, 4.0),
    sph_steps=_sph_steps(-10.0, 4.0, 0.25),
    cyl_steps=[-0.75, -1.25, -1.75, -2.25],
    axis_steps=_axis_steps_10(),
    notes="Water Gradient toric. Axis 10–180° in 10° steps."
)

AL_DAILIES_TOTAL1 = CLProduct(
    brand="Alcon", name="Dailies Total1",
    lens_type="spherical", replace="daily",
    material="delefilcon A", water=33.0,
    bc=[8.5], dia=[14.1],
    sph_range=(-12.0, 6.0),
    sph_steps=(
        _sph_steps(-12.0, -6.5, 0.50) +
        _sph_steps(-6.0,   6.0, 0.25)
    ),
    notes="Water Gradient daily. Outermost layer >99% water. Premium daily."
)

AL_DAILIES_TOTAL1_TORIC = CLProduct(
    brand="Alcon", name="Dailies Total1 for Astigmatism (Toric)",
    lens_type="toric", replace="daily",
    material="delefilcon A", water=33.0,
    bc=[8.6], dia=[14.5],
    sph_range=(-10.0, 4.0),
    sph_steps=_sph_steps(-10.0, 4.0, 0.25),
    cyl_steps=[-0.75, -1.25, -1.75, -2.25],
    axis_steps=_axis_steps_10(),
    notes="Water Gradient daily toric. Axis 10–180° in 10° steps."
)

AL_DAILIES_AQUACOMFORT = CLProduct(
    brand="Alcon", name="Dailies AquaComfort Plus",
    lens_type="spherical", replace="daily",
    material="nelfilcon A", water=69.0,
    bc=[8.7], dia=[14.0],
    sph_range=(-10.0, 6.0),
    sph_steps=_sph_steps(-10.0, 6.0, 0.25),
    notes="Blink-activated moisture. High water 69%. Economy daily option."
)

AL_FRESHLOOK_COLORS = CLProduct(
    brand="Alcon", name="FreshLook Colors",
    lens_type="spherical", replace="monthly",
    material="phemfilcon A", water=55.0,
    bc=[8.6], dia=[14.5],
    sph_range=(-8.0, 4.0),
    sph_steps=_sph_steps(-8.0, 4.0, 0.25),
    notes="4 colors. Also available in Plano. 3-month replacement."
)

AL_FRESHLOOK_COLORBLENDS = CLProduct(
    brand="Alcon", name="FreshLook ColorBlends",
    lens_type="spherical", replace="monthly",
    material="phemfilcon A", water=55.0,
    bc=[8.6], dia=[14.5],
    sph_range=(-8.0, 4.0),
    sph_steps=_sph_steps(-8.0, 4.0, 0.25),
    notes="12 blended colors. Also available in Plano."
)


# ─────────────────────────────────────────────────────────────────────────────
# JOHNSON & JOHNSON CATALOG
# ─────────────────────────────────────────────────────────────────────────────

JJ_ACUVUE_OASYS = CLProduct(
    brand="Johnson & Johnson", name="Acuvue Oasys",
    lens_type="spherical", replace="fortnightly",
    material="senofilcon A", water=38.0,
    bc=[8.4, 8.8], dia=[14.0],
    sph_range=(-12.0, 8.0),
    sph_steps=(
        _sph_steps(-12.0, -6.5, 0.50) +
        _sph_steps(-6.0,   6.0, 0.25) +
        _sph_steps(6.5,    8.0, 0.50)
    ),
    notes="Hydraclear Plus technology. 2-week silicone hydrogel."
)

JJ_ACUVUE_OASYS_TORIC = CLProduct(
    brand="Johnson & Johnson", name="Acuvue Oasys for Astigmatism (Toric)",
    lens_type="toric", replace="fortnightly",
    material="senofilcon A", water=38.0,
    bc=[8.6], dia=[14.5],
    sph_range=(-9.0, 4.0),
    sph_steps=_sph_steps(-9.0, 4.0, 0.25),
    cyl_steps=[-0.75, -1.25, -1.75, -2.25, -2.75],
    axis_steps=_axis_steps_10(),
    notes="eStabilization design for stable vision. Axis 10–180° in 10° steps."
)

JJ_ACUVUE_OASYS_1DAY = CLProduct(
    brand="Johnson & Johnson", name="Acuvue Oasys 1-Day",
    lens_type="spherical", replace="daily",
    material="senofilcon A", water=38.0,
    bc=[8.5], dia=[14.3],
    sph_range=(-12.0, 8.0),
    sph_steps=(
        _sph_steps(-12.0, -6.5, 0.50) +
        _sph_steps(-6.0,   6.0, 0.25) +
        _sph_steps(6.5,    8.0, 0.50)
    ),
    notes="Hydraluxe technology. UV blocking Class 1."
)

JJ_ACUVUE_OASYS_1DAY_TORIC = CLProduct(
    brand="Johnson & Johnson", name="Acuvue Oasys 1-Day for Astigmatism (Toric)",
    lens_type="toric", replace="daily",
    material="senofilcon A", water=38.0,
    bc=[8.5], dia=[14.3],
    sph_range=(-9.0, 4.0),
    sph_steps=_sph_steps(-9.0, 4.0, 0.25),
    cyl_steps=[-0.75, -1.25, -1.75, -2.25, -2.75],
    axis_steps=_axis_steps_10(),
    notes="Hydraluxe + eStabilization. UV blocking. Daily toric."
)

JJ_ACUVUE_VITA = CLProduct(
    brand="Johnson & Johnson", name="Acuvue Vita",
    lens_type="spherical", replace="monthly",
    material="senofilcon C", water=41.0,
    bc=[8.1, 8.5], dia=[14.2],
    sph_range=(-12.0, 8.0),
    sph_steps=(
        _sph_steps(-12.0, -6.5, 0.50) +
        _sph_steps(-6.0,   6.0, 0.25) +
        _sph_steps(6.5,    8.0, 0.50)
    ),
    notes="HydraMax technology. Consistent moisture through 30 days."
)

JJ_ACUVUE_VITA_TORIC = CLProduct(
    brand="Johnson & Johnson", name="Acuvue Vita for Astigmatism (Toric)",
    lens_type="toric", replace="monthly",
    material="senofilcon C", water=41.0,
    bc=[8.6], dia=[14.5],
    sph_range=(-9.0, 4.0),
    sph_steps=_sph_steps(-9.0, 4.0, 0.25),
    cyl_steps=[-0.75, -1.25, -1.75, -2.25, -2.75],
    axis_steps=_axis_steps_10(),
    notes="HydraMax + eStabilization. Monthly toric. Axis 10–180° in 10° steps."
)

JJ_1DAY_ACUVUE_MOIST = CLProduct(
    brand="Johnson & Johnson", name="1-Day Acuvue Moist",
    lens_type="spherical", replace="daily",
    material="etafilcon A", water=58.0,
    bc=[8.5, 9.0], dia=[14.2],
    sph_range=(-12.0, 6.0),
    sph_steps=(
        _sph_steps(-12.0, -6.5, 0.50) +
        _sph_steps(-6.0,   6.0, 0.25)
    ),
    notes="Lacreon technology — bound moisture. High water 58%."
)

JJ_1DAY_ACUVUE_MOIST_TORIC = CLProduct(
    brand="Johnson & Johnson", name="1-Day Acuvue Moist for Astigmatism (Toric)",
    lens_type="toric", replace="daily",
    material="etafilcon A", water=58.0,
    bc=[8.5], dia=[14.5],
    sph_range=(-9.0, 4.0),
    sph_steps=_sph_steps(-9.0, 4.0, 0.25),
    cyl_steps=[-0.75, -1.25, -1.75, -2.25, -2.75],
    axis_steps=_axis_steps_10(),
    notes="Lacreon + eStabilization. Daily toric. Axis 10–180° in 10° steps."
)

JJ_1DAY_ACUVUE_TRU_EYE = CLProduct(
    brand="Johnson & Johnson", name="1-Day Acuvue TruEye",
    lens_type="spherical", replace="daily",
    material="narafilcon A", water=46.0,
    bc=[8.5], dia=[14.2],
    sph_range=(-12.0, 6.0),
    sph_steps=(
        _sph_steps(-12.0, -6.5, 0.50) +
        _sph_steps(-6.0,   6.0, 0.25)
    ),
    notes="First daily silicone hydrogel. Hydraclear 1 technology."
)

JJ_ACUVUE2 = CLProduct(
    brand="Johnson & Johnson", name="Acuvue 2",
    lens_type="spherical", replace="fortnightly",
    material="etafilcon A", water=58.0,
    bc=[8.3, 8.7], dia=[14.0],
    sph_range=(-12.0, 9.0),
    sph_steps=(
        _sph_steps(-12.0, -6.5, 0.50) +
        _sph_steps(-6.0,   6.0, 0.25) +
        _sph_steps(6.5,    9.0, 0.50)
    ),
    notes="Classic 2-week hydrogel. Good range up to +9.00."
)


# ─────────────────────────────────────────────────────────────────────────────
# MASTER CATALOG
# ─────────────────────────────────────────────────────────────────────────────

ALL_PRODUCTS: list[CLProduct] = [
    # ── Bausch & Lomb ─────────────────────────────────────────────────────────
    BL_ULTRA, BL_ULTRA_TORIC,
    BL_PUREVISION2, BL_PUREVISION2_TORIC,
    BL_SOFLENS59, BL_SOFLENS_TORIC, BL_SOFLENS_DAILY,
    BL_BIOTRUE, BL_BIOTRUE_TORIC,
    BL_INFUSE, BL_INFUSE_TORIC,

    # ── CooperVision ──────────────────────────────────────────────────────────
    CV_BIOFINITY, CV_BIOFINITY_TORIC,
    CV_BIOFINITY_XR, CV_BIOFINITY_XR_TORIC,
    CV_MYDAY, CV_MYDAY_TORIC,
    CV_AVAIRA_VITALITY, CV_AVAIRA_VITALITY_TORIC,
    CV_CLARITI_1DAY, CV_CLARITI_1DAY_TORIC,
    CV_PROCLEAR, CV_PROCLEAR_TORIC,
    CV_MISIGHT,

    # ── Alcon ─────────────────────────────────────────────────────────────────
    AL_AIROPTIX_AQUA, AL_AIROPTIX_AQUA_TORIC,
    AL_AIROPTIX_PLUS_HG, AL_AIROPTIX_PLUS_HG_TORIC,
    AL_AIROPTIX_NIGHT_DAY,
    AL_AIROPTIX_COLORS,
    AL_TOTAL30, AL_TOTAL30_TORIC,
    AL_DAILIES_TOTAL1, AL_DAILIES_TOTAL1_TORIC,
    AL_DAILIES_AQUACOMFORT,
    AL_FRESHLOOK_COLORS, AL_FRESHLOOK_COLORBLENDS,

    # ── Johnson & Johnson ─────────────────────────────────────────────────────
    JJ_ACUVUE_OASYS, JJ_ACUVUE_OASYS_TORIC,
    JJ_ACUVUE_OASYS_1DAY, JJ_ACUVUE_OASYS_1DAY_TORIC,
    JJ_ACUVUE_VITA, JJ_ACUVUE_VITA_TORIC,
    JJ_1DAY_ACUVUE_MOIST, JJ_1DAY_ACUVUE_MOIST_TORIC,
    JJ_1DAY_ACUVUE_TRU_EYE,
    JJ_ACUVUE2,
]

BRAND_CATALOG: dict[str, list[CLProduct]] = {}
for _p in ALL_PRODUCTS:
    BRAND_CATALOG.setdefault(_p.brand, []).append(_p)

BRANDS = list(BRAND_CATALOG.keys())


def get_product(brand: str, name: str) -> Optional[CLProduct]:
    for p in BRAND_CATALOG.get(brand, []):
        if p.name == name:
            return p
    return None


# ─────────────────────────────────────────────────────────────────────────────
# CONVERSION RESULT
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CLConversionResult:
    brand:          str
    product:        str
    lens_type:      str
    replace:        str
    bc:             list[float]
    dia:            list[float]
    # Input (spectacle Rx)
    spec_sph:       float
    spec_cyl:       float
    spec_axis:      int
    # Vertex-corrected (intermediate)
    vc_sph:         float
    vc_cyl:         float
    vc_axis:        int
    # Final CL Rx (snapped to available powers)
    cl_sph:         float
    cl_cyl:         float
    cl_axis:        int
    # Status
    in_range:       bool
    out_of_range_reason: str = ""
    warnings:       list[str] = field(default_factory=list)
    notes:          str = ""

    def display(self) -> str:
        if not self.in_range:
            return f"⚠️ Out of range: {self.out_of_range_reason}"
        if self.lens_type == "spherical":
            return f"SPH {self.cl_sph:+.2f}"
        return (f"SPH {self.cl_sph:+.2f} / CYL {self.cl_cyl:+.2f} / "
                f"AXIS {self.cl_axis:03d}°")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN CONVERTER
# ─────────────────────────────────────────────────────────────────────────────

def convert_rx_to_cl(
    sph: float,
    cyl: float = 0.0,
    axis: int  = 0,
    brand: str = "Bausch & Lomb",
    product: str = "Ultra",
) -> CLConversionResult:
    """
    Convert a spectacle Rx to contact lens power for a specific brand/product.

    Steps:
      1. Normalise to minus-cyl form
      2. Apply vertex distance correction (product-specific vertex_mm)
      3. Snap SPH to nearest available step within product range
      4. For toric: snap CYL to nearest available step, snap AXIS to nearest 10°
      5. Return full result with warnings if snapping was significant

    Args:
        sph:     Spectacle SPH (e.g. -3.25)
        cyl:     Spectacle CYL (e.g. -1.50 or 0.0 for spherical)
        axis:    Spectacle AXIS (e.g. 90)
        brand:   "Bausch & Lomb" or "CooperVision"
        product: Product name string (must match catalog exactly)

    Returns:
        CLConversionResult with cl_sph, cl_cyl, cl_axis and status flags.
    """
    prod = get_product(brand, product)
    if not prod:
        return CLConversionResult(
            brand=brand, product=product,
            lens_type="unknown", replace="", bc=[], dia=[],
            spec_sph=sph, spec_cyl=cyl, spec_axis=int(axis),
            vc_sph=sph, vc_cyl=cyl, vc_axis=int(axis),
            cl_sph=sph, cl_cyl=cyl, cl_axis=int(axis),
            in_range=False,
            out_of_range_reason=f"Product '{product}' not found in {brand} catalog."
        )

    sph  = float(sph  or 0)
    cyl  = float(cyl  or 0)
    axis = int(axis   or 0)

    warnings: list[str] = []

    # ── 1. Vertex correction ─────────────────────────────────────────────────
    # RULE: ALWAYS apply toric VD correction when |CYL| >= 0.25D, regardless
    # of whether the selected product is spherical or toric.
    # Reason: the two principal meridians each have different powers and must
    # be corrected independently. Zeroing CYL before VD correction loses the
    # meridional difference — clinically wrong for any toric prescription.
    #
    # For a SPHERICAL product selected with significant CYL entered:
    #   → Apply full toric VD correction
    #   → Then reduce to spherical equivalent + warn
    #
    # For a TORIC product: apply toric VD correction + snap CYL to steps.

    _has_significant_cyl = abs(cyl) >= 0.25

    if _has_significant_cyl:
        vc_sph, vc_cyl, vc_axis = _vertex_toric(sph, cyl, axis, prod.vertex_mm)
    else:
        vc_sph  = _vertex_sph(sph, prod.vertex_mm)
        vc_cyl  = 0.0
        vc_axis = axis

    # ── 2. Snap SPH to nearest available step ────────────────────────────────
    cl_sph_raw = _nearest(vc_sph, prod.sph_steps)

    # Check SPH in range
    lo, hi = prod.sph_range
    if not (lo - 0.01 <= cl_sph_raw <= hi + 0.01):
        return CLConversionResult(
            brand=brand, product=product,
            lens_type=prod.lens_type, replace=prod.replace,
            bc=prod.bc, dia=prod.dia,
            spec_sph=sph, spec_cyl=cyl, spec_axis=axis,
            vc_sph=round(vc_sph, 2), vc_cyl=round(vc_cyl, 2), vc_axis=int(vc_axis),
            cl_sph=cl_sph_raw, cl_cyl=0.0, cl_axis=0,
            in_range=False,
            out_of_range_reason=(
                f"SPH {vc_sph:+.2f} is outside {prod.name} range "
                f"({lo:+.2f} to {hi:+.2f})."
            ),
            notes=prod.notes
        )

    if abs(cl_sph_raw - vc_sph) >= 0.375:
        warnings.append(
            f"SPH snapped from {vc_sph:+.2f} → {cl_sph_raw:+.2f} "
            f"(step rounding > 0.375D)"
        )

    # ── 3. CYL handling ──────────────────────────────────────────────────────
    cl_cyl  = 0.0
    cl_axis = 0

    if _has_significant_cyl:

        if prod.lens_type == "toric" and prod.cyl_steps:
            # ── Toric product: snap CYL to available steps ───────────────
            if abs(vc_cyl) < 0.37:
                # CYL too small for toric — reduce to spherical.
                # CORRECT ORDER: SE from spectacle Rx first, then VD correct.
                se_spec    = sph + cyl / 2.0
                vc_se      = _vertex_sph(se_spec, prod.vertex_mm)
                cl_sph_raw = _nearest(vc_se, prod.sph_steps)
                warnings.append(
                    f"CYL {cyl:+.2f} too small for toric — "
                    f"SE = {sph:+.2f} + ({cyl:+.2f}/2) = {se_spec:+.2f} → "
                    f"VD corrected {vc_se:+.2f} → snapped {cl_sph_raw:+.2f}."
                )
            else:
                cl_cyl_raw = _nearest(vc_cyl, prod.cyl_steps)
                if abs(cl_cyl_raw - vc_cyl) >= 0.375:
                    warnings.append(
                        f"CYL snapped from {vc_cyl:+.2f} → {cl_cyl_raw:+.2f}"
                    )
                # Snap AXIS to nearest available step
                # Handle 0° = 180° wraparound: axis 180 becomes 0 after % 180
                # but clinically 0° and 180° are the same meridian.
                # Normalise to 180 when input was 180° or snapping would go to 10°.
                if prod.axis_steps:
                    _valid_axes = [a for a in prod.axis_steps if 0 < a <= 180]
                    # Normalise vc_axis: 0 → 180 (same meridian)
                    _snap_from = 180 if vc_axis == 0 else vc_axis
                    cl_axis_raw = _nearest(_snap_from, _valid_axes)
                    _diff = min(
                        abs(cl_axis_raw - _snap_from),
                        180 - abs(cl_axis_raw - _snap_from)  # wraparound distance
                    )
                    if _diff >= 15:
                        warnings.append(
                            f"AXIS snapped from {_snap_from}° → {cl_axis_raw}° "
                            f"(difference ≥15°)"
                        )
                    cl_axis = int(cl_axis_raw)
                else:
                    cl_axis = int(vc_axis) % 180 or 180
                cl_cyl = cl_cyl_raw

        else:
            # ── Spherical product selected but CYL is significant ─────────
            # CORRECT ORDER: SE first (from spectacle Rx), then VD correct.
            # SE = sph + cyl/2  (spectacle values, before any VD correction)
            # Then vertex-correct the SE as a single spherical power.
            se_spec    = sph + cyl / 2.0
            vc_se      = _vertex_sph(se_spec, prod.vertex_mm)
            cl_sph_raw = _nearest(vc_se, prod.sph_steps)
            cl_cyl     = 0.0
            cl_axis    = 0
            warnings.append(
                f"⚠️ Toric Rx (CYL {cyl:+.2f}) on spherical product — "
                f"SE = {sph:+.2f} + ({cyl:+.2f}/2) = {se_spec:+.2f} → "
                f"VD corrected {vc_se:+.2f} → snapped {cl_sph_raw:+.2f}. "
                f"Switch to Toric variant for full CYL correction."
            )

    return CLConversionResult(
        brand=brand, product=product,
        lens_type=prod.lens_type, replace=prod.replace,
        bc=prod.bc, dia=prod.dia,
        spec_sph=sph, spec_cyl=cyl, spec_axis=axis,
        vc_sph=round(vc_sph, 2), vc_cyl=round(vc_cyl, 2), vc_axis=int(vc_axis),
        cl_sph=cl_sph_raw, cl_cyl=cl_cyl, cl_axis=cl_axis,
        in_range=True,
        warnings=warnings,
        notes=prod.notes
    )


# ─────────────────────────────────────────────────────────────────────────────
# STREAMLIT UI
# ─────────────────────────────────────────────────────────────────────────────

def render_cl_converter_ui():
    """
    Render the full Contact Lens Converter UI in Streamlit.
    Call this from any page that imports it.
    """
    import streamlit as st

    st.markdown(
        "<h3 style='color:#6366f1'>👁️ Contact Lens Power Converter</h3>",
        unsafe_allow_html=True
    )
    st.caption(
        "Converts spectacle Rx to contact lens power with vertex distance "
        "correction. Snaps to the nearest available power for each product."
    )

    # ── Brand & Product selectors ─────────────────────────────────────────────
    col_brand, col_product = st.columns([1, 2])
    with col_brand:
        brand = st.selectbox("Brand", BRANDS, key="cl_conv_brand")
    with col_product:
        product_names = [p.name for p in BRAND_CATALOG[brand]]
        product = st.selectbox("Product", product_names, key="cl_conv_product")

    prod = get_product(brand, product)
    if prod:
        replace_label = {
            "daily": "🗓️ Daily", "monthly": "📅 Monthly",
            "fortnightly": "📅 Fortnightly", "quarterly": "📅 Quarterly"
        }.get(prod.replace, prod.replace)
        info_cols = st.columns(5)
        info_cols[0].metric("Replacement", replace_label)
        info_cols[1].metric("Material", prod.material)
        info_cols[2].metric("Water", f"{prod.water}%")
        info_cols[3].metric("Base Curve", " / ".join(str(b) for b in prod.bc))
        info_cols[4].metric("Diameter", " / ".join(str(d) for d in prod.dia))
        if prod.notes:
            st.caption(f"ℹ️ {prod.notes}")

    st.markdown("---")

    # ── Rx input ─────────────────────────────────────────────────────────────
    st.markdown("**Enter Spectacle Rx**")
    rc1, rc2, rc3, rc4, rc5 = st.columns([1, 1, 1, 0.2, 1])

    with rc1:
        st.markdown("<div style='color:#94a3b8;font-size:0.75rem'>👁 RIGHT EYE</div>",
                    unsafe_allow_html=True)
    r_sph  = rc1.number_input("SPH (R)",  min_value=-25.0, max_value=25.0,
                               value=0.0, step=0.25, format="%.2f",
                               key="cl_r_sph", label_visibility="collapsed")
    r_cyl  = rc2.number_input("CYL (R)",  min_value=-10.0, max_value=10.0,
                               value=0.0, step=0.25, format="%.2f",
                               key="cl_r_cyl", label_visibility="collapsed")
    r_axis = rc3.number_input("AXIS (R)", min_value=0,     max_value=180,
                               value=0,   step=1,
                               key="cl_r_axis", label_visibility="collapsed")

    with rc4:
        st.write("")

    with rc5:
        st.markdown("<div style='color:#94a3b8;font-size:0.75rem'>👁 LEFT EYE</div>",
                    unsafe_allow_html=True)
    l_sph  = rc5.number_input("SPH (L)",  min_value=-25.0, max_value=25.0,
                               value=0.0, step=0.25, format="%.2f",
                               key="cl_l_sph", label_visibility="collapsed")

    lc1, lc2, lc3 = st.columns([1, 1, 1])
    l_cyl  = lc1.number_input("CYL (L)",  min_value=-10.0, max_value=10.0,
                               value=0.0, step=0.25, format="%.2f",
                               key="cl_l_cyl")
    l_axis = lc2.number_input("AXIS (L)", min_value=0,     max_value=180,
                               value=0,   step=1,
                               key="cl_l_axis")

    st.markdown("---")

    if st.button("🔄 Convert Rx to Contact Lens Power",
                 type="primary", use_container_width=True,
                 key="cl_conv_btn"):

        for eye, s, c, a in [("Right", r_sph, r_cyl, r_axis),
                              ("Left",  l_sph, l_cyl, l_axis)]:

            res = convert_rx_to_cl(s, c, int(a), brand, product)
            eye_icon = "👁️R" if eye == "Right" else "👁️L"

            if not res.in_range:
                st.error(f"{eye_icon} **{eye} Eye** — {res.out_of_range_reason}")
                continue

            with st.container():
                st.markdown(
                    f"<div style='background:#0f172a;border-left:4px solid #6366f1;"
                    f"border-radius:8px;padding:12px 18px;margin:8px 0'>",
                    unsafe_allow_html=True
                )
                st.markdown(
                    f"**{eye_icon} {eye} Eye** &nbsp;·&nbsp; "
                    f"<span style='color:#6366f1;font-size:1.1rem;font-weight:700'>"
                    f"{res.display()}</span>",
                    unsafe_allow_html=True
                )

                detail_cols = st.columns(3)
                detail_cols[0].markdown(
                    f"**Spectacle Rx**  \n"
                    f"SPH {res.spec_sph:+.2f} / CYL {res.spec_cyl:+.2f} / AXIS {res.spec_axis}°"
                )
                detail_cols[1].markdown(
                    f"**After Vertex Correction**  \n"
                    f"SPH {res.vc_sph:+.2f} / CYL {res.vc_cyl:+.2f} / AXIS {res.vc_axis}°"
                )
                detail_cols[2].markdown(
                    f"**CL Power ({product})**  \n"
                    f"{res.display()}"
                )

                if res.warnings:
                    for w in res.warnings:
                        st.warning(f"⚠️ {w}")

                st.markdown("</div>", unsafe_allow_html=True)

        # ── Product info reminder ─────────────────────────────────────────
        if prod:
            st.info(
                f"📦 **{prod.brand} {prod.name}** · "
                f"BC {' / '.join(str(b) for b in prod.bc)} · "
                f"Dia {' / '.join(str(d) for d in prod.dia)} · "
                f"{prod.replace.title()} replacement · "
                f"{prod.water}% water · {prod.material}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# QUICK PROGRAMMATIC ACCESS (no UI)
# ─────────────────────────────────────────────────────────────────────────────

def convert_both_eyes(
    r_sph: float, r_cyl: float, r_axis: int,
    l_sph: float, l_cyl: float, l_axis: int,
    brand: str, product: str
) -> dict:
    """Convert both eyes in one call. Returns {'R': result, 'L': result}."""
    return {
        "R": convert_rx_to_cl(r_sph, r_cyl, r_axis, brand, product),
        "L": convert_rx_to_cl(l_sph, l_cyl, l_axis, brand, product),
    }


# ─────────────────────────────────────────────────────────────────────────────
# SIDEBAR PANEL — Rx → Calculate → Brand/Product → Result → Use
# ─────────────────────────────────────────────────────────────────────────────

def _eye_calc(sph, cyl, axis, vertex_mm):
    """Compute toric VD result and SE VD result for one eye."""
    sph, cyl, axis = float(sph or 0), float(cyl or 0), int(axis or 0)
    result = {}

    # Toric path — always compute if CYL >= 0.25
    if abs(cyl) >= 0.25:
        vc_s, vc_c, vc_a = _vertex_toric(sph, cyl, axis, vertex_mm)
        result["toric"] = {
            "vc_sph":  round(vc_s, 4),
            "vc_cyl":  round(vc_c, 4),
            "vc_axis": 180 if vc_a == 0 else vc_a,
        }
    else:
        result["toric"] = None

    # Spherical equivalent path — SE first, then VD
    se_spec = sph + cyl / 2.0
    vc_se   = _vertex_sph(se_spec, vertex_mm)
    result["sph"] = {"se_spec": round(se_spec, 2), "vc_se": round(vc_se, 4)}
    return result


def _snap_eye(calc: dict, prod, force_sph=False) -> dict:
    """
    Snap one eye's VD-corrected values to a product's available steps.
    Returns {sph, cyl, axis, in_range, lens_type_used}.

    Mixed-eye rule: if this eye has no CYL (calc["toric"] is None),
    snap SPH only — even on a toric product. The same product accommodates
    one eye spherical + other eye toric.
    """
    sph_steps  = prod.sph_steps
    cyl_steps  = prod.cyl_steps or []
    ax_steps   = [a for a in (prod.axis_steps or []) if 0 < a <= 180]
    lo, hi     = prod.sph_range

    # Decide path
    use_toric = (
        not force_sph
        and prod.lens_type == "toric"
        and bool(cyl_steps)
        and calc.get("toric") is not None
    )

    if use_toric:
        t = calc["toric"]
        cl_sph = _nearest(t["vc_sph"], sph_steps)
        vc_cyl = t["vc_cyl"]

        if abs(vc_cyl) >= 0.37:
            cl_cyl  = _nearest(vc_cyl, cyl_steps)
            snap_ax = 180 if t["vc_axis"] == 0 else t["vc_axis"]
            cl_axis = _nearest(snap_ax, ax_steps) if ax_steps else (snap_ax or 180)
        else:
            # Residual CYL too small — spherical equivalent
            se_spec = calc["sph"]["se_spec"]
            vc_se   = calc["sph"]["vc_se"]
            cl_sph  = _nearest(vc_se, sph_steps)
            cl_cyl, cl_axis = 0.0, 0

        in_range = lo - 0.01 <= cl_sph <= hi + 0.01
        return {"sph": cl_sph, "cyl": cl_cyl, "axis": int(cl_axis),
                "in_range": in_range, "lens_type_used": "toric" if cl_cyl else "sph_equiv"}

    else:
        # Spherical path — SE already VD-corrected
        vc_se  = calc["sph"]["vc_se"]
        cl_sph = _nearest(vc_se, sph_steps)
        in_range = lo - 0.01 <= cl_sph <= hi + 0.01
        return {"sph": cl_sph, "cyl": 0.0, "axis": 0,
                "in_range": in_range, "lens_type_used": "sph_equiv"}


def _rx_line(d: dict) -> str:
    if not d.get("in_range"):
        return "⚠️ Out of range"
    if d.get("cyl"):
        return f"SPH {d['sph']:+.2f} / CYL {d['cyl']:+.2f} @ {d['axis']}°"
    return f"SPH {d['sph']:+.2f}"


def render_cl_sidebar_panel():
    """
    Flow:
      1. Rx entry (R + L)
      2. [Calculate]
      3. Brand dropdown  →  Product dropdown  (filtered to in-range products)
      4. R and L result shown (Toric or SE automatically per eye)
      5. Radio: Use Toric / Use Spherical Equivalent  (only if toric CYL exists)
      6. [✅ Use This Power]  →  session state for Power Entry hint
    """
    import streamlit as st

    with st.sidebar.expander("👁️ CL Calculator", expanded=False):

        # ── VD ────────────────────────────────────────────────────────────────
        vd_mm = st.select_slider(
            "VD mm",
            options=[11.0, 11.5, 12.0, 12.5, 13.0, 13.5, 14.0],
            value=st.session_state.get("cl_sb_vd_val", 12.5),
            key="cl_sb_vd",
            help="Vertex distance — typically 12–13mm"
        )
        st.session_state["cl_sb_vd_val"] = vd_mm

        # ── Rx input ─────────────────────────────────────────────────────────
        st.markdown(
            "<div style='font-size:0.71rem;color:#6b7280;margin:4px 0 1px'>"
            "R &nbsp; SPH &nbsp;&nbsp;&nbsp; CYL &nbsp;&nbsp;&nbsp; AXIS</div>",
            unsafe_allow_html=True
        )
        rc1, rc2, rc3 = st.columns(3)
        r_sph  = rc1.number_input("r_sph", -30.0, 30.0, 0.0, 0.25, format="%.2f",
                                   key="cl_sb_r_sph", label_visibility="collapsed")
        r_cyl  = rc2.number_input("r_cyl", -10.0, 10.0, 0.0, 0.25, format="%.2f",
                                   key="cl_sb_r_cyl", label_visibility="collapsed")
        r_axis = rc3.number_input("r_ax",  0, 180, 0, 1,
                                   key="cl_sb_r_axis", label_visibility="collapsed")

        st.markdown(
            "<div style='font-size:0.71rem;color:#6b7280;margin:1px 0'>"
            "L &nbsp; SPH &nbsp;&nbsp;&nbsp; CYL &nbsp;&nbsp;&nbsp; AXIS</div>",
            unsafe_allow_html=True
        )
        lc1, lc2, lc3 = st.columns(3)
        l_sph  = lc1.number_input("l_sph", -30.0, 30.0, 0.0, 0.25, format="%.2f",
                                   key="cl_sb_l_sph", label_visibility="collapsed")
        l_cyl  = lc2.number_input("l_cyl", -10.0, 10.0, 0.0, 0.25, format="%.2f",
                                   key="cl_sb_l_cyl", label_visibility="collapsed")
        l_axis = lc3.number_input("l_ax",  0, 180, 0, 1,
                                   key="cl_sb_l_axis", label_visibility="collapsed")

        if not st.button("🔄 Calculate", key="cl_sb_calc",
                         use_container_width=True):
            # Show last result if available
            _last = st.session_state.get("_cl_calc_cache")
            if _last:
                st.caption(f"Last: {_last.get('product','')} · "
                           f"R {_last.get('r_label','')} · "
                           f"L {_last.get('l_label','')}")
            return

        # ── Calculate ─────────────────────────────────────────────────────────
        r_calc = _eye_calc(r_sph, r_cyl, int(r_axis), vd_mm)
        l_calc = _eye_calc(l_sph, l_cyl, int(l_axis), vd_mm)

        # Cache calcs so brand/product dropdowns can use them without recalculating
        st.session_state["_cl_r_calc"] = r_calc
        st.session_state["_cl_l_calc"] = l_calc
        st.session_state["_cl_calc_done"] = True

    # ── Outside the if-button block so dropdowns survive rerun ────────────────
    if not st.session_state.get("_cl_calc_done"):
        return

    r_calc = st.session_state.get("_cl_r_calc", {})
    l_calc = st.session_state.get("_cl_l_calc", {})

    with st.sidebar.expander("👁️ CL Calculator", expanded=True):

        st.markdown("---")

        # ── Has CYL on either eye? ────────────────────────────────────────────
        _r_has_cyl = r_calc.get("toric") is not None
        _l_has_cyl = l_calc.get("toric") is not None
        _any_cyl   = _r_has_cyl or _l_has_cyl

        # ── Brand dropdown ────────────────────────────────────────────────────
        sel_brand = st.selectbox(
            "Brand", BRANDS,
            key="cl_sb_brand2"
        )

        # Filter products for selected brand
        brand_products = BRAND_CATALOG.get(sel_brand, [])

        # Mark which products are in-range for both eyes
        def _both_in_range(prod):
            r = _snap_eye(r_calc, prod)
            l = _snap_eye(l_calc, prod)
            return r["in_range"] or l["in_range"]

        available_products = [p for p in brand_products if _both_in_range(p)]
        if not available_products:
            available_products = brand_products  # show all if none in range

        product_names = [p.name for p in available_products]

        sel_product = st.selectbox(
            "Product", product_names,
            key="cl_sb_product2"
        )

        sel_prod = get_product(sel_brand, sel_product)
        if not sel_prod:
            st.caption("Product not found")
            return

        # Product info line
        _rep = {"daily":"Daily","monthly":"Monthly","fortnightly":"2-Week","quarterly":"Quarterly"}.get(sel_prod.replace, sel_prod.replace)
        st.caption(
            f"{_rep} · BC {sel_prod.bc[0]} · Dia {sel_prod.dia[0]} · "
            f"{sel_prod.water}% H₂O · {sel_prod.material}"
        )

        # ── Radio: Toric or SE (only when CYL present AND toric product) ──────
        force_sph = False
        if _any_cyl and sel_prod.lens_type == "toric" and sel_prod.cyl_steps:
            rx_choice = st.radio(
                "Rx type for this product",
                ["🌀 Toric — use full CYL", "○ Spherical Equivalent"],
                index=0,
                key="cl_sb_rx_type",
                horizontal=False,
                label_visibility="collapsed"
            )
            force_sph = rx_choice.startswith("○")
        elif _any_cyl and sel_prod.lens_type != "toric":
            st.caption("○ Spherical product — SE used for CYL eyes")
            force_sph = True

        # ── Compute final snapped result ──────────────────────────────────────
        r_final = _snap_eye(r_calc, sel_prod, force_sph=force_sph)
        l_final = _snap_eye(l_calc, sel_prod, force_sph=force_sph)

        # ── Result display ────────────────────────────────────────────────────
        _r_type_col = "#10b981" if r_final.get("lens_type_used") == "toric" else "#f59e0b"
        _l_type_col = "#10b981" if l_final.get("lens_type_used") == "toric" else "#f59e0b"

        st.markdown(
            f"<div style='background:#0f172a;border:1px solid #1e293b;"
            f"border-radius:8px;padding:8px 12px;margin:4px 0'>"
            f"<div style='font-size:0.7rem;color:#475569;margin-bottom:4px'>"
            f"{'🌀 Toric' if sel_prod.lens_type=='toric' else '○ Spherical'} · "
            f"VD {vd_mm}mm</div>"
            f"<div style='color:{_r_type_col};font-size:0.82rem;font-weight:700'>"
            f"R &nbsp; {_rx_line(r_final)}</div>"
            f"<div style='color:{_l_type_col};font-size:0.82rem;font-weight:700;margin-top:2px'>"
            f"L &nbsp; {_rx_line(l_final)}</div>"
            f"</div>",
            unsafe_allow_html=True
        )

        # Mixed eye note
        if _r_has_cyl != _l_has_cyl:
            mixed_eye = "R" if _r_has_cyl else "L"
            sph_eye   = "L" if _r_has_cyl else "R"
            st.caption(
                f"ℹ️ Mixed prescription — {mixed_eye} toric, {sph_eye} spherical. "
                f"Both fit on same lens."
            )

        # SE working note
        if force_sph or (not _r_has_cyl and not _l_has_cyl):
            r_se = r_calc["sph"]
            l_se = l_calc["sph"]
            st.caption(
                f"SE: R {r_se['se_spec']:+.2f} → {r_se['vc_se']:+.2f} · "
                f"L {l_se['se_spec']:+.2f} → {l_se['vc_se']:+.2f}"
            )

        # ── Use This Power ────────────────────────────────────────────────────
        if st.button("✅ Use This Power", key="cl_sb_use",
                     use_container_width=True, type="primary"):

            st.session_state["_last_cl_result"] = {
                "brand":   sel_brand,
                "product": sel_product,
                "vd_mm":   vd_mm,
                "rx_type": "toric" if (not force_sph and sel_prod.lens_type == "toric") else "sph_equiv",
                "R": {
                    "sph":  r_final["sph"], "cyl": r_final["cyl"],
                    "axis": r_final["axis"], "ok": r_final["in_range"],
                },
                "L": {
                    "sph":  l_final["sph"], "cyl": l_final["cyl"],
                    "axis": l_final["axis"], "ok": l_final["in_range"],
                },
            }
            # Cache labels for collapsed view
            st.session_state["_cl_calc_cache"] = {
                "product":  sel_product,
                "r_label":  _rx_line(r_final),
                "l_label":  _rx_line(l_final),
            }
            st.session_state["_cl_hint_dismissed"] = False  # re-show hint
            st.toast(f"✅ {sel_product} saved → Power Entry")
