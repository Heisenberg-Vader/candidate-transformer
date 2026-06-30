"""Tests for projecting canonical profiles onto the output contract."""

import pytest

from transformer.config_loader import parse_output_config
from transformer.models import CandidateProfile, Links, Location, Skill
from transformer.projection import ProjectionError, project_profile


def _profile(**overrides):
    base = dict(
        candidate_id="cand_1",
        full_name="Ada Lovelace",
        emails=("ada@x.io", "ada@work.io"),
        phones=("+14155550100",),
        location=Location(city="London", country="GB"),
        links=Links(github="https://github.com/ada"),
        skills=(Skill("python", 0.9, ("ats",)), Skill("go", 0.6, ("github",))),
        overall_confidence=0.84,
    )
    base.update(overrides)
    return CandidateProfile(**base)


def test_rename_and_index_projection():
    config = parse_output_config({
        "fields": [
            {"path": "full_name", "as": "name"},
            {"path": "emails[0]", "as": "primary_email"},
            {"path": "location.country", "as": "country"},
        ]
    })
    record = project_profile(_profile(), config)
    assert record == {
        "name": "Ada Lovelace",
        "primary_email": "ada@x.io",
        "country": "GB",
    }


def test_skills_flatten_to_string_list():
    config = parse_output_config({"fields": [{"path": "skills[].name", "as": "skills"}]})
    record = project_profile(_profile(), config)
    assert record["skills"] == ["python", "go"]


def test_missing_policy_null_emits_none():
    config = parse_output_config({
        "fields": [{"path": "headline", "as": "headline", "missing": "null"}]
    })
    record = project_profile(_profile(headline=None), config)
    assert record == {"headline": None}


def test_missing_policy_omit_drops_key():
    config = parse_output_config({
        "fields": [{"path": "headline", "as": "headline", "missing": "omit"}]
    })
    record = project_profile(_profile(headline=None), config)
    assert "headline" not in record


def test_missing_policy_error_raises():
    config = parse_output_config({
        "fields": [{"path": "headline", "as": "headline", "missing": "error"}]
    })
    with pytest.raises(ProjectionError):
        project_profile(_profile(headline=None), config)


def test_index_out_of_range_is_missing():
    config = parse_output_config({
        "fields": [{"path": "phones[1]", "as": "second_phone", "missing": "omit"}]
    })
    record = project_profile(_profile(), config)
    assert "second_phone" not in record


def test_whole_array_projection():
    config = parse_output_config({"fields": ["emails"]})
    record = project_profile(_profile(), config)
    assert record["emails"] == ["ada@x.io", "ada@work.io"]
