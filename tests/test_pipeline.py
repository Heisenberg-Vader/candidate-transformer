"""End-to-end pipeline tests over the bundled sample inputs."""

import json
import pathlib

from transformer.config_loader import load_output_config
from transformer.pipeline import PipelineInputs, result_to_dict, run

SAMPLES = pathlib.Path(__file__).resolve().parent.parent / "sample_inputs"
CONFIG = pathlib.Path(__file__).resolve().parent.parent / "config"


def _load_inputs() -> PipelineInputs:
    return PipelineInputs(
        ats_json=(SAMPLES / "ats.json").read_text(encoding="utf-8"),
        csv_text=(SAMPLES / "recruiter.csv").read_text(encoding="utf-8"),
        notes_texts=(
            (SAMPLES / "notes.txt").read_text(encoding="utf-8"),
            (SAMPLES / "ghost_note.txt").read_text(encoding="utf-8"),
        ),
        github_profiles=tuple(
            json.loads((SAMPLES / "github.json").read_text(encoding="utf-8"))
        ),
        default_region="GB",
    )


def test_pipeline_resolves_expected_candidates():
    # Ada (ATS+CSV+GitHub), Grace (ATS+CSV), Alan (CSV+Notes+GitHub) merge
    # into three profiles; the walk-in is quarantined.
    result = run(_load_inputs())
    by_name = {p.full_name: p for p in result.profiles}
    assert set(by_name) == {"Ada Lovelace", "Grace Hopper", "Alan Turing"}
    assert len(result.profiles) == 3
    assert len(result.quarantined) == 1
    assert "Unknown Walk-in" in str(result.quarantined[0].record.observations)


def test_pipeline_merges_across_sources_by_strong_key():
    result = run(_load_inputs())
    ada = next(p for p in result.profiles if p.full_name == "Ada Lovelace")
    # Email shared across ATS+CSV merges them; GitHub enriches via the link.
    assert "ada.lovelace@analytical.io" in ada.emails
    skill_names = {s.name for s in ada.skills}
    assert "rust" not in skill_names  # GitHub "Rust" -> canonical "rust"? no alias
    assert "machine learning" in skill_names
    # GitHub-only skill (Rust) is enrichment-attached via links.github.
    assert "Rust" in skill_names or "rust" in skill_names


def test_pipeline_invalid_phone_warns_but_continues():
    # Grace's CSV phone is "not-a-number": dropped + warned, profile survives.
    result = run(_load_inputs())
    assert any("invalid phone" in w for w in result.warnings)
    grace = next(p for p in result.profiles if p.full_name == "Grace Hopper")
    assert grace.phones == ("+12025550199",)  # the valid ATS phone remains


def test_pipeline_projection_matches_config_contract():
    config = load_output_config(CONFIG / "output.example.yaml")
    result = run(_load_inputs(), output_config=config)
    record = result.output_records[0]
    # Output keys are the config's aliases, not canonical names.
    assert set(record).issubset(
        {"id", "name", "primary_email", "primary_phone",
         "country", "github", "skills", "confidence"}
    )
    assert isinstance(record["skills"], list)
    assert "primary_email" in record  # null policy keeps the key present


def test_result_dict_is_json_serializable():
    result = run(_load_inputs())
    # Must round-trip through JSON without custom encoders.
    json.dumps(result_to_dict(result))
