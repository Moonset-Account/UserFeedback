# Secure help-center draft boundary (SEC-20261114-047)

A local, self-contained model of the help-center editing rule: editors may
modify drafts **in the languages they are responsible for**, and callers
never touch disk paths directly.  Callers pass a **workspace** and a
**logical document ID**; a trusted per-workspace index locates the file.

Pure Python 3 standard library.  No pip, venv, pytest or network access.

## Layout

| Path | Role |
| --- | --- |
| `secure_docs.py` | core boundary library (store, handles, errors) |
| `app.py` | CLI main entry (`read` / `write` / `reload`) |
| `tests/test_secure_docs.py` | unittest suite, all fixtures in temp dirs |
| `tools/local_verify.py` | verification wrapper (unit tests + sample assertions) |

## Run the verification

```sh
/usr/local/bin/python3 tools/local_verify.py
```

Exit code `0` only if the discovered unittest suite (must contain and
actually execute ≥ 1 test) passes **and** every delivered sample/boundary
assertion prints `PASS`.  Zero discovered tests, all-skipped suites,
errors, failures or sample mismatches all exit non-zero.

## Input format

A root directory holds `users.json` plus one directory per workspace:

```
<root>/
  users.json                 # {"<user>": {"workspaces": [...], "languages": [...]}}
  <workspace>/
    index.json               # trusted index, see below
    ...draft files...
```

`index.json` (trusted; still validated against the boundary rules):

```json
{"documents": {"intro": {"path": "drafts/intro.md", "language": "fr", "version": 1}}}
```

`path` is always relative to the workspace directory.  `version` is the
current content version used for optimistic concurrency.

## CLI usage

```sh
/usr/local/bin/python3 app.py --root <root> read   --workspace ws-alpha --user alice --doc intro
/usr/local/bin/python3 app.py --root <root> write  --workspace ws-alpha --user alice --doc intro \
    --expected-version 1 --content "new draft text"
/usr/local/bin/python3 app.py --root <root> reload --workspace ws-alpha
```

## Output semantics

Every command prints one JSON object to stdout.  Success:
`{"ok": true, ...}` (`read` adds `content`, `language`, `version`;
`write` returns the new `version`).  Failure:
`{"ok": false, "error": "<ExceptionClass>", "message": "..."}`.

Exit codes: `0` ok · `1` unexpected · `2` unknown workspace/document ·
`3` forbidden (not a member / not the language editor) · `4` version
conflict · `5` unsafe path (symlink component or non-regular target) ·
`6` stale reference · `64` usage.

## Enforced rules → tests

| Requirement | case_id (test / sample) |
| --- | --- |
| Callers use workspace + logical doc ID, index locates the file, no disk paths accepted | `normal_edit_ok`, `cli_round_trip` |
| Read requires workspace membership | `read_requires_membership`, `same_name_cross_workspace` |
| Write additionally requires responsibility for the document's language | `write_requires_language_editor`, `sample_language_editor_required` |
| Write must match the expected content version | `version_conflict_same_version`, `sample_version_conflict` |
| Symlink between workspace root and target is rejected | `symlink_dir_component_rejected`, `symlink_file_target_rejected`, `sample_symlink_rejected` |
| Non-regular target is rejected | `non_regular_target_rejected` |
| Index reload invalidates old references | `stale_reference_after_reload`, `sample_stale_reference` |
| Same-name documents across workspaces stay isolated | `same_name_cross_workspace`, `sample_cross_workspace_isolation` |
| Original draft verifiably intact after every failure | asserted in each failure test (`assert_intact`) and in the `sample_*` checks |

## Limitations

* Local simulation: `UserDirectory` is a JSON-file adapter standing in for
  the external identity/team system; no real cloud, identity or mail
  integration.
* The index is trusted input, but its paths are still validated (relative,
  no `..`, no symlink components, regular-file target) before any I/O.
* Concurrency control is optimistic via the persisted `version` counter;
  it serializes writers within a workspace but is not a distributed lock.
* Writes are atomic via temp-file + `os.replace` in the target directory.
* Read opens use `O_NOFOLLOW` where the platform provides it (macOS/Linux).
