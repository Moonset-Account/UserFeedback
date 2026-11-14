# Requirement → verification map

Each row maps one explicit requirement from the task prompt to the concrete
tests/cases that exercise it. `case_id`s appear verbatim in
`build/verify_evidence.json`. This map is only a pointer: it does **not** claim
anything beyond what those cases actually assert.

| # | Requirement (from prompt) | Implementation | Real tests / cases |
| --- | --- | --- | --- |
| R1 | Show device-returned error descriptions | `diagnostics/render.py` `render_page`; sample `rec-html-angle-brackets` | `CLI-001`, `STRUCT-001`, `TEXT-001` |
| R2 | Angle brackets and quotes must be visible verbatim | `escape_text` + text nodes only | `TEXT-002`, `TEXT-003`, `TextIsPreservedTests.*`, `BOUND-005`, `BOUND-007` |
| R3 | Locally runnable diagnostics page | `app.py` CLI → static HTML; README quick start | `CLI-001`, `HILITE-001`, `BOUND-004` |
| R4 | Data comes from JSON | `diagnostics/model.py` `load_dataset` | `STRUCT-001`, `STRUCT-002`, `DataValidationTests.*`, `BOUND-003` |
| R5 | Group headings | `grouped()` + `<section>`/`<h2>` | `STRUCT-002`, `STRUCT-003`, `test_title_and_group_text_preserved` |
| R6 | Plain text body | `p.summary` | `TEXT-001`, `test_summary_and_details_preserved` |
| R7 | Preformatted detail block | `pre.details`, whitespace preserved | `test_pre_leading_newline_survives`, `TEXT-001` |
| R8 | One optional documentation link | `doc` optional / nullable | `test_doc_null_is_allowed`, `rec-no-doc` in `STRUCT-001`, `REPORT-001` |
| R9 | Content rendered as text, not markup | escaping + CSP; no `innerHTML`-style paths | `test_script_payload_is_inert_text`, `test_attribute_breaking_label_cannot_escape_anchor`, `BOUND-005` |
| R10 | Search hits may highlight | `split_highlights` + `<mark class="hit">` | `HighlightTests.*`, `HILITE-001..004` |
| R11 | Highlighted content keeps original characters | slices from source string; spans from `re.finditer` | `test_highlight_dotted_capital_i_keeps_length`, `HILITE-002`, `HILITE-004`, `test_sample_search_highlights_without_changing_text` |
| R12 | Links only to allowlisted HTTPS hosts | `LinkPolicy.check` | `AllowCases.*`, `RejectCases.*`, `LINK-ALLOWED`, `LINK-HOST` |
| R13 | Validate using the parsed hostname | `urlsplit(...).hostname` + `canonicalize_host` | `test_at_in_fragment_does_not_rescue_evil_host`, `test_evil_suffix_rejected`, `UrlConfusionTests.*` |
| R14 | Reject links containing credentials | `CREDENTIALS` check on `netloc` + parsed fields | `test_userinfo_rejected`, `test_user_only_credential_rejected`, `test_empty_password_credential_rejected`, `test_port_then_at_rejected_as_credentials`, `LINK-CRED`, `LINK-rec-link-userinfo` |
| R15 | Invalid link shows as plain text | `p.doc-blocked` branch | `LINK-rec-link-*` (five), `LINK-VISIBLE`, `test_credentialed_link_is_plain_text_and_record_survives` |
| R16 | Other records still viewable | independent per-record rendering | `LINK-COUNT`, `test_credentialed_link_is_plain_text_and_record_survives`, `STRUCT-001` |
| R17 | Sample set with markup chars, non-Latin, invalid doc URLs | `samples/errors.json` | `TEXT-002`, `TEXT-004`, `LINK-rec-link-*`, `test_sample_*` |
| R18 | Results checkable in page and generated DOM | `--report` re-parses the HTML | `REPORT-001`, `build/report.json`, `test_only_allowlisted_hosts_are_anchored` |
| R19 | Verification rejects zero/all-skipped tests | guard in `run_unit_tests` | `GUARD-001`, `GUARD-002`, `PROVE-001`, `PROVE-002` |
| R20 | Verification fails on real test failure | exit code 6 path | `GUARD-003`, `PROVE-003` |

## Unplanned / not covered

These were **not** part of the original prompt and are not claimed as verified:

* Browser rendering (CSS layout, visual appearance) — only DOM text is asserted.
* DNS/TLS trust: an allowlisted hostname is trusted by name; a compromised DNS
  or CA is out of scope.
* A live HTTP server: the prompt did not require one, so none is started. All
  verification is offline against generated files.
* `mailto:`, relative URLs, and non-HTTP schemes beyond the tested set are
  rejected by the same `scheme_not_https` rule but only spot-tested.
* Very large datasets (performance) are not benchmarked.
