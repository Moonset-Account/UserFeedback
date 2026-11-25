# logcursor — resumable composite log cursor

`logcursor` is a dependency-free Go 1.20 library that models the log side of a
parallel step runner whose CI page disconnects and reconnects while steps are
still running. It provides a **resumable, composable cursor** over per-step log
fragments, plus an in-memory backend used to drive it.

Everything here is local: no network access, no external targets, no
credentials. Test data is synthetic.

## Model

* **Per-step attempts.** A step is retried by starting attempt `1`, `2`, … .
  Each attempt numbers *its own* byte fragments from `0` again; sequence
  numbers are scoped to `(step, attempt)`.
* **Byte fragments.** Workers append `data` at an absolute offset inside the
  attempt's byte stream. Fragments may arrive out of order.
* **Gaps.** The backend may declare a byte range `[start, end)` as permanently
  unrecoverable.
* **End markers.** The backend declares the exclusive end offset of an attempt.
* **Snapshots / visibility version.** Every mutating call advances a version.
  Version `v` identifies exactly the state visible at that moment, and any past
  version can be reconstructed exactly.

## Public API

Backend (`backend.go`):

| Symbol | Purpose |
| --- | --- |
| `NewLog() *Log` | Create an empty backend. |
| `(*Log).AppendFragment(step string, attempt int, offset int64, data []byte) error` | Record bytes at an absolute stream offset. Identical re-append is idempotent; conflicting overlap, overlap with a gap, or data past the end marker is rejected. |
| `(*Log).DeclareGap(step string, attempt int, start, end int64) error` | Declare `[start,end)` permanently lost. Identical re-declaration is idempotent. |
| `(*Log).DeclareEnd(step string, attempt int, endOffset int64) error` | Declare the exclusive end of an attempt's stream. May be declared before all bytes arrive. |
| `(*Log).Version() int64` | Current visibility version. |
| `(*Log).HasGapRange(step string, attempt int, start, end int64) bool` | Whether a byte range is fully covered by declared gaps. |
| `(*Log).Seal()` / `(*Log).Sealed() bool` | Optional visibility fence against further mutation. |

Cursor (`cursor.go`):

| Symbol | Purpose |
| --- | --- |
| `(*Log).Read(c Cursor, limit int) (Page, error)` | Return up to `limit` bytes in total across all steps. Does not mutate the receiver or `c`. |
| `(*Log).Advance(c Cursor) (Cursor, error)` | Explicitly re-pin the cursor to the backend's current snapshot. |
| `Cursor` / `StepState` | JSON-serialisable position (`Pinned`, `Snapshot`, per-step `Attempt`, `Offset`, `GapCheck`, `Ended`). |
| `Page` / `StepData` / `GapNotice` / `FragmentRef` | Page result with per-step data, fragment ranges, gap notices, `Waiting`/`WaitingAt`, and `Done`. |
| `Page.Step(name)` / `Cursor.StepState(name)` | Lookups. |

### Semantics

* **Pinning.** A zero `Cursor` is unpinned; the first `Read` pins it to the
  current version. Later reads of that same cursor value are answered from the
  pinned snapshot, so re-reading a fixed page returns the **same data and the
  same next cursor** even if the backend has grown.
* **Explicit advance.** Only `Advance` moves a cursor to a newer snapshot.
  Without it a cursor never observes data added after its pin.
* **Contiguous offset advance.** Each step delivers only the contiguous prefix
  of its stream. A page may stop in the middle of a fragment and the returned
  cursor resumes at exactly the next unseen byte.
* **Page budget is global.** `limit` bounds the sum of delivered bytes over all
  steps, not the number of steps or fragments, and not per step.
* **Independent steps.** A step blocked on a hole (`Waiting`) never stops other
  steps from moving forward in the same page.
* **End needs closure.** An end marker does not finish a step. Every hole before
  the declared end offset must first be filled by a fragment or declared as a
  gap. `Page.Done` is true only when every step has reached that point.
* **Gap notice exactly once.** A gap that a cursor crosses produces one
  `GapNotice`. The acknowledgement lives in `StepState.GapCheck`, so it survives
  serialisation and reconnects and is never replayed.
* **No stale-content mixing.** A step reads only the highest attempt visible in
  its pinned snapshot, and offsets are absolute within that attempt. Late
  content from a superseded attempt (or data appended after the pin) can never
  be spliced into the stream a cursor delivers. `Advance` restarts a step from
  offset `0` when a newer attempt appears.

## Reconnect recipe

1. `page, err := backend.Read(cursor, limit)`.
2. Store `page.Cursor` (for example `json.Marshal`).
3. On reconnect, `json.Unmarshal` it back and call `Read` again — you resume at
   the exact byte where you stopped.
4. Once the caller decides to observe newer upstream data, call
   `backend.Advance(cursor)`.

## Run

```sh
go test ./...
go test -race -count=3 ./...
```

No module dependencies are required (`go.mod` declares only the module and Go
1.20).

## Verification scope

`cursor_test.go` covers, using synthetic in-memory data only:

* contiguous single-step reads, `Done`, byte accounting, fragment sequence
  numbers;
* per-attempt fragment numbering restarting at zero;
* a slow step's hole not blocking other steps, then filling the hole;
* end marker arriving before data and waiting for the hole;
* gap followed by the end marker, and end marker followed by a gap;
* gap notice emitted exactly once, including across a JSON round-trip;
* page limit splitting inside a fragment and resuming at the exact byte;
* page limit counted as a total across steps (round-robin fairness);
* a gap spanning multiple pages announced once, with exact offsets;
* retry: old-attempt stragglers excluded both for a pinned snapshot and after
  `Advance`;
* reproducibility: re-reading the same pinned cursor yields an identical page
  after backend mutation; JSON round-trip resumes identically;
* new steps invisible until `Advance`; `Advance` preserving partially read
  positions;
* zero limit, empty backend, zero-length stream;
* backend validation (empty step, negative attempt, empty fragment, overlaps,
  data past end, end conflict, gap past end, seal) and cursor validation
  (unsorted/unknown/future-version cursors);
* an interleaved three-step hole/gap/end scenario driven with a small budget;
* a concurrent append/read test intended for `-race`.

`example_test.go` is a runnable `Example` asserting the documentated reconnect
output.

### Known limits

* The backend keeps the full byte payload of delivered fragments in memory and
  retains version history for the life of the `Log`; this is a synthetic,
  in-process model, not a production storage engine.
* `Version` increments per recorded item, so a cursor pinned to a version
  records the *currently visible* set of attempts at that version. Cursors
  created from a snapshot remain reproducible because history is append-only.
