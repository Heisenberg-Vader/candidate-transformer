"""Tests for fail-fast output-config validation."""

import pytest

from transformer.config_loader import ConfigError, parse_output_config


def test_valid_config_parses_with_aliases_and_kinds():
    config = parse_output_config({
        "missing": "omit",
        "fields": [
            "full_name",
            {"path": "emails[0]", "as": "primary_email"},
            {"path": "skills[].name", "as": "skills"},
            {"path": "location.country", "as": "country", "missing": "null"},
        ],
    })
    by_alias = {f.alias: f for f in config.fields}
    assert by_alias["primary_email"].kind == "scalar"
    assert by_alias["skills"].kind == "list"          # [] projection -> list
    assert by_alias["full_name"].missing == "omit"    # inherits default
    assert by_alias["country"].missing == "null"      # per-field override


def test_unknown_field_is_rejected_before_run():
    # Design edge case 3: an unknown field is caught at config time.
    with pytest.raises(ConfigError) as exc:
        parse_output_config({"fields": ["salary_expectation"]})
    assert "unknown field" in str(exc.value)


def test_indexing_a_non_array_is_rejected():
    with pytest.raises(ConfigError) as exc:
        parse_output_config({"fields": ["full_name[0]"]})
    assert "not an array" in str(exc.value)


def test_descending_into_scalar_is_rejected():
    with pytest.raises(ConfigError):
        parse_output_config({"fields": ["full_name.first"]})


def test_duplicate_alias_is_rejected():
    with pytest.raises(ConfigError) as exc:
        parse_output_config({
            "fields": [
                {"path": "emails[0]", "as": "contact"},
                {"path": "phones[0]", "as": "contact"},
            ]
        })
    assert "duplicate" in str(exc.value)


def test_bad_missing_policy_is_rejected():
    with pytest.raises(ConfigError):
        parse_output_config({"fields": ["full_name"], "missing": "explode"})


def test_empty_fields_is_rejected():
    with pytest.raises(ConfigError):
        parse_output_config({"fields": []})


def test_nested_projection_path_kind():
    # skills[].confidence is still a list (one confidence per skill).
    config = parse_output_config(
        {"fields": [{"path": "skills[].confidence", "as": "confs"}]}
    )
    assert config.fields[0].kind == "list"
