"""Tests for the source parsers, including fault-isolation behavior."""

import json

from transformer.parsers import (
    extract_github_username,
    parse_ats,
    parse_csv,
    parse_github,
    parse_notes,
)


def _values(record, field_name):
    return sorted(record.values_for(field_name))


# --- ATS JSON -------------------------------------------------------------


def test_ats_parses_core_fields_and_normalizes():
    raw = json.dumps([
        {
            "name": "Ada Lovelace",
            "emails": ["Ada@Example.IO"],
            "phones": ["(415) 555-0100"],
            "location": {"city": "San Francisco", "country": "USA"},
            "skills": ["JS", "Quantum Weaving"],
        }
    ])
    result = parse_ats(raw)
    assert len(result.records) == 1
    rec = result.records[0]
    assert rec.values_for("full_name") == ("Ada Lovelace",)
    assert rec.values_for("emails") == ("ada@example.io",)
    # National-format number parsed against the US region hint -> E.164.
    assert rec.values_for("phones") == ("+14155550100",)
    assert rec.values_for("location.country") == ("US",)
    assert "javascript" in rec.values_for("skills")


def test_ats_invalid_json_yields_warning_not_crash():
    result = parse_ats("{not json")
    assert result.records == ()
    assert any("invalid JSON" in w for w in result.warnings)


def test_ats_one_bad_record_isolated():
    # Second object is malformed (skills is a non-iterable int); the first
    # must still come through, with a warning for the bad one.
    raw = json.dumps([
        {"name": "Good", "emails": ["good@x.io"]},
        {"name": "Bad", "skills": 5, "links": 7},
    ])
    result = parse_ats(raw)
    names = [r.values_for("full_name") for r in result.records]
    assert ("Good",) in names


# --- CSV ------------------------------------------------------------------


def test_csv_aliased_headers_and_multi_value_skills():
    raw = (
        "candidate_name,e-mail,mobile,country,skills\n"
        "Ada Lovelace,ada@x.io,+14155550100,USA,JS;Python;k8s\n"
    )
    result = parse_csv(raw)
    rec = result.records[0]
    assert rec.values_for("full_name") == ("Ada Lovelace",)
    assert rec.values_for("location.country") == ("US",)
    assert set(rec.values_for("skills")) == {"javascript", "python", "kubernetes"}


def test_csv_bad_phone_is_rejected_with_warning():
    raw = "name,phone\nAda,123\n"
    result = parse_csv(raw)
    # Record still exists (name observed); phone dropped + warned.
    assert result.records[0].values_for("phones") == ()
    assert any("invalid phone" in w for w in result.warnings)


# --- Notes ----------------------------------------------------------------


def test_notes_extracts_email_phone_github():
    text = (
        "Name: Grace Hopper\n"
        "Reach her at grace@navy.mil or +1 415-555-0199.\n"
        "Profile: https://github.com/GraceH\n"
        "Skills: COBOL, python\n"
    )
    result = parse_notes(text)
    rec = result.records[0]
    assert rec.values_for("full_name") == ("Grace Hopper",)
    assert rec.values_for("emails") == ("grace@navy.mil",)
    assert rec.values_for("phones") == ("+14155550199",)
    assert rec.values_for("links.github") == ("https://github.com/GraceH",)
    assert "python" in rec.values_for("skills")


def test_notes_name_only_is_ghost_with_no_strong_keys():
    # Ghost candidate (design edge case 1): name but no email/phone.
    result = parse_notes("Name: Mystery Candidate\nNice to meet them.\n")
    rec = result.records[0]
    assert rec.strong_keys() == frozenset()


# --- GitHub ---------------------------------------------------------------


def test_github_profile_yields_links_and_skills_no_strong_key():
    profiles = [{
        "login": "ada",
        "html_url": "https://github.com/ada",
        "blog": "https://ada.dev",
        "bio": "Systems engineer",
        "languages": ["Python", "Go"],
    }]
    rec = parse_github(profiles).records[0]
    assert rec.values_for("links.github") == ("https://github.com/ada",)
    assert rec.values_for("links.portfolio") == ("https://ada.dev",)
    assert set(rec.values_for("skills")) == {"python", "go"}
    # Enrichment-only: GitHub never contributes a strong identity key.
    assert rec.strong_keys() == frozenset()


def test_extract_github_username_is_case_insensitive():
    assert extract_github_username("https://github.com/GraceH") == "graceh"
    assert extract_github_username("https://example.com/x") is None
    assert extract_github_username(None) is None
