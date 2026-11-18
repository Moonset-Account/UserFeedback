# Local verification: stateful authorization-code exchange tests

Quality-assurance deliverable for
`oauthlib/oauth2/rfc6749/grant_types/authorization_code.py`.  Only test
code, fixtures and a replay entry point were added; the production
implementation is unchanged.

## What was added

| Path | Role |
| --- | --- |
| `tests/oauth2/rfc6749/grant_types/test_authorization_code_stateful.py` | Stateful `RequestValidator` double, exchange harness, case functions `run_case_c01`–`run_case_c04`, and a `unittest` wrapper class. |
| `tools/local_verify.py` | Verification entry point (`--list`, `--case C01..C04`). |

The stateful double replaces the always-true `MagicMock` validator from
`tests/oauth2/rfc6749/grant_types/test_authorization_code.py`: it keeps
registered clients, issued authorization codes (client/redirect_uri/PKCE
bindings, invalidation flag) and saved tokens, and decides every callback
from that state.  Each token-exchange attempt is recorded as an ordered
list of validator callbacks (*stages*), so a failing sequence is pinned to
its **last legitimate stage** and its **first extraneous side effect**
(`save_token` / `invalidate_authorization_code` — never legitimate on a
rejected exchange).

## Actual API (test module)

- `StatefulRequestValidator` — callbacks used by `AuthorizationCodeGrant`:
  `client_authentication_required`, `authenticate_client`,
  `authenticate_client_id`, `validate_grant_type`, `validate_code`,
  `get_code_challenge`, `get_code_challenge_method`, `is_pkce_required`,
  `get_default_redirect_uri`, `confirm_redirect_uri`, `save_token`,
  `invalidate_authorization_code`.  Fixture helpers: `register_client`,
  `issue_code`, `callback_count`.  Observable state: `clients`, `codes`,
  `tokens`, `log` (ordered, secret-free).
- `build_environment(seed)` — validator + grant + `BearerToken` wired to a
  deterministic seeded token generator (`tok-<seed>-access-N`).
- `run_exchange(grant, bearer, validator, **params)` — runs one
  `create_token_response` and returns `outcome`, `error`,
  `error_description`, `stages`, `last_valid_stage`,
  `first_extra_side_effect`, `token`.
- `analyze_entries(entries, outcome)` — the stage/side-effect model.
- `run_case_c01()` … `run_case_c04()` — return
  `{seed, assertions, failures, summary}`.

## Commands

```bash
# list cases
.venv/bin/python tools/local_verify.py --list

# run one case (C01/C02/C03/C04) or all
.venv/bin/python tools/local_verify.py --case C01
.venv/bin/python tools/local_verify.py

# same assertions through unittest
.venv/bin/python -m unittest tests.oauth2.rfc6749.grant_types.test_authorization_code_stateful -v
```

Each case prints one JSON record with `case`, `assertions`, `passed`,
`failed`, `skipped`, `seed` and a `summary` of attempt digests.  Exit
status is non-zero on any failed assertion, missing case entry, raised
exception, zero assertions, or skips.  Output contains only synthetic
fixture identifiers (client ids, code labels, token ids, stage names) —
no secrets or raw sensitive samples.

## Replayable samples

All fixtures are synthetic and deterministic; rerunning a case reproduces
the exact token sequence (asserted in C04 by running the scripted sequence
twice with seed `C04SEED`).

- Clients: `client-alice` (confidential, secret held by the validator,
  redirect `https://alice.example/cb`), `client-bob` (public, PKCE
  required, redirect `https://bob.example/cb`).
- C01: 7 attempts — plain success; wrong secret → `invalid_client` stops
  at `authenticate_client`; foreign code → `invalid_grant` at
  `validate_code`; redirect mismatch → `invalid_request` at
  `confirm_redirect_uri`; PKCE plain success; wrong verifier →
  `invalid_grant` at `get_code_challenge_method`; PKCE S256 success.
- C02: same verifier string bound to `alice/plain` and `bob/S256`;
  interleaved redemptions, both cross-client presentations rejected at
  `validate_code`; missing verifier → `invalid_request` ("Code verifier
  required."); PKCE-required client without stored challenge →
  `invalid_request` (no verifier) / `invalid_grant` ("Challenge not
  found", verifier supplied); failed attempt leaves the code redeemable.
- C03: wrong verifier → retry succeeds (`save_token` recorded before
  `invalidate_authorization_code`, each exactly once) → replay denied at
  `validate_code` with no further `save_token`/`invalidate` calls.
- C04: seeded generator yields `tok-C04SEED-access-1/2` for two successes
  and nothing for the scripted auth failure; tokens and invalidation state
  are per-request; analyser self-check flags an injected `save_token`
  after a failed validation.

## Uncovered edges (not asserted here)

- Authorization-endpoint issuance (`create_authorization_response`,
  `validate_authorization_request`, scope/response_type validation);
  codes are injected via the `issue_code` fixture instead.
- `duplicate_params` rejection, default redirect URI fallback
  (`get_default_redirect_uri`), CORS headers, custom pre/post token
  validators, and code/token modifiers.
- Refresh-token rotation (`rotate_refresh_token`) and hybrid flows.
- Unknown `code_challenge_method` (`ServerError`) and the `openid` grant
  type alias.
- Concurrency: attempts are sequential; no race-condition coverage.
- The stateful double models one reasonable validator contract (exact
  redirect_uri match, secret comparison); it does not prove oauthlib
  behaves identically against every real deployment's validator.

## Known pre-existing suite state

`python -m unittest discover -s tests` shows failures/errors in unrelated
modules (`rfc8628` device code, `rfc6749.test_parameters`, several
`clients` tests) caused by missing optional dependencies in this
environment (e.g. `pytest`).  They exist on the pristine tree; this
change adds only the two paths listed above and all
`tests/oauth2/rfc6749/grant_types` tests pass.
