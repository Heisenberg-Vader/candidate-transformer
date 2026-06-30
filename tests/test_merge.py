"""Tests for entity resolution, conflict resolution, and confidence."""

from transformer.merge import ConfidenceModel, merge_records
from transformer.config_files import load_trust
from transformer.models import Observation, SourceRecord


def _rec(source, **fields):
    """Build a SourceRecord from field=value(s); lists become many obs."""
    obs = []
    for field_name, value in fields.items():
        values = value if isinstance(value, list) else [value]
        for item in values:
            normalized = not str(item).startswith("RAW:")
            clean = str(item).replace("RAW:", "")
            obs.append(Observation(field_name, clean, source, f"{source}:x", normalized))
    return SourceRecord(source, tuple(obs))


# --- entity resolution ----------------------------------------------------


def test_records_sharing_email_merge_into_one_profile():
    ats = _rec("ats", full_name="Ada Lovelace", emails="ada@x.io")
    csv = _rec("csv", full_name="Ada L.", emails="ada@x.io", phones="+14155550100")
    result = merge_records([ats, csv])
    assert len(result.profiles) == 1
    profile = result.profiles[0]
    assert "ada@x.io" in profile.emails
    assert "+14155550100" in profile.phones


def test_records_without_shared_key_stay_separate():
    a = _rec("ats", full_name="Ada", emails="ada@x.io")
    b = _rec("ats", full_name="Bob", emails="bob@x.io")
    result = merge_records([a, b])
    assert len(result.profiles) == 2


def test_ghost_record_is_quarantined_not_merged():
    # Design edge case 1: name only, no strong key -> quarantined.
    ghost = _rec("notes", full_name="Mystery Person")
    real = _rec("ats", full_name="Ada", emails="ada@x.io")
    result = merge_records([ghost, real])
    assert len(result.profiles) == 1
    assert len(result.quarantined) == 1
    assert "strong identity key" in result.quarantined[0].reason


def test_phone_only_link_still_merges():
    # Two records sharing only a phone are the same person.
    a = _rec("ats", full_name="Ada", phones="+14155550100", emails="a@x.io")
    b = _rec("csv", full_name="Ada Byron", phones="+14155550100", emails="b@x.io")
    result = merge_records([a, b])
    assert len(result.profiles) == 1
    assert set(result.profiles[0].emails) == {"a@x.io", "b@x.io"}


# --- conflict resolution --------------------------------------------------


def test_higher_trust_source_wins_a_scalar_conflict():
    # ATS (trust 1.0) beats Notes (0.4) for the name when each stands alone.
    ats = _rec("ats", full_name="Ada Lovelace", emails="ada@x.io")
    notes = _rec("notes", full_name="Ada L", emails="ada@x.io")
    result = merge_records([ats, notes])
    assert result.profiles[0].full_name == "Ada Lovelace"


def test_corroboration_overrides_single_higher_source():
    # CSV + Notes agree on "Ada Byron"; ATS alone says "Ada Lovelace".
    # Two agreeing sources outrank the lone higher-trust source.
    ats = _rec("ats", full_name="Ada Lovelace", emails="ada@x.io")
    csv = _rec("csv", full_name="Ada Byron", emails="ada@x.io")
    notes = _rec("notes", full_name="Ada Byron", emails="ada@x.io")
    result = merge_records([ats, csv, notes])
    assert result.profiles[0].full_name == "Ada Byron"


def test_losing_value_is_kept_in_provenance():
    ats = _rec("ats", full_name="Ada Lovelace", emails="ada@x.io")
    notes = _rec("notes", full_name="Ada L", emails="ada@x.io")
    result = merge_records([ats, notes])
    losers = [
        p for p in result.profiles[0].provenance
        if p.field == "full_name" and not p.won
    ]
    assert any(p.value == "Ada L" for p in losers)


def test_arrays_are_combined_not_contested():
    a = _rec("ats", emails="a@x.io", phones="+14155550100")
    b = _rec("csv", emails="b@x.io", phones="+14155550100")
    result = merge_records([a, b])
    assert set(result.profiles[0].emails) == {"a@x.io", "b@x.io"}


# --- confidence -----------------------------------------------------------


def test_corroborated_value_scores_higher_than_solo():
    model = ConfidenceModel(load_trust())
    solo = model.field_confidence(0.8, 1, True)
    corroborated = model.field_confidence(0.8, 2, True)
    assert corroborated > solo


def test_confidence_never_exceeds_one():
    model = ConfidenceModel(load_trust())
    assert model.field_confidence(1.0, 3, True) <= 1.0


def test_missing_identity_field_drags_overall_down():
    model = ConfidenceModel(load_trust())
    full = model.overall_confidence(
        {"full_name": 1.0, "emails": 1.0, "phones": 1.0}
    )
    no_phone = model.overall_confidence({"full_name": 1.0, "emails": 1.0})
    # phones is required, so its absence (zero, in denominator) lowers score.
    assert no_phone < full


# --- GitHub enrichment ----------------------------------------------------


def test_github_enriches_matched_candidate_only():
    # Candidate's ATS record carries a github link; the github record then
    # contributes skills -- without any identity matching.
    ats = _rec(
        "ats", full_name="Ada", emails="ada@x.io",
    )
    ats = SourceRecord("ats", ats.observations + (
        Observation("links.github", "https://github.com/ada", "ats", "ats:gh"),
    ))
    github = _rec("github", **{"links.github": "https://github.com/ada"})
    github = SourceRecord("github", github.observations + (
        Observation("skills", "rust", "github", "github:skills"),
    ))
    result = merge_records([ats], github_records=[github])
    skill_names = {s.name for s in result.profiles[0].skills}
    assert "rust" in skill_names


def test_github_without_matching_link_does_not_attach():
    ats = _rec("ats", full_name="Ada", emails="ada@x.io")
    github = _rec("github", **{"links.github": "https://github.com/someoneelse"})
    github = SourceRecord("github", github.observations + (
        Observation("skills", "rust", "github", "github:skills"),
    ))
    result = merge_records([ats], github_records=[github])
    assert result.profiles[0].skills == ()


def test_determinism_same_inputs_same_ids():
    a = _rec("ats", full_name="Ada", emails="ada@x.io")
    b = _rec("ats", full_name="Ada", emails="ada@x.io")
    assert merge_records([a]).profiles[0].candidate_id == \
        merge_records([b]).profiles[0].candidate_id
