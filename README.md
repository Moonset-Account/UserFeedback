# Admission hook interpreter

A finite-domain admission hook interpreter. Objects have declared fields whose
values come from declared finite domains. Hooks fire on equality, missing-field
and set-membership conditions and either set a field to a declared value or
delete a field. Each round runs every hook exactly once, in any order; a round
with no *effective* write is what makes an object admissible, and the accept
condition is checked at that moment.

The tool answers, for a caller-supplied initial object: **do all legal round
orderings terminate, and do they all end with the same admission decision and
the same object?** When the answer is no, it produces, for every failure kind
present, separately replayable evidence that a second entry point re-executes
from scratch.

## Requirements

- Node.js **24.15.0** (the `node` on `PATH`; the project is ESM-only and uses
  the standard library exclusively)
- No dependencies are installed, ever.

Everything below was executed on Node v24.15.0 with no `node_modules` present.

## Quick start

```sh
node --test                 # run the full suite
node bin/admission.mjs --help
```

## Public API

Import from `src/index.mjs` (equivalently `src/` modules directly).

### `defineProgram(spec) -> program`

Validates and compiles a program. Throws `AdmissionError` with a stable `code`.

```js
{
  domain: { field: [allowedValue, ...], ... },   // required, non-empty field names
  hooks: [ { id?: string, when: condition, action: action }, ... ], // <= 8
  accept: condition
}
```

- `MAX_HOOKS` is `8`; more throws `TOO_MANY_HOOKS`.
- Hook ids default to `h0`, `h1`, … and must be unique (`DUPLICATE_HOOK_ID`).
- Every field referenced by a condition must be declared (`UNKNOWN_FIELD`).
- `action` is `{ kind: 'set', field, value }` or `{ kind: 'delete', field }`.
  A `set` value must be in the declared domain (`VALUE_OUT_OF_DOMAIN`).
- `__proto__` is rejected as a field name; duplicate domain values throw
  `DUPLICATE_DOMAIN_VALUE`.
- Values are arbitrary finite JSON (numbers, strings, booleans, null, arrays,
  objects), compared structurally with canonicalised object keys.

Conditions:

| kind      | shape                            | meaning                               |
| --------- | -------------------------------- | ------------------------------------- |
| `eq`      | `{kind:'eq', field, value}`      | field present and structurally equal  |
| `missing` | `{kind:'missing', field}`        | field absent                          |
| `in`      | `{kind:'in', field, values:[…]}` | field present and in the set          |
| `all`     | `{kind:'all', of:[cond,…]}`      | every sub-condition holds             |
| `any`     | `{kind:'any', of:[cond,…]}`      | at least one holds                    |
| `not`     | `{kind:'not', of:cond}`          | sub-condition does not hold           |

`eq`/`in` require presence; use `not({kind:'missing'})` for "present, any value".

### `runRound(program, initialObject, order) -> result`

Executes one round with `order` (a permutation of all hook ids) and returns:

```js
{
  object,           // resulting object
  changed,          // true iff at least one write changed the object
  effectiveWrites,  // count of writes that actually changed the object
  trace: [ { hook, matched, action, field, before, after, changed,
             pendingBefore, pendingAfter } ]
}
```

A round that writes a value and writes it back is `changed: true` with
`effectiveWrites: 2` — it is *not* a stable round. A hook whose `set` stores the
value already present is a no-op (`changed: false`). One sample therefore
distinguishes "restored to the original" from "genuinely stable".

### `runRounds(program, initialObject, orders) -> { rounds, object }`

Runs an explicit sequence of rounds, each with its own order.

### `analyze(program, initialObject, { maxStates? }) -> report`

Explores **every** legal run (all round orders, every round) and returns:

```js
{
  verdict,            // 'admissible' | 'inconsistent-results' |
                      // 'stable-rejection' | 'nontermination' | 'multiple-failures'
  admissible,         // verdict === 'admissible'
  admission,          // 'accepted' when admissible, else null
  allRunsTerminate,
  uniformTermination,
  terminationOutcomes: [ { decision, object, witness } ], // every distinct ending
  nonterminationWitnesses: [ witness ],
  findings: [ { kind, message, witnesses | outcomes } ],  // one per failure kind
  witnesses: [ witness ],  // all of the above, replayable
  components,         // static independence diagnostic
  stats
}
```

`maxStates` (default 200000) bounds the reachable state space; exceeding it
throws `ANALYSIS_LIMIT` rather than looping.

### `replayEvidence(program, evidence) -> { ok, failures, trace, reproduced }`

The **independent entry point**. It re-executes the interpreter from the
evidence's `initialObject` and recomputes everything; it does not consult the
analyser. `verifyEvidence(spec, evidence)` additionally rebuilds the program from
a raw spec, so a replayed claim is checked against a fresh compilation.

