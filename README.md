# 设备错误诊断页 / Device Error Diagnostics

A locally runnable diagnostic page for device-returned error descriptions.
Data comes from JSON; the page renders a group heading per group, plain-text
summaries, preformatted detail blocks and one optional documentation link per
record.

Everything is rendered as **text**. Angle brackets, quotes and non-Latin
scripts appear exactly as they appear in the JSON. Search only wraps matching
substrings in `<mark>`; the rendered characters are unchanged. Documentation
links are only emitted as `<a>` for `https:` URLs whose **parsed hostname** is
on the configured allowlist; URLs carrying credentials, raw control characters
or any other scheme are shown as plain text, and the rest of the page —
including neighbouring records — stays visible.

Python 3 standard library only. No pip, virtualenv, pytest or network access.

## Quick start

From this directory, with `/usr/local/bin/python3`:

```sh
# 1. Generate the page from the shipped sample (10 records, mixed scripts,
#    markup-like text and invalid documentation URLs).
/usr/local/bin/python3 app.py \
  --data samples/errors.json \
  --allowlist samples/allowlist.json \
  --out build/diagnostics.html \
  --report build/report.json

# 2. Open build/diagnostics.html in a browser.

# 3. Optional: highlight every "E42" occurrence and regenerate.
/usr/local/bin/python3 app.py \
  --data samples/errors.json --allowlist samples/allowlist.json \
  --query E42 --out build/diagnostics-highlight.html

# 4. Run the whole verification (unit tests + sample/boundary assertions).
/usr/local/bin/python3 tools/local_verify.py
```

`tools/local_verify.py` prints one line per check, writes
`build/verify_evidence.json`, and exits non-zero on any failure. It takes under
a second and is hard-capped at 75 seconds by an alarm.

## CLI

`app.py` options:

| Flag | Required | Meaning |
| --- | --- | --- |
| `--data` | yes | records JSON file |
| `--allowlist` | yes | documentation-host allowlist JSON |
| `--out` | no (`build/diagnostics.html`) | HTML output path |
| `--report` | no | write a JSON report built by re-parsing the generated DOM |
| `--query` | no | highlight text; case-insensitive, literal |

Exit codes: `0` success, `2` invalid data / allowlist / arguments, `1`
unexpected error.

## Input format

### Records (`--data`)

```json
{
  "title": "设备错误诊断 / Device Error Diagnostics",
  "records": [
    {
      "id": "rec-html-angle-brackets",
      "group": "渲染安全 / Rendering Safety",
      "summary": "设备返回：<error code=\"E42\">缓冲区溢出 & 重试</error>；请检查 <config> 与 'quotes'。",
      "details": "<response status=\"500\">\n  <message>unsafe <b>markup</b></message>\n</response>",
      "doc": {
        "label": "错误码说明 <E42> & 处理建议",
        "url": "https://docs.example.com/errors/E42?ref=diagnostics&lang=zh"
      }
    }
  ]
}
```

* `title` — optional string, defaults to `设备错误诊断 / Device Error Diagnostics`.
* `records` — required array, rendered in array order and bucketed by `group`
  in first-appearance order.
* `id` — required, non-empty, unique. Used for element ids
  (`record-<id>`) and the DOM report.
* `group` — required, non-empty group heading.
* `summary` — required string, rendered in a `<p>`. May be empty.
* `details` — optional string or `null`, rendered in a `<pre>` with preserved
  whitespace.
* `doc` — optional object or `null`. `url` is required inside it; `label`
  defaults to the URL text when absent or empty.
* Text fields have `CRLF`/`CR` normalised to `LF`. A `NUL` character is
  rejected because HTML text cannot represent it faithfully.

### Allowlist (`--allowlist`)

Either `{"allowed_doc_hosts": ["docs.example.com"]}` or `["docs.example.com"]`.
At least one host is required; an empty list is an error, never an implicit
"allow everything". Entries are canonicalised (lowercase, IDNA, trailing root
dot removed) when loaded. Entries that are not bare hostnames (for example
`https://docs.example.com/path` or ` docs.example.com`) are rejected at load
time.

## Link policy (the security-relevant part)

A URL becomes an `<a href>` only when all of these hold:

1. No character is whitespace or Unicode category `Cc`/`Cf` (control or
   invisible format, including `U+00A0` and bidi overrides). Browsers silently
   strip many of these, which is a classic host-confusion trick — so they are
   rejected before parsing rather than repaired.
