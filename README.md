# DAG Saga Orchestrator

An event-sourced orchestrator that provisions resources by walking a
directed acyclic dependency graph. Every forward call is paired with exactly
one compensation, and the whole run can be resumed from its recorded events
after a crash.

Built with the Node.js 24.15.0 standard library only — no dependencies.

## The guarantee set

1. **All prerequisites must succeed before a step is dispatched.** A step is
   only ready when *every* direct prerequisite has reached `succeeded`.
2. **One failure stops new work.** After the first observed failure no new
   forward step is started, but every call already in flight is awaited to a
   conclusion — a late success is still a success.
3. **Every successful step is compensated exactly once.**
4. **A step may only be undone after its successful direct successors are
   undone.** In a diamond, the shared prerequisite is compensated last, after
   both branches. If the left branch fails and the right branch reports
   success late, the shared prerequisite waits for the right branch's
   compensation to be confirmed.
5. **Compensation may retry, always with the same operation credential.** The
   credential is a pure function of `(runId, stepId, phase)`, so a retry or a
   post-crash replay is deduplicated by the called service rather than
   applying the effect twice.
6. **Reopening from recorded events continues unconfirmed commands.** A
   `step.dispatched` with no terminal event is an *in-doubt* command; it is
   re-issued with its original command id and attempt number, never a fresh
   one. The retry limit counts recorded failures, not process restarts, so a
   crash cannot silently consume the budget.
7. **A compensation that is retried past its limit is abandoned, and its
   prerequisites are blocked rather than compensated out of order.** The run
   finishes as `compensation_incomplete` with an explicit list of residual
   effects.

## Public API

### `new Orchestrator({ plan, runId, log, services?, maxCompensationAttempts? })`

| Option | Type | Meaning |
| --- | --- | --- |
| `plan` | `{ steps, services }` | The DAG and its service implementations. |
| `runId` | `string` | Identity of the run; part of every credential. |
| `log` | `{ append(event), events() }` | Durable event log (see below). |
| `services` | `object` | Optional override of `plan.services`. |
| `maxCompensationAttempts` | `number` | Retry limit per compensation (default `3`). |

Plan shape:

```js
{
  steps: [
    { id: 'network-root', service: 'network' },
    { id: 'dns-record',   service: 'dns',     dependsOn: ['network-root'] },
    { id: 'compute-node', service: 'compute', dependsOn: ['network-root'] },
    { id: 'database',     service: 'database', dependsOn: ['compute-node'] },
  ],
  services: {
    network: {
      async forward(ctx) { /* returns a result */ },
      async compensate(ctx) { /* undoes the effect */ },
    },
    // ...
  },
}
```

A service receives:

```js
{
  runId, stepId, phase: 'forward' | 'compensate',
  commandId,       // equals credential; stable across retries and replay
  credential,      // same value as commandId
  attempt,         // 1 on first try, +1 per recorded retry; a post-crash
                   // resend of an unconfirmed command keeps its attempt
  data,            // the step's own `data` field, if any
  results,         // forward only: direct prerequisites' results by step id
  result,          // compensate only: the forward result of this step
}
```

### `await orchestrator.run()`

Drives the run to a terminal state and returns a summary:

```js
{
  runId,
  status: 'completed' | 'compensated' | 'compensation_incomplete' | 'running',
  aborted: boolean,
  failedStepId: string | null,
  residualEffects: [{ stepId, service, state, reason, blockedBy, error }],
  steps: [{ id, forward, compensation, attempts: { forward, compensate } }],
}
```

Calling `run()` again on a finished log is a no-op. Calling it on a log from
an interrupted process resumes from the log.

### `new MemoryEventLog()` / `MemoryEventLog.from(snapshot)`

The durable log used by the orchestrator and tests. `append()` assigns a
monotonic `seq`; `snapshot()` returns a detached copy; `from(events)`
rebuilds it (sequence numbers continue correctly).

Any object with compatible `append(event)` / `events()` can be substituted,
so a file-backed or database-backed log can be dropped in without changing
the orchestrator.

### `projectEvents(events, plan)`

