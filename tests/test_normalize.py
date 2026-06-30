"""Tests for the field normalizers.

Each test doubles as a regression guard: an edge case from the design (or a
bug found while building) becomes a named case here.
"""

import pytest

from transformer.normalize import (
    normalize_country,
    normalize_date,
    normalize_email,
    normalize_phone,
    normalize_skill,
)


# --- phones ---------------------------------------------------------------


def test_phone_us_national_with_region_becomes_e164():
    assert normalize_phone("(415) 555-0100", "US") == "+14155550100"


def test_phone_already_e164_passes_through():
    assert normalize_phone("+91 80 4710 2000", None) == "+918047102000"


def test_phone_two_formats_converge_to_same_e164():
    # Design edge case 2: differently-formatted numbers normalize to one key.
    a = normalize_phone("+1 415-555-0100", None)
    b = normalize_phone("4155550100", "US")
    assert a == b == "+14155550100"


def test_phone_structurally_invalid_is_rejected_not_coerced():
    # "123" is not a real number; we return None rather than fake an E.164.
    assert normalize_phone("123", "US") is None


def test_phone_blank_is_none():
    assert normalize_phone("", "US") is None
    assert normalize_phone(None) is None


# --- emails ---------------------------------------------------------------


def test_email_is_lowercased_and_trimmed():
    assert normalize_email("  Ada@Example.IO ") == "ada@example.io"


@pytest.mark.parametrize("bad", ["not-an-email", "a@b", "@x.io", "a@@b.io"])
def test_email_malformed_rejected(bad):
    assert normalize_email(bad) is None


# --- countries ------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,code",
    [("United States", "US"), ("usa", "US"), ("India", "IN"),
     ("uk", "GB"), ("Germany", "DE")],
)
def test_country_name_to_alpha2(raw, code):
    assert normalize_country(raw) == code


def test_country_unknown_is_rejected():
    assert normalize_country("Atlantis") is None


# --- dates ----------------------------------------------------------------


def test_date_bare_year_defaults_to_january():
    assert normalize_date("2021") == "2021-01"


@pytest.mark.parametrize(
    "raw,out",
    [("2021-07", "2021-07"), ("2021/7", "2021-07"),
     ("07/2021", "2021-07"), ("July 2021", "2021-07"),
     ("Jan 2020", "2020-01")],
)
def test_date_various_formats(raw, out):
    assert normalize_date(raw) == out


@pytest.mark.parametrize("present", ["Present", "current", "", "ongoing"])
def test_date_ongoing_is_none(present):
    # "Present" means ongoing -> None (no end date), per the design.
    assert normalize_date(present) is None


def test_date_invalid_month_rejected():
    assert normalize_date("2021-13") is None


# --- skills ---------------------------------------------------------------


def test_skill_alias_maps_to_canonical():
    assert normalize_skill("JS") == ("javascript", True)
    assert normalize_skill("k8s") == ("kubernetes", True)


def test_skill_unknown_kept_raw_and_flagged():
    # Unknown skills are kept (never dropped) but flagged as not canonical.
    name, canonical = normalize_skill("Quantum Basket Weaving")
    assert name == "Quantum Basket Weaving"
    assert canonical is False


def test_skill_blank_is_none():
    assert normalize_skill("   ") == (None, False)
