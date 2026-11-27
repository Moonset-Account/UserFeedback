# Event-driven channel kernel

A single-file, dependency-free Python 3.11 model of an inter-process message
channel that supports **buffered** and **unbuffered** transfers and
**selection** (one action registering several candidate branches, at most one
of which completes). Everything that changes state is driven by an ordered
**event list**, so same-beat races between readiness and cancel/timeout are
decided by event order rather than by wall-clock timing.

## Files

```
channel.py              implementation (library)
tests/test_channel.py   behaviour tests (buffered, unbuffered, selection, cancel)
tests/test_invariants.py invariant + randomized-seed tests
README.md               this file
```

## Running

```bash
python3 -m unittest discover -s tests -v
```

Requires only the Python 3.11 standard library. No network access, no
third-party packages.

## Public API

### `Channel(name, capacity=0)`

* `capacity > 0` — buffered. Values sit in a FIFO queue; a send appends when
  there is room, a receive pops the oldest value. A transfer never bypasses the
  queue.
* `capacity == 0` — unbuffered. Nothing is stored; a transfer requires a live
  send and a live receive to meet.
* `channel.snapshot()` → tuple of queued values, oldest first.
* `channel.seed(*values)` — fixture helper: place values directly in the queue
  (respects capacity). Intended for tests; normal operation goes through the
  kernel.

### `Kernel()`

Registration:

* `kernel.register(caller, branches) -> seq`
  `branches` is a non-empty list of `Branch(kind, channel, value=None)` objects
  or `(kind, channel[, value])` tuples, where `kind` is `"send"` or
  `"receive"`. Branch order is significant. Returns the action's sequence
  number; sequence numbers increase in registration order and are the primary
  tie-break key.

Event list (all return `self`, so they chain):

* `kernel.event("ready"[, seq])` / `kernel.ready([seq])` — run a matching
  round.
* `kernel.event("cancel", seq)` / `kernel.cancel(seq)` — withdraw an action as
  `cancelled`.
* `kernel.event("timeout", seq)` / `kernel.timeout(seq)` — withdraw an action
  as `timed_out`.
* `kernel.has_events()` — whether queued events remain.
* `kernel.step()` — process exactly one event; returns `False` when the list is
  empty. Lets a caller observe state between two events on the same beat.
* `kernel.run(settle=True)` — process the whole list in order, then (by
  default) run one final matching round, and return `list[ActionState]` in
  registration order. `run(settle=False)` honours the event list strictly:
  only `ready` events commit anything.

Inspection:

* `kernel.snapshots()` → `list[ActionState]` in registration order.
* `kernel.results()` → `{seq: ActionState}`.
* `kernel.state_of(seq)` → `ActionState` for one action (`KeyError` if unknown).
* `kernel.branches_of(seq)` → per-branch tuple of `pending` / `committed` /
  `withdrawn`.
* `kernel.pair_log()` → `list[PairRecord]` of completed unbuffered rendezvous.
* `kernel.timeline()` → every completion / withdrawal in the order it happened.
* `kernel.pending()` → sequence numbers still live.

Result types (frozen dataclasses):

* `ActionState(seq, caller, state, value, branch, peer, peer_branch)`, with
  `state ∈ {pending, sent, received, cancelled, timed_out}` and
  `finished` = state is terminal.
* `PairRecord(send_seq, send_branch, recv_seq, recv_branch, value)`.

Module constants: `PENDING`, `SENT`, `RECEIVED`, `CANCELLED`, `TIMED_OUT`,
`COMMITTED`, `WITHDRAWN`.

## Semantics and decisions

1. **Buffered (capacity > 0).** Sends append to a FIFO, receives pop the front.
   If the queue is full a send stays `pending`; if empty a receive stays
   `pending`. Neither ever bypasses the queue to meet a counterpart directly, so
   FIFO order is a hard invariant.
2. **Unbuffered (capacity == 0).** A commitment consumes exactly one live send
   and one live receive that belong to **two different callers**. A caller may
   not rendezvous with itself, even through two separate registered actions.
3. **Selection.** Branches of one action are mutually exclusive. Committing one
   branch retires the whole action, so every sibling branch leaves the candidate
   pool in the same step. A single action's two branches can never pair with each
   other.
4. **Tie-breaking.** Across actions: lower registration `seq` wins. Within an
   action: lower branch index wins. Peer search during a rendezvous follows the
   same ordering.
5. **Event order.** `run()`/`step()` consume events strictly in the order they
   were queued. If a `cancel`/`timeout` for an action and a `ready` round that
   would commit it land on the same beat, whichever appears first in the list
   wins. A final matching round runs after the list drains (unless
   `settle=False`), but it cannot revive an action withdrawn earlier.
6. **Late cancel is a no-op.** Withdrawing an action that already finished
   changes nothing, so a value already delivered is never taken back or handed
   out a second time. Withdrawing an action clears its value and branch and sets
   the `cancelled`/`timed_out` terminal state.
7. **No double collection.** Each commitment removes both actions from the live
   set; each completed unbuffered transfer is recorded once in `pair_log()` and
   appears in exactly one `ActionState.peer` link on each side.

## Verification scope

Verified locally with synthetic data only (no network, no real IPC):

* `python3 -m unittest discover -s tests -v` — 58 tests, all passing.
* Buffered: FIFO across rounds, blocked send on full, blocked receive on empty,
  retry after drain, full-then-drain ordering, decoupled transfers have no peer,
  seeded fixture order.
* Unbuffered: send/receive from different callers, single-sided pending,
  same-caller rejection, self-selection rejection, registration-order peer
  choice, multiple sends one receive, channel identity, late arrival joining an
  earlier pending action.
* Selection: only one branch commits, branch-order and registration-order
  tie-breaks, siblings withdrawn on both sides, three-way crossing selects
  commit exactly one pair, two independent pairs in one round.
* Cancel/timeout: cancel-before-ready and ready-before-cancel, timeout before
  readiness, same-beat ordering in both directions (including buffered),
  late cancel after delivery, three-way cross candidates with late cancel,
  idempotent double cancel, unknown-seq cancel ignored, strict `settle=False`
  runs, per-event `step()` observation.
* Invariants over 200 deterministic pseudo-random seeds: at most one committed
  branch per action, terminal actions carry no value, every unbuffered pair is
  distinct send/receive with distinct callers and one shared value, peer links
  are symmetric and match the pair log, buffered queues never exceed capacity
  and satisfy `committed sends − committed receives == queued count` per
  channel, unbuffered channels never store.

The tests assert the documented ordering and terminal-state rules; they do not
claim to model OS-level scheduling or thread timing — the kernel is a
deterministic, event-ordered model.
