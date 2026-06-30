# Multi-Source Candidate Data Transformer
Rule-driven deterministic pipeline that merges candidate information
from multiple heterogenous sources – ATS JSON, recruiter CSV, free text, and
GitHub – into one standard profile for each candidate with **field-level confidence**
and **complete provenance**. No large language models, no persistency: all is done
in memory.

**Coverage of sources (2 structured + 2 unstructured):** ATS JSON and recruiter CSV
(structured); recruiter notes in `.txt` format and GitHub profile data (unstructured).
Assignment requires at least one source from each category – this exceeds minimum.

## Requirements

- Python 3.12+
- No network access required (GitHub data is a saved payload — see Assumptions)
- Dependencies: `phonenumbers`, `PyYAML`, `pytest` (installed below)

## Setup

```bash
git clone https://github.com/Heisenberg-Vader/candidate-transformer.git
cd candidate-transformer
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The commands below call the virtualenv interpreter by path
(`.venv/bin/python`), so there is no need to `activate` the environment first.

## Run

**Default schema** — every field, with provenance and confidence, run
end-to-end on the sample inputs and emitting schema-valid JSON. The `--notes`
flag is repeatable; pass it once per notes file:

```bash
.venv/bin/python -m transformer.cli \
    --ats sample_inputs/ats.json \
    --csv sample_inputs/recruiter.csv \
    --notes sample_inputs/notes.txt \
    --notes sample_inputs/ghost_note.txt \
    --github sample_inputs/github.json \
    --region GB \
    --out outputs/default.json
```

**Custom config** — a runtime config selects a subset of fields and renames
them, producing a *different* output schema from the *same* engine:

```bash
.venv/bin/python -m transformer.cli \
    --ats sample_inputs/ats.json \
    --csv sample_inputs/recruiter.csv \
    --config config/output.example.yaml \
    --out outputs/custom.json
```

Both produced outputs are committed under `outputs/`, so they can be inspected
without running anything.

### Expected result

The default run writes `outputs/default.json` containing the merged candidate
profiles, each with per-field provenance and confidence. The ghost-candidate
note (no email or phone) is quarantined rather than merged, and is reported
separately. The custom run writes `outputs/custom.json` with the reduced,
renamed schema defined by `config/output.example.yaml`.

## Run the tests

```bash
.venv/bin/python -m pytest -q
```

The suite covers normalization, entity resolution, conflict resolution, and
projection, and includes edge-case tests — notably the ghost-candidate
quarantine and the malformed-source case (graceful degradation). All 78 tests
should pass.

## Pipeline

<img width="739" height="202" alt="pipeline drawio" src="https://github.com/user-attachments/assets/f0e9a524-996f-4b50-b40a-89caf64fd730" />

| Stage | Module | Responsibility |
| --- | --- | --- |
| ingest + normalize | `parsers.py` | Parse each source in isolation; normalize on the way in. A malformed record is logged as a warning and skipped — the run continues. |
| normalize (rules) | `normalize.py` | Phones → E.164 (invalid rejected, never coerced); countries → ISO-3166 alpha-2; dates → `YYYY-MM` (bare year → `-01`, "Present" → null); skills → canonical names (unknowns kept, never dropped). |
| resolve + merge + score | `merge.py` | Strong-key entity resolution, field-level conflict resolution, confidence model, GitHub enrichment. |
| project + validate | `projection.py`, `config_loader.py` | Apply the runtime output config; validate the result against the schema the config implies. |
| orchestration | `pipeline.py`, `cli.py` | Wire the stages together; command-line entry point. |

The canonical schema and all intermediate types live in `models.py` as
immutable (frozen) dataclasses — no pydantic, so every invariant is explicit.

## Key design decisions

**Strong-signal-only matching.** Records merge only when they share an email or
phone. Names never trigger a merge (a name is a weak corroborator). A record
with no strong key — a "ghost" candidate — is **quarantined**, not merged, so it
can never be glued onto someone else. The bias is deliberate: under-merging
(a duplicate profile) is recoverable; over-merging (two people fused) corrupts
real data irreversibly.

**Conflict resolution with corroboration override.** Within a cluster each field
is resolved independently. The default winner follows the source-trust hierarchy
(**ATS > CSV > GitHub > Notes**), but agreement is evidence: a value seen in two
sources can outrank a lone higher-trust value. Array fields (emails, phones,
skills) are combined, not contested. **Losing values are never dropped
silently** — they are written to the provenance log.

**Dynamic confidence.** Each field's confidence is
`source_trust × corroboration_bonus × normalization_success`, capped at 1.0.
`overall_confidence` is a weighted average that heavily weights identity fields
(name, email, phone), so baseline identity outranks peripheral skills.

**GitHub is enrichment only.** A GitHub profile contributes links and skills to
an already-resolved candidate, matched by a `links.github` username the
candidate's own sources provided. No entity matching is performed on GitHub
data, and it carries no email/phone, so it can neither form nor break a match.

**Config-driven, validated output.** A JSON/YAML config selects a subset of
fields, renames each from its canonical path (`emails[0]` → `primary_email`,
`skills[].name` → a flat `string[]`), and picks a missing-value policy
(`null | omit | error`). The config is validated **before** the run, so an
unknown field or malformed path fails fast and explainably; each produced record
is then validated against the schema the config implies.

Everything tunable — source-trust weights, corroboration bonuses, skill aliases,
country codes — lives in `config/*.yaml` and is read at runtime; none of it is
hard-coded in Python.

## Configuration files

| File | Purpose |
| --- | --- |
| `config/trust.yaml` | Source-trust weights, corroboration bonuses, normalization factors, overall-confidence weights. |
| `config/skill_aliases.yaml` | Canonical skill name → aliases. |
| `config/countries.yaml` | Country name/code → ISO-3166 alpha-2. |
| `config/output.example.yaml` | Example runtime output contract. |

## Edge cases handled

1. **Ghost candidate** (name, no email/phone) → quarantined, surfaced with a
   reason rather than risking a false merge.
2. **Format divergence** — `(415) 555-0100` and `+1 415-555-0100` normalize to
   one E.164 key; structurally invalid numbers are rejected.
3. **Unknown config field** → caught by config validation before the run, with a
   clear message.
4. **Malformed source** → isolated inside its own parser, logged as a warning;
   the run continues on the remaining sources.

## Assumptions

- One recruiter-notes `.txt` file describes one candidate.
- GitHub input is a saved public-API JSON payload, not a live fetch — keeps the
  run offline, deterministic, and free of OAuth/rate-limit concerns.
- A bare year in a date normalizes to January (`YYYY-01`), recorded rather than
  dropped.
- Source-trust order (ATS > CSV > GitHub > Notes) reflects structured-vs-noisy
  origin, not data freshness.

## Deliberately out of scope (time-boxed)

- LLM-based extraction (deterministic rules instead, for explainability and
reproducibility)
- Live OAuth (GitHub via saved public payloads only)
- Persistent storage (in-memory only)
- Fuzzy/ML name dedupe (name stays a weak signal).

## Authored by: 
Hussain Haidary | hussain.haidary452@gmail.com | [LinkedIn](https://www.linkedin.com/in/hussainh0211/)