`replayEvidence` throws `INVALID_EVIDENCE` only for structurally unusable input
(wrong version/kind). All *failed claims* are returned as `failures[]` with
stable codes (`PROGRAM_MISMATCH`, `STABLE_ROUND_CHANGED`, `OBJECT_MISMATCH`,
`CYCLE_OBJECT_NOT_CLOSED`, `CYCLE_PENDING_NOT_CLOSED`, `DECISION_MISMATCH`, …).

### Evidence format

```js
{
  version: 1,
  kind: 'termination' | 'nontermination',
  programSignature, programHookIds,
  initialObject,
  path: order[],           // complete rounds, each performing >= 1 write
  // termination:
  stableRound: order,      // the complete zero-write round
  expected: { decision, object },
  // nontermination:
  prefix: hookId[],        // hooks already executed in the current round
  cycle: hookId[],         // steps that close back onto the start state
  expected: {
    startObject, endObject,      // must be structurally equal
    startPending, endPending,    // must be equal within-round pending sets
    startChanged, endChanged     // must be equal change flags
  }
}
```

A cycle witness is accepted only if replaying `path`, `prefix`, then `cycle`
returns to the **same object** and the **same within-round pending hook set**
(and the same change flag) with no terminating round in between. Closing the
object alone is not enough: the remaining hooks would differ, so the repetition
would not be legal.

## How convergence is decided

Runs are analysed at single-hook-step granularity. A search state is
`(object, pending hook set, changed-this-round flag)`; executing a hook removes
it from `pending`. When `pending` empties, a round with no write **terminates**
(accept is checked on that object); a round with a write starts a fresh round
with the full pending set. Because the domains are finite, an infinite run exists
iff the non-terminal step graph has a cycle (Tarjan SCC + cycle extraction).

Crucially, states are keyed by the pending **set**, not by an order. All
interleavings reaching the same `(object, pending set, flag)` are merged, so
reordering independent hooks stores one state rather than `k!` permutations.
`buildComponents` reports the static independence groups for diagnosis. Example:
eight hooks on eight independent fields have `8! = 40320` orders but the search
stores only 510 states (tested).

## Reproduction steps

```sh
node --test
node bin/admission.mjs analyze examples/admissible.spec.json examples/admissible.initial.json
node bin/admission.mjs analyze examples/nonterminating.spec.json examples/nonterminating.initial.json --json > /tmp/nt.json
# extract the witness and replay it through the verify entry point:
node -e "const r=require('/tmp/nt.json');require('fs').writeFileSync('/tmp/w.json',JSON.stringify(r.nonterminationWitnesses[0]))"
node bin/admission.mjs verify examples/nonterminating.spec.json /tmp/w.json
node bin/admission.mjs analyze examples/divergent.spec.json examples/divergent.initial.json
```

CLI exit codes: `0` when the analysis ran (whatever the verdict), `1` on
invalid input or failed verification, `2` on usage errors.

## Verification scope

Tested with `node --test` (**43 tests**, all passing on Node v24.15.0, no
dependencies). Every row below is executed by the suite; nothing is claimed that
the tests do not run.

| area | tests | what is covered |
| --- | --- | --- |
| program validation | `tests/engine.test.mjs` | hook cap (8 accepted, 9 rejected), ids, domain/values, unknown fields, malformed conditions, structured values |
| single round | `tests/engine.test.mjs` | order must be a permutation, per-hook trace with pending sets, write-back vs stable, delete no-op, all condition kinds |
| convergence | `tests/analyze.test.mjs` | admissible, inconsistent-results (incl. two accepted-but-different objects), stable-rejection, nontermination, mixed `multiple-failures` (inconsistent+nontermination and rejection+nontermination), empty hook set, missing initial fields, state limit |
| write-back distinction | `tests/engine.test.mjs`, `tests/analyze.test.mjs` | a single sample separates a restored-value round (`changed: true`) from a genuinely stable round |
| independence | `tests/analyze.test.mjs` | 8 independent hooks: `< 2000` stored states vs `40320` permutations; components reported |
| reference cross-check | `tests/analyze.test.mjs` | 120 randomised programs compared against a naive all-permutations reference (`tests/reference.mjs`); a wider 800-case sweep was run during development with zero mismatches (not part of the committed suite) |
| evidence replay | `tests/witness.test.mjs` | genuine evidence replays; object-closure break, pending-set break (object closes!), forged object/decision, wrong program, non-progressing path round all rejected; every analyser witness replays via `verifyEvidence` |
| CLI | `tests/cli.test.mjs` | analyze/round/verify end-to-end, exit codes, README example files exist |

Known limits:

- Reachable state space is bounded by `maxStates` (default 200000); exceeding it
  throws `ANALYSIS_LIMIT` rather than silently truncating.
- The analysis is exhaustive within that bound; it never approximates.
- `node --test tests` (a bare directory argument) is not supported by this Node
  version; use `node --test` (discovers `tests/*.test.mjs`) or
  `node --test "tests/*.test.mjs"`.

