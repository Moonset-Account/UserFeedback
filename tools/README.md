# DUO123 local verification

This directory contains the local verification entry point for the optional
intra-procedural `verify=False` analysis added to the DUO123
(`BadRequestsUseLinter`) rule. Everything here runs against synthetic,
in-memory Python source: there are no network calls, no credentials, and no
private account data.

## What was implemented

### Production API

* `dlint.linters.BadRequestsUseLinter(intraproc_analysis=True)` — opt-in
  constructor flag. With the flag absent (default `False`) the linter keeps
  its historical direct-keyword behavior and result set exactly.
* `dlint.linters.helpers.intraproc_verify_analysis` — the analysis module:
  * `analyze_module(module_node, namespace)` returns a dict keyed by call-node
    id with `FlowFinding(node, state, origins)` values.
  * States: `FALSE` (confirmed insecure on every supported path), `TRUE`,
    `NONE`, `TOP` (cannot determine), and `BOTTOM` (not bound yet).
  * `join_values` / `join_environments` implement the finite-height lattice.
  * `parameter_environment` and `collect_bound_names` handle parameter and
    scope-binding isolation.
* Result messages emitted by the linter:
  * Confirmed: `DUO123 use of "verify=False" is insecure in "requests"
    module (value assigned at line N)` — the call keeps its real line/column;
    the suffix cites the assignment that produced the value.
  * Review: `DUO123 unable to determine "verify" value in "requests" module
    call, manual review required` — distinct from the confirmed message so an
    unknown value can never be displayed as confirmed safe or confirmed
    insecure.

The flake8/`MultiNodeVisitor` execution path is supported in both modes: in
intra-procedural mode the linter's `visit_Module` runs the whole-module
analysis and skips the generic visitor to avoid double reporting.

### Analysis semantics

* Boolean constants and variable copies are tracked with assignment-line
  provenance; later reassignment overwrites earlier values in order.
* Statically decidable conditions take only the live branch; undecidable
  conditions treat both sides as possible and join their environments.
* `return`/`raise` terminate a block, so only reachable continuations join;
  `while False` bodies and code after an unconditional return are unreachable
  and never counted.
* Loops run a bounded fixed point (64 iterations over a five-state lattice).
  Names assigned in the loop, and calls inside the loop body, are widened to
  the review state to stay honest about loop-carried values.
* `try`/`except`, `with`, `global`/`nonlocal`, augmented/attribute/subscript
  assignment and unknown calls widen affected locals to `TOP`.
* Only calls bound to the imported `requests` module (including
  `import requests as x` and `from requests import get`) are targets; a local
  rebinding such as `requests = MyClient()` shadows the import and is not
  analyzed as the library. Nested function/lambda/class scopes and function
  parameters are analyzed in isolation.

## Commands

Use the workspace virtualenv (Python 3.11) from the repository root:

```
.venv/bin/python tools/local_verify.py --list
.venv/bin/python tools/local_verify.py --case C01
.venv/bin/python tools/local_verify.py --case C02
.venv/bin/python tools/local_verify.py --case C03
.venv/bin/python tools/local_verify.py --case C04
```

Each case prints one JSON record with `case`, `seed`, the assertion counts
(`total`, `passed`, `failed`, `skipped`), any failures with expected/observed
values, and a summary of analyzer/linter outputs. The process exits non-zero
on any failed assertion, an exception, a missing case, zero assertions, or an
entirely skipped case — printing success text alone is never enough.

The conventional unit tests cover the same surface:

```
.venv/bin/python -m unittest tests.test_bad_requests_use
.venv/bin/python -m unittest tests.test_helpers.test_intraproc_verify_analysis
```

Note: `tests/test_namespace.py` and `tests/test_tree.py` require `pytest`,
which is not installed in this environment; their collection errors are
pre-existing and unrelated to this change. All other tests pass.

## Replayable samples

The samples below are the canonical scenarios; the same shapes are embedded
in `local_verify.py` and the unit tests.

Branch overwrite (true/false merge -> review, never confirmed):

```python
import requests

def f(flag):
    v = True
    if flag:
        v = False
    requests.get(verify=v)   # DUO123 unable to determine ... manual review
```

Loop changing the switch (both calls become review results):

```python
import requests

def f(items):
    v = False
    for item in items:
        v = True
        requests.get(verify=v)   # review: loop-carried value
    requests.get(verify=v)       # review: widened exit state
```

Early return isolating the only reachable (safe) path:

```python
import requests

def f(flag):
    v = True
    if flag:
        return
    requests.get(verify=v)   # no result: reachable path proves verify=True
```

Same-named custom object (not the requests library, no result):

```python
import requests

class Fake(object):
    def get(self, **kwargs):
        return None

def f():
    requests = Fake()
    requests.get(verify=False)   # shadowed binding: not a DUO123 target
```

## Known uncovered boundaries

These are deliberately conservative; unknown is reported rather than guessed:

* Inter-procedural flow is not modeled. A value returned by another function
  or passed in as a parameter starts at `TOP`, even if the callee would
  return a literal.
* `try`/`except`/`finally`, `with` blocks, `global`/`nonlocal`, augmented
  assignment and attribute/subscript mutation widen affected *locally
  assigned* names to `TOP`. A `global`/`nonlocal` declaration does not hide
  the imported module itself, so a global `requests` call is still analyzed
  against the import.
* `break`/`continue` inside loops are not modeled with a full control-flow
  graph; loop-assigned names and in-loop calls are widened regardless.
* Tuple/list unpacking where elements have differing abstract values binds
  each target to the single (possibly widened) value rather than element-wise
  precision.
* `**kwargs` calls are review results because the mapping may itself contain
  `verify`; an explicit `verify=` alongside it is still evaluated.
* Imports inside conditional or nested statements are outside the module
  namespace model and are treated as unknown local bindings.
* The legacy (default) mode remains purely import/namespace based: it does
  not see local rebinding, so a same-named custom object is still flagged
  there. Enabling `intraproc_analysis` removes that false positive.
