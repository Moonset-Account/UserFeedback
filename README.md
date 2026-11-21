# Cross-compilation dependency resolver

A local, dependency-free Python 3.11 package that solves package assembly for
a cross-compilation platform with two independent assembly domains.

The problem it models: when cross-compiling, generators/build tools **run on
the host**, while the libraries they help produce are **shipped in the target
image**.  A package name may therefore need different versions in each domain
— for example a host code generator linked against `libssl` 1.x and a target
runtime library linked against `libssl` 3.x.  Both sides must assemble
successfully, and neither domain's choice may constrain the other's.

## Entry points

The module entry point is `resolver.resolve`:

```python
from resolver import (
    Domain, build, run, pkg, root, locked, vrange, request, resolve,
)

result = resolve(request(
    roots=[root("app", Domain.TARGET)],
    candidates=[
        pkg("app", (1, 0), [build("codegen"), run("libssl", vrange({0: (3, 3)}))]),
        pkg("codegen", (1, 0), [build("libssl", vrange({0: (1, 1)}))]),
        pkg("libssl", (1, 9)),
        pkg("libssl", (3, 2)),
    ],
    host_platform="x86_64",
    target_platform="arm64",
))

if result.ok:
    for selection in result.selections:
        print(selection.domain, selection.name, selection.version)
else:
    print(result.failure.render())   # dependency chains back to the contradiction
```

Test entry point (run from the repository root):

```console
python3 -m unittest discover -s tests -v
```

There is no network access, no third-party dependency, and no CLI beyond the
library call above.

## Input model (`resolver/model.py`)

| Concept | Type | Meaning |
| --- | --- | --- |
| Domain | `Domain.HOST` / `Domain.TARGET` | Host = build machine; target = produced image. |
| Version | tuple of ints | Version vector; compared lexicographically. |
| Constraint | `VersionRange` | Conjunction of closed integer `Interval`s per vector coordinate. |
| Dependency kind | `DependencyKind.BUILD` / `RUN` | Build → host; run → referrer's domain. |
| Root requirement | `RootRequirement(name, domain, range)` | A demanded package with its domain. |
| Candidate | `PackageVersion(name, version, deps, platforms)` | One available version. |
| Platform | string | Runnable platform for a candidate / active-platform gate for an edge. |
| Lock | `LockedPackage(domain, name, version)` | Previously locked slot, used only for the objective. |

## Rules

1. **Placement.** Build dependencies always resolve in the host domain. Run
   dependencies resolve in the same domain as the package declaring them.
   Host and target are separate slot spaces.
2. **One version per slot.** A slot is `(domain, package name)`; at most one
   version per slot per domain.
3. **Platform gating.** A candidate is usable only on a platform listed in its
   `platforms` set (empty = universal), judged against **its domain's**
   platform. A dependency edge is active only when its `platforms` set contains
   the **destination** domain's platform.
4. **Reachability.** Only packages reachable from a root through active edges
   are selected; unused candidates are ignored.
5. **Objective.** Minimise re-versioned locked slots — slots present in both
   the old lock and the solution whose version changed. Locked slots that are
   not selected, and slots absent from the lock, are free.
6. **Tie-break.** Compare selected version vectors slot by slot in fixed order
   (host before target, then package name); the lexicographically greatest
   vector at the first differing slot wins. An unselected slot ranks below
   every version.
7. **Backtracking.** A constraint discovered later in the graph may force an
   already-selected version to be reselected; the search is an exhaustive,
   deterministic DFS that accumulates constraints and backtracks.
8. **Failure.** With no valid assembly, the result carries a `ConflictReport`
   whose chains each begin at a root requirement and end at a participating
   version constraint; `render()` prints them.

## Verification scope (`tests/test_resolver.py`)

38 `unittest` cases, all driving the public API with local synthetic fixtures:

* **Accepted results** — host/target placement of build and run edges,
  transitive domain stickiness, the cross-compilation case where the same
  package resolves to different major versions in host and target, one version
  per slot, reachability, platform gating, and the objective/tie-break rules.
* **Rejected results** — duplicate candidates, conflicting lock entries,
  unknown domain/kind, negative version components, and a node-budget
  overrun all raise; unsatisfiable requests return `ok == False` with a
  conflict report.
* **Boundary / interleaved cases** — a new constraint forces the
  lexicographically greatest version to be retracted; a locked version is
  retracted when it cannot satisfy; the lock is a preference, not a hard
  constraint; a locked-but-unreachable slot is free; host-before-target slot
  ordering changes the chosen trade-off; a candidate must run on its own
  domain's platform; edge gating uses the destination platform; root-vs-root
  contradictions produce one-hop chains; a cyclic dependency graph terminates.

## Layout

```
resolver/versions.py   version vectors, intervals, ranges
resolver/model.py      dataclasses: domains, deps, candidates, roots, lock, results
resolver/solver.py     the search, objective, tie-break, and conflict explanation
resolver/__init__.py   public API and convenience constructors
tests/test_resolver.py acceptance tests
```