2. No backslash (browsers treat `\` as `/`).
3. `urlsplit` succeeds and the scheme is exactly `https`.
4. The URL carries no credentials (`user:pass@`, `user@`, `user:@`). Checked on
   the raw authority (`@` in `netloc`) as well as the parsed `username` /
   `password` fields.
5. `parts.port` parses (catches `:notaport`).
6. The **parsed** `parts.hostname` canonicalises to a valid hostname (IDNA,
   lowercase, no trailing dot, no empty labels, no underscores) and matches an
   allowlist entry exactly.

Rejected URLs are rendered inside
`<p class="doc doc-blocked" data-doc-reason="…">` as plain text, together with
a short Chinese explanation. Reason codes: `empty_url`, `control_chars`,
`backslash`, `malformed_url`, `scheme_not_https`, `missing_host`,
`credentials`, `invalid_port`, `malformed_host`, `host_not_allowed`.

Accepted URLs get `rel="noopener noreferrer"` and `data-doc-host` set to the
canonicalised host that was checked. The page also sets a restrictive CSP
(`default-src 'none'`) and `referrer: no-referrer`, as defence in depth.

## Output semantics

* One `<section class="group" data-group-index="N">` per group, containing an
  `<h2>` with the group name.
* One `<article class="record" id="record-ID" data-record-id="ID">` per record,
  containing `p.record-id`, `p.summary`, optional `pre.details`, and either an
  `a.doc-link` (allowed) or `p.doc-blocked` (rejected).
* The generated DOM's `textContent` for each field equals the JSON string
  exactly. `<mark class="hit">` elements nest inside those fields during search
  and are transparent to `textContent`.
* `--report` re-parses the written HTML with the project's own DOM parser
  (`diagnostics/dom.py`) and reports what a consumer of the page would see:
  title, groups, per-record summary/details text, an `anchors` list containing
  every `href` in the document, and `record_count`.

## Checking the result directly

`build/report.json` (made from the shipped sample) is the quickest way to check
without a browser. It shows, for example:

* `rec-html-angle-brackets` → `summary` contains the literal
  `<error code="E42">缓冲区溢出 & 重试</error>`, and its `doc.kind` is `anchor`
  with `host` `docs.example.com`.
* `rec-non-latin` → Japanese/Russian/Arabic/Korean/Greek/emoji text preserved.
* `rec-link-userinfo` → `doc.kind` `plain-text`, `reason` `credentials`.
* `rec-link-evil-suffix`, `rec-link-ip-literal` → `host_not_allowed`.
* `rec-link-http` → `scheme_not_https`.
* `rec-link-tab-bypass` → `control_chars`.

## Verification and what it does *not* prove

`tools/local_verify.py`:

1. Calls `unittest.defaultTestLoader.discover("tests")` and reads
   `countTestCases()`. Zero discovered tests is an immediate failure
   (exit `3`), never a silent pass.
2. Runs the suite and requires at least one test to have **actually executed
   and not been skipped**; all-skipped also fails. Failures/errors exit `6`.
3. Independently proves the guard itself works: narrow guard probes (`GUARD-*`)
   plus a genuinely failing suite are fed through the same discovery path and
   asserted to be rejected, and three staged copies of the project are run as
   real subprocesses to prove the wrapper exits `3` for zero/all-skipped tests
   and `6` for a real failure (`PROVE-001..003`).
4. Runs the shipped sample and adversarial boundary cases end to end via the
   CLI, then asserts against the parsed DOM (`CLI-*`, `STRUCT-*`, `TEXT-*`,
   `LINK-*`, `HILITE-*`, `BOUND-*`, `REPORT-*`). Every case records its
   `case_id`, expected value and actual value in `build/verify_evidence.json`.
5. Exits `5` if any sample/boundary case fails, `4` on the 75 s timeout.

What this does **not** prove:

* Only the bundled 71 unit tests and 36 sample/boundary cases are covered.
  Any requirement, defect or edge case not encoded there is unverified; a green
  run is not a blanket claim over the whole feature.
* The DOM parser is a small standard-library parser, not a browser. It
  implements the `textContent`-relevant behaviour (entity decoding, `<pre>`
  leading-newline handling) but not full HTML tree-construction error
  recovery.
* No real network, cloud, identity or mail system is contacted. The
  allowlist and URLs are synthetic.
* CSS rendering, browser-specific URL canonicalisation beyond `urllib.parse`,
  and DNS resolution are out of scope. Host checks are string/IDNA based, so a
  URL whose host matches the allowlist by name is trusted even if DNS is
  compromised — that is a deployment concern, not something this page can fix.

## Layout

```
app.py                    CLI entry point
diagnostics/model.py      JSON loading, schema validation, grouping
diagnostics/link_policy.py allowlist + URL checks
diagnostics/render.py     HTML rendering, highlight splitting, DOM report
diagnostics/dom.py        small HTML parser used for assertions
samples/errors.json       sample data (markup chars, non-Latin, bad links)
samples/allowlist.json    sample allowlist (docs.example.com)
tests/                    unittest suite
tools/local_verify.py     verification wrapper (writes build/verify_evidence.json)
build/                    generated output (HTML, reports, evidence)
REQUIREMENTS.md           requirement → test/case_id map, plus unplanned items
```

## Requirement → evidence map

`REQUIREMENTS.md` maps each explicit prompt requirement to the tests or
`case_id`s that exercise it, and lists what is deliberately not covered. It is
a pointer table, not a substitute for the recorded actual values in
`build/verify_evidence.json`.

