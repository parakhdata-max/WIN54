from typing import Tuple
import math

# ======================================================
# CONSTANTS
# ======================================================

_AVAILABLE_TORIC_CYLS = [
    -0.75, -1.25, -1.75, -2.25,
    -2.75, -3.25, -3.50, -3.75, -4.00
]


# ======================================================
# BASIC HELPERS
# ======================================================

def closest_toric(cyl: float) -> float:
    c = float(cyl)

    choices = sorted(
        [(abs(c - v), abs(v), v) for v in _AVAILABLE_TORIC_CYLS],
        key=lambda x: (x[0], x[1])
    )

    return float(choices[0][2])


def truncate_to_step_toward_zero(v: float, step: float) -> float:

    if step == 0:
        return v

    sign = -1 if v < 0 else 1
    av = abs(v)

    n = math.floor(av / step)

    return round(sign * (n * step), 4)


def round_sph_truncate(v: float) -> float:

    try:
        v = float(v)
    except:
        return v

    sign = -1 if v < 0 else 1
    av = abs(v)

    if av <= 20.0:
        rounded = round(av * 4) / 4.0
        res = sign * rounded

        if res == -0.0:
            res = 0.0

        return round(res, 2)

    return truncate_to_step_toward_zero(v, 0.5)


# ======================================================
# TRANSPOSE
# ======================================================

def compute_transpose_if_needed(
    sph: float,
    cyl: float,
    axis: float
) -> Tuple[float, float, int]:

    s = float(sph)
    c = float(cyl)
    a = int(round(axis)) % 360

    if c > 0:
        sph_t = s + c
        cyl_t = -c
        axis_t = (a - 90) % 180

        return sph_t, cyl_t, axis_t

    return s, c, a % 180


# ======================================================
# CONTACT LENS VERTEX (CORRECT)
# ======================================================

def vertex_correct_spherical(
    sph_spec: float,
    vertex_mm: float = 12.5
) -> float:
    """
    Vertex distance correction — spherical.
    Correct formula: F_CL = F_spec / (1 - d x F_spec)
    Default VD = 12.5mm (clinical standard).
    """
    F = float(sph_spec)
    d = float(vertex_mm) / 1000.0

    denom = 1.0 - (F * d)

    if abs(denom) < 1e-12:
        return F

    return round_sph_truncate(F / denom)


def vertex_correct_toric(
    sph: float,
    cyl: float,
    axis: float,
    vertex_mm: float = 12.5
) -> Tuple[float, float, int]:
    """
    Vertex distance correction — toric.
    Applies correct formula F_CL = F / (1 - d x F) to both principal meridians.
    Default VD = 12.5mm (clinical standard).
    """
    # Minus cyl form
    sph, cyl, axis = compute_transpose_if_needed(sph, cyl, axis)

    mer1 = sph
    mer2 = sph + cyl
    d    = float(vertex_mm) / 1000.0

    def eff(F):
        denom = 1.0 - (F * d)   # CORRECT sign: 1 - F*d
        if abs(denom) < 1e-12:
            return F
        return F / denom

    cl_sph = eff(mer1)
    cl_cyl = eff(mer2) - eff(mer1)
    cl_axis = axis

    if abs(cl_cyl) < 1e-9:
        cl_cyl = 0.0
    else:
        cl_cyl = closest_toric(cl_cyl)

    cl_sph  = round_sph_truncate(cl_sph)
    cl_axis = int(round(cl_axis)) % 180

    return float(cl_sph), float(cl_cyl), cl_axis


# ======================================================
# TOOL HELPERS
# ======================================================

def parse_basecurve_to_3digit(bc):

    try:
        v = float(bc)
    except:
        return 400

    if v >= 100:
        return int(round(v))

    return int(round(v * 100))


def to_3digit_power(v: float) -> int:
    return int(round(float(v) * 100))