Pure replay of a log into the full forward/compensation state. Useful for
inspecting a run without executing anything.

### `credentialFor(runId, stepId, phase)`

The deterministic operation credential, `op_<32 hex chars>`.

### `validatePlan(plan)`

Validates the graph: non-empty steps, unique ids, known prerequisites, no
self-dependency, no cycles, and `forward`/`compensate` present for every
referenced service. Throws `PlanError`. Returns the topological `order`,
`dependents` and `prerequisites` maps.

### `src/testing.mjs` (test-only)

`createServiceHarness()`, `diamondPlan()`, `CrashingLog`. See the test files.

## Event log

| Event | Fields |
| --- | --- |
| `run.started` | `runId, planFingerprint, steps, credentials` |
| `step.dispatched` | `stepId, phase, commandId, attempt` |
| `step.succeeded` | `stepId, commandId, attempt, result` |
| `step.failed` | `stepId, commandId, attempt, error` |
| `step.compensated` | `stepId, commandId, attempt` |
| `step.compensation_failed` | `stepId, commandId, attempt, error` |
| `step.abandoned` | `stepId, reason, blockedBy?` |
| `run.aborted` | `failedStepId, reason` |
| `run.finished` | `status, failedStepId, residualEffects` |

Every event takes a `seq` from the log. All state is derived from these
events; nothing lives only in memory.

## Reproduce

```sh
node --version        # must be v24.15.0
node --test 'tests/*.test.mjs'
node examples/provisioning.mjs
```

`npm test` runs the same suite.

## Verification scope

### Covered by executed tests (33 passing)

| File | Scenarios |
| --- | --- |
| `tests/graph.test.mjs` | Well-formed diamond, cycles, duplicate ids, unknown prerequisites, self-dependency, empty plan, missing forward/compensate, direct topological check, credential stability |
| `tests/event-log.test.mjs` | Monotonic `seq`, caller `seq`/`ts` ignored, snapshot round-trip through JSON, sequence continuation after restore, frozen events, malformed event rejection |
| `tests/forward.test.mjs` | Happy-path diamond ordering, dispatch gating on all prerequisites, left-branch failure compensates the shared root once, no new forward after first failure, same-wave in-flight step allowed to finish |
| `tests/compensation.test.mjs` | Reverse dependency order (shared root last), exactly one compensation per successful step, retries reuse one credential, prerequisite blocked by an uncompensated successor, retry limit then abandon, transitive blocking up a chain |
| `tests/interleaving.test.mjs` | Left fails fast / right succeeds late (root waits for right's compensation, verified by event order), late success in the failing wave is compensated and gates its parent, concurrent dispatch within a wave, forward-failed step never compensated, never-started branch left pending |
| `tests/replay.test.mjs` | Resume unconfirmed forward with identical credential, resume unconfirmed commands when the abort was never persisted, resume unconfirmed compensation exactly once, finished run not re-executed, deterministic projection, mismatched runId/plan refused, crash between retries, `maxCompensationAttempts = 1` |

The late-success ordering assertions read the actual event log and compare
indexes of `step.compensated` vs `step.dispatched`, so they verify causal
order rather than a pre-written expectation.

### Not covered / limitations

- **No persistent storage shipped.** `MemoryEventLog` is in-memory. Surviving
  a real process restart requires writing `snapshot()` to disk and rebuilding
  with `MemoryEventLog.from`, or supplying another `append`/`events` store.
  The crash tests simulate the crash in-process with `CrashingLog`; they do
  not spawn a second OS process.
- **No timeouts or transport retries for forward calls.** A forward call that
  never settles will hold the wave open. Timeout policy is left to the
  service implementation.
- **Compensation retries are immediate**, with no backoff or jitter.
- **Abandoned compensations are terminal.** Once a step is abandoned for
  exhausting retries, a later `run()` on the same log will not retry it. A
  future "operator resume" mode is not implemented.
- **Idempotency is delegated.** Exactly-once effects depend on the called
  service honouring the operation credential. The harness models this; the
  orchestrator cannot prevent a service that ignores credentials from
  double-applying.
- **No concurrency limit** across a wave; all ready steps are dispatched at
  once.
