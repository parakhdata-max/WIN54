"""
Small display/save helpers for human names.

Only use these for patient/person names. Do not use for shops, suppliers,
brands, products, or wholesale party names because intentional capitals such
as ISEE, DV, A.K., Pvt Ltd, and brand styling can be damaged by title-casing.
"""

from __future__ import annotations

import re


_WORD_RE = re.compile(r"([A-Za-z]+)")


def _format_word(match: re.Match) -> str:
    word = match.group(1)
    if len(word) == 1:
        return word.upper()
    return word[0].upper() + word[1:].lower()


def format_person_name(name: object) -> str:
    """
    Normalize patient/person names for display and patient-master saves.

    Examples:
        RAJESH ZILPE  -> Rajesh Zilpe
        aarya tiwari  -> Aarya Tiwari
        A. k. joshi   -> A. K. Joshi

    Non-ASCII text is returned with whitespace normalized only.
    """
    text = " ".join(str(name or "").strip().split())
    if not text:
        return ""
    if not re.search(r"[A-Za-z]", text):
        return text
    text = _WORD_RE.sub(_format_word, text)
    # Keep leading initials compact: "A. K. Joshi" -> "A.K. Joshi".
    text = re.sub(r"\b([A-Z])\.\s+(?=[A-Z]\.)", r"\1.", text)
    return text