def compute_toolAB_ints(bc, sph, cyl):

    s = float(sph)
    c = float(cyl)

    if c > 0:
        s = s + c
        c = -c

    BCi = parse_basecurve_to_3digit(bc)

    SPHi = to_3digit_power(s)
    CYLi = to_3digit_power(c)

    A = BCi - SPHi
    B = A - CYLi

    return int(A), int(B)


# ======================================================
# KRYPTOK
# ======================================================

def apply_kryptok_axis_correction(axis: int, eye: str) -> int:

    a = int(axis) % 180
    eye = (eye or "").upper()

    if eye in ["R", "RIGHT"]:
        return (a - 15) % 180

    return (a + 15) % 180


# ======================================================
# BASE CURVE
# ======================================================

def sph_equivalent(s, c):
    return float(s) + float(c) / 2.0


def recommend_base_curve(sph: float, cyl: float) -> float:

    try:
        se = sph_equivalent(sph, cyl)
    except:
        se = 0.0

    if se >= 4:
        return 8.00
    elif se >= 2:
        return 6.00
    elif se >= 0:
        return 5.50
    else:
        return 4.00


# ======================================================
# MAIN ENGINE
# ======================================================

def calculate_surfacing_powers(
    sph: float,
    cyl: float,
    axis: float,
    eye_side: str,
    category: str,
    base_curve: float = None,
    is_contact_lens: bool = False,
    use_effectivity: bool = False
) -> dict:

    # -------------------------------
    # Parse
    # -------------------------------

    try:
        sph = float(sph)
        cyl = float(cyl)
        axis = float(axis)
    except:
        sph = float(sph or 0)
        cyl = float(cyl or 0)
        axis = int(axis or 0)

    # -------------------------------
    # Detect CL Mode
    # -------------------------------

    cl_mode = False

    if is_contact_lens:
        cl_mode = True

    if use_effectivity:
        cl_mode = True

    if category and "contact" in str(category).lower():
        cl_mode = True

    # -------------------------------
    # Transpose
    # -------------------------------

    sph, cyl, axis = compute_transpose_if_needed(
        sph, cyl, axis
    )

    no_cyl = cyl in [None, "", 0, 0.0]

    # -------------------------------
    # POWER LOGIC
    # -------------------------------

    if cl_mode:

        # ===== CONTACT LENS =====

        if no_cyl:

            sph_surf = vertex_correct_spherical(sph)
            cyl_surf = 0.0

        else:

            sph_surf, cyl_surf, axis = vertex_correct_toric(
                sph, cyl, axis
            )

        axis_surf = int(axis) % 180


    else:

        # ===== OPHTHALMIC =====

        sph_surf = round(sph, 2)

        if no_cyl:
            cyl_surf = 0.0
        else:
            cyl_surf = round(cyl, 2)

        axis_surf = int(axis) % 180


    original_axis = axis_surf


    # -------------------------------
    # Kryptok
    # -------------------------------

    kryptok = False

    # Kryptok detection: match "kryptok", "kt bifocal", "kt bifocals", or standalone "kt"
    _cat_lower = str(category).lower() if category else ""
    _is_kryptok = (
        "kryptok" in _cat_lower
        or "kt bifocal" in _cat_lower
        or _cat_lower.strip() in ("kt", "kt bifocals", "kt bifocal")
    )
    if _is_kryptok:

        kryptok = True

        axis_surf = apply_kryptok_axis_correction(
            axis_surf,
            eye_side
        )


    # -------------------------------
    # Tools
    # -------------------------------

    tool_a = None
    tool_b = None

    if base_curve:

        try:
            tool_a, tool_b = compute_toolAB_ints(
                base_curve,
                sph_surf,
                cyl_surf
            )
        except:
            pass


    # -------------------------------
    # Output
    # -------------------------------

    return {
        "sph_surf": float(sph_surf),
        "cyl_surf": float(cyl_surf),
        "axis_surf": int(axis_surf),

        "original_axis": int(original_axis),

        "kryptok_correction_applied": kryptok,

        "tool_a": tool_a,
        "tool_b": tool_b,
    }
