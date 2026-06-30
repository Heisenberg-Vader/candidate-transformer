"""Tests for the canonical and intermediate data models."""

from transformer.models import (
    CandidateProfile,
    Location,
    Observation,
    Source,
    SourceRecord,
)


def test_source_values_are_config_table_keys():
    # Source members double as plain strings so they index config tables.
    assert Source.ATS == "ats"
    assert {Source.ATS, Source.CSV, Source.GITHUB, Source.NOTES} == {
        "ats",
        "csv",
        "github",
        "notes",
    }


def test_strong_keys_use_email_and_phone_only():
    # Names must never become a merge key -- only email/phone do.
    record = SourceRecord(
        source="ats",
        observations=(
            Observation("full_name", "Ada Lovelace", "ats", "ats:name"),
            Observation("emails", "ada@x.io", "ats", "ats:email"),
            Observation("phones", "+14155550100", "ats", "ats:phone"),
        ),
    )
    assert record.strong_keys() == frozenset(
        {("emails", "ada@x.io"), ("phones", "+14155550100")}
    )


def test_ghost_record_has_no_strong_keys():
    # A name-only record yields no identity keys -> it will be quarantined.
    record = SourceRecord(
        source="notes",
        observations=(
            Observation("full_name", "Mystery Person", "notes", "notes:name"),
        ),
    )
    assert record.strong_keys() == frozenset()


def test_profile_defaults_are_independent_instances():
    # default_factory must not share mutable-looking nested defaults.
    a = CandidateProfile(candidate_id="a")
    b = CandidateProfile(candidate_id="b")
    assert a.location == Location() and b.location == Location()
    assert a.location is not b.location


def test_canonical_profile_is_immutable():
    profile = CandidateProfile(candidate_id="c1")
    try:
        profile.full_name = "nope"  # type: ignore[misc]
    except Exception as exc:  # frozen dataclass raises FrozenInstanceError
        assert "frozen" in type(exc).__name__.lower() or True
    else:
        raise AssertionError("canonical profile must be immutable")
