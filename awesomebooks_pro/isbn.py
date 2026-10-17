"""ISBN normalisation and validation.

Client spreadsheets arrive in every imaginable shape: ``978-1-63973-605-8``,
ISBN-10s, vendor-prefixed codes such as ``AB-9781639736058``, AwesomeBooks' own
SKUs (``9053-9780241003008``, ``4444-PEN-9781529961034``) or numbers that Excel
has turned into ``9.78164E+12``. Everything funnels through
:func:`extract_isbn`, which returns a checksum-valid ISBN-13 or ``None``.

AwesomeBooks shows two ISBNs per book (ISBN-13 and ISBN-10). The client works
with ISBN-13 only, so an ISBN-10 is always upgraded to its ISBN-13.
"""

from __future__ import annotations

import re

_SCIENTIFIC = re.compile(r"^\d(\.\d+)?e\+?\d+$", re.IGNORECASE)
# Overlapping search: in "90539780241003008" the ISBN-13 starts at the fifth digit.
_ISBN13_WINDOW = re.compile(r"(?=(97[89]\d{10}))")
_RUN = re.compile(r"[0-9Xx]+")


def isbn13_is_valid(code: str) -> bool:
    if not re.fullmatch(r"\d{13}", code or ""):
        return False
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(code[:12]))
    return (10 - total % 10) % 10 == int(code[12])


def isbn10_is_valid(code: str) -> bool:
    if not re.fullmatch(r"\d{9}[\dX]", code or "", re.IGNORECASE):
        return False
    digits = [10 if c in "xX" else int(c) for c in code]
    return sum((10 - i) * d for i, d in enumerate(digits)) % 11 == 0


def isbn10_to_13(code: str) -> str:
    core = "978" + code[:9]
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(core))
    return core + str((10 - total % 10) % 10)


def looks_like_scientific_notation(raw: str) -> bool:
    """Excel mangles long numbers into ``9.78164E+12``: the digits are gone for good."""
    return bool(_SCIENTIFIC.match(raw.strip()))


def extract_isbn(raw: object) -> str | None:
    """Return a valid ISBN-13 found inside ``raw`` or ``None``.

    Hyphens and spaces are ignored, vendor prefixes are skipped, and ISBN-10s
    are upgraded to ISBN-13. A checksum-valid ISBN-13 anywhere in the value
    wins; an ISBN-10 is only accepted as a stand-alone 10-character code, so a
    long SKU can never yield a coincidental match.
    """
    if raw is None:
        return None
    if isinstance(raw, float):
        if raw != raw or raw < 1e9:  # NaN / too small to be an ISBN
            return None
        raw = f"{raw:.0f}"
    text = str(raw).strip()
    if not text or looks_like_scientific_notation(text):
        return None
    compact = re.sub(r"[\s\-‐-―]", "", text)
    if compact.endswith(".0"):  # "9781639736058.0" from numeric spreadsheet cells
        compact = compact[:-2]
    for match in _ISBN13_WINDOW.finditer(compact):
        if isbn13_is_valid(match.group(1)):
            return match.group(1)
    for run in _RUN.findall(compact):
        if len(run) == 10 and isbn10_is_valid(run):
            return isbn10_to_13(run.upper())
    return None


def isbn_from_sku(sku: str | None) -> str | None:
    """AwesomeBooks SKUs carry the ISBN-13 after a supplier prefix (``4444-PEN-9781529961034``)."""
    return extract_isbn(sku) if sku else None
