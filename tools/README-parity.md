# Vulnerability-handling JSON/XML parity verification

These tests check that vulnerability **handling statements**
(`Vulnerability.analysis` and `Vulnerability.affects` in
`cyclonedx/model/vulnerability.py`, enums in
`cyclonedx/model/impact_analysis.py`) carry the same business meaning when
rendered through the two output modules
(`cyclonedx/output/json.py`, `cyclonedx/output/xml.py`) for the CycloneDX
schema versions that support the fields under test: **1.5 and 1.6**.

The work is test-only: no production implementation was modified.

## Layout

| Path | Role |
| --- | --- |
| `tests/parity/fixtures.py` | Deterministic synthetic model fixtures (explicit, defaulted and partial samples) and pure-value model snapshots |
| `tests/parity/projection.py` | Two **independent** parsers, one per wire format, projecting rendered documents into one neutral business structure |
| `tests/parity/diffing.py` | Field-level semantic diffs; every entry names the concrete model field |
| `tests/parity/checker.py` | Recording assertion harness (real assertion counts; zero assertions cannot pass) |
| `tests/parity/evidence.py` | Failure evidence writer: minimal model, both rendered documents, diffs and summary |
| `tests/parity/engine.py` | Rendering/assertion engine built on the public `Json`/`Xml` outputters |
| `tests/parity/cases.py` | The four replayable cases `C01`–`C04` |
| `tests/test_vulnerability_handling_parity.py` | `unittest` entry point mapping the cases to the standard test runner |
| `tools/local_verify.py` | Local verification CLI wrapper |

## Commands

Run from the repository root with the prepared virtual environment
(`.venv` on `PATH`):

```bash
# list the available cases
python tools/local_verify.py --list

# run one case
python tools/local_verify.py --case C01

# run every case
python tools/local_verify.py --case all

# same cases through the project's standard test runner
python -m unittest tests.test_vulnerability_handling_parity -v
```

Exit codes from `tools/local_verify.py`:

| Code | Meaning |
| --- | --- |
| 0 | Every selected case had at least one assertion and none failed |
| 1 | One or more recorded assertions failed |
| 2 | No case selected, or a case produced zero effective assertions |
| 3 | Unknown / unimplemented case id |
| 4 | Case raised `NotImplementedError` |
| 5 | Case raised an unexpected exception |

Each case prints one JSON record with `case`, the actual assertion counts
(`total/passed/failed/skipped`), the fixture samples or seeds used, and an
`expected_observed` summary for every divergence, including the model field
path. No credentials, original sensitive samples or private account data are
involved - all fixtures are synthetic.

## What each case covers

- **C01** — fully populated handling sample (state, justification, response
  set, `first_issued`/`last_updated`, several `affects` targets with exact
  versions and `vers` ranges). JSON and XML are each parsed by their own
  projector, checked against the same semantic contract, and only then
  compared to each other for 1.5 and 1.6. Neither format is the truth for the
  other.
- **C02** — timestamps use different explicit offsets (`+05:30`, `-03:00`);
  projectors normalize to UTC and compare the instant, while asserting the
  raw offsets survive in both documents. Responses use set semantics
  (order/duplicates irrelevant), version-range boundary strings (including an
  open `>=4.0.0` bound and an open-lower `<9.0.0` bound) are retained
  verbatim with their affected status. Defaulted-only and partial (explicit
  beside defaulted) models are separate samples.
- **C03** — interleaved JSON/XML exports across 1.5/1.6, reversed insertion
  order and duplicate equivalent entries (a response enum and an equivalent
  `BomTargetVersionRange`). Semantic projections stay identical, repeated
  exports are deterministic, and a pure-value snapshot proves the input model
  keeps its original handling values and restored `bom-ref` after exporting.
- **C04** — failure-path drill on deliberately mutated neutral views
  (production code untouched). Verifies that divergences are reported on the
  exact model fields (`Vulnerability.analysis.state`,
  `Vulnerability.analysis.responses`, `Vulnerability.affects…`) and that
  evidence retains the minimal model plus **both** raw rendered documents.

## Replayable samples / seeds

All inputs are constructed in code from fixed values
(`tests/parity/fixtures.py`): bom-refs `parity-vuln-explicit`,
`parity-vuln-default`, `parity-vuln-partial`; components
`parity-component-alpha` / `parity-component-beta`; fixed datetimes
`2026-01-02T03:04:05.678+05:30` and `2026-06-07T08:09:10-03:00`; range seeds
`vers:py/>=2.0.0|<3.0.0`, `vers:py/>=4.0.0`, `vers:py/<9.0.0`. There is no
randomness in the model content; the library's own `BomRefDiscriminator`
only touches empty/duplicate bom-refs, which the fixtures never use.

## Failure evidence

When a case fails, `tests/_data/parity-evidence/<CASE>/` receives:

- `model.min.json` — minimal reproduction of the input handling model;
- `rendered-<version>.json` and `rendered-<version>.xml` — both documents
  exactly as rendered (no cross-format canonicalization);
- `diffs.json` — field-level divergences with `expected`/`observed`;
- `summary.json` — case id, render sizes, affected model fields and failures.

The directory is wiped and recreated per run and is git-ignored; only
synthetic data is ever written. `C04` always writes a `C04-drill` bundle to
demonstrate the evidence machinery.

## Boundaries not covered

- Only schema versions **1.5 and 1.6** are exercised, because
  `first_issued`/`last_updated` are view-gated to `>=1.5`; 1.4 and older are
  out of scope by design.
- `vers` range strings are compared verbatim as boundary information; the
  library does not implement the purl version-range spec, so the tests do not
  parse range math or evaluate version ordering inside a range.
- Naive (timezone-less) datetimes are intentionally not used as fixtures:
  cross-format equality of an ambiguous local instant cannot be asserted
  honestly; `normalize_timestamp` raises on such input instead.
- Schema/XSD validation of the rendered documents is not re-implemented here
  (the repository's `test_output_json.py` / `test_output_xml.py` already cover
  schema validation); these cases focus on semantic parity of the handling
  fields.
- The wrapper records and drives the real assertions but is not an
  independent proof of correctness: audits should still read the model,
  output and projection code and the retained evidence directly, and treat
  anything not demonstrated here as unknown.
