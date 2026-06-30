"""Field normalizers.

Each normalizer is a pure function that turns one raw value into its
canonical form, or returns ``None`` when the value cannot be trusted in that
form. The contract is uniform and strict: **return a value or None, never
coerce a structurally invalid input into a fake-valid one** (a bad phone is
rejected, not padded into E.164).

Canonical formats (per the design):
  * phones    -> E.164                     (libphonenumber; invalid -> None)
  * countries -> ISO-3166 alpha-2
  * dates     -> "YYYY-MM"                 (bare year -> -01; Present -> None)
  * skills    -> canonical name            (alias table; unknowns kept raw)
"""

from __future__ import annotations

import pathlib
import re
from typing import Optional

import phonenumbers

from transformer.config_files import load_countries, load_skill_aliases

# Tokens that mean "this role/education has no end date yet". Compared
# case-insensitively after stripping. Kept as a module constant rather than
# inline so the vocabulary is defined in exactly one place.
_ONGOING_TOKENS = frozenset({"present", "current", "now", "ongoing", ""})

# A 4-digit year, optionally followed by a month, in the forms we accept:
# "2021", "2021-07", "2021/7", "07/2021", "July 2021" (month name handled
# separately). This regex covers the numeric YYYY[-/]MM and MM[-/]YYYY cases.
_YEAR_FIRST = re.compile(r"^(?P<year>\d{4})[-/]?(?P<month>\d{1,2})?$")
_MONTH_FIRST = re.compile(r"^(?P<month>\d{1,2})[-/](?P<year>\d{4})$")

_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def normalize_phone(
    raw: Optional[str], default_region: Optional[str] = None
) -> Optional[str]:
    """Return ``raw`` as an E.164 string, or None if it is not a valid number.

    A ``default_region`` (ISO alpha-2) lets national-format numbers without a
    country code be parsed; numbers that are merely well-shaped but not
    actually assignable are rejected, never coerced.
    """
    if not raw or not str(raw).strip():
        return None
    try:
        parsed = phonenumbers.parse(str(raw), default_region)
    except phonenumbers.NumberParseException:
        return None
    # ``is_valid_number`` enforces real length/prefix rules for the region;
    # ``is_possible_number`` alone would wave through impossible numbers.
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(
        parsed, phonenumbers.PhoneNumberFormat.E164
    )


def normalize_email(raw: Optional[str]) -> Optional[str]:
    """Return a lower-cased, trimmed email if it is syntactically valid.

    Email is a strong identity key, so we apply a deliberately conservative
    shape check (one ``@``, a dotted domain) and reject anything else rather
    than risk merging on a malformed address.
    """
    if not raw:
        return None
    candidate = str(raw).strip().lower()
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", candidate):
        return candidate
    return None


def normalize_country(
    raw: Optional[str], config_dir: Optional[pathlib.Path] = None
) -> Optional[str]:
    """Return the ISO-3166 alpha-2 code for a country name/code, or None.

    Unknown strings are rejected (None) rather than guessed -- a wrong
    country is worse than an absent one.
    """
    if not raw:
        return None
    return load_countries(config_dir).get(str(raw).strip().lower())


def normalize_date(raw: Optional[str]) -> Optional[str]:
    """Return a date as ``"YYYY-MM"``, or None.

    Rules: a bare year ("2021") becomes ``"2021-01"``; an ongoing marker
    ("Present", "Current", blank) becomes None to signal "no end date".
    Anything unparseable also returns None so a caller can tell the value
    apart from a real date.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if text.lower() in _ONGOING_TOKENS:
        return None

    # "July 2021" / "Jul 2021": month name followed by a year.
    name_match = re.match(r"^([A-Za-z]{3,})\.?\s+(\d{4})$", text)
    if name_match:
        month = _MONTH_NAMES.get(name_match.group(1)[:3].lower())
        if month:
            return f"{int(name_match.group(2)):04d}-{month:02d}"
        return None

    month_first = _MONTH_FIRST.match(text)
    if month_first:
        return _format_year_month(
            month_first.group("year"), month_first.group("month")
        )

    year_first = _YEAR_FIRST.match(text)
    if year_first:
        return _format_year_month(
            year_first.group("year"), year_first.group("month")
        )
    return None


def _format_year_month(year: str, month: Optional[str]) -> Optional[str]:
    """Assemble ``"YYYY-MM"`` from parts, defaulting a missing month to 01.

    Returns None if the month is out of range, so callers never emit a
    structurally invalid date such as ``"2021-13"``.
    """
    month_num = int(month) if month else 1
    if not 1 <= month_num <= 12:
        return None
    return f"{int(year):04d}-{month_num:02d}"


def normalize_skill(
    raw: Optional[str], config_dir: Optional[pathlib.Path] = None
) -> tuple[Optional[str], bool]:
    """Map a skill to its canonical name.

    Returns ``(name, was_canonicalized)``. Unknown skills are **never
    dropped**: they pass through trimmed but flagged ``False`` so the caller
    can lower their confidence without losing the data. A blank input
    returns ``(None, False)``.
    """
    if not raw or not str(raw).strip():
        return None, False
    cleaned = str(raw).strip()
    canonical = load_skill_aliases(config_dir).get(cleaned.lower())
    if canonical:
        return canonical, True
    return cleaned, False
