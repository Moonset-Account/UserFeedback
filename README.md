# requestbudget

Hierarchical request budget for a fan-out RPC tree.

A foreground request opens one `Root` with a fixed **action quota** and an
**absolute deadline**. Every RPC — including RPCs issued by a sub-service —
must first reserve its maximum usable quota from its parent reservation. A
reservation can only hand out quota from its own remaining balance, so the
tree can never allocate more than the root quota, and nothing can be borrowed
from a sibling branch.

## Public API

```go
package budget // import "requestbudget"

func NewRoot(id string, limit int64, deadline time.Time, clock Clock) (*Root, error)
type Clock func() time.Time

// Root
func (r *Root) ID() string
func (r *Root) Limit() int64
func (r *Root) Deadline() time.Time
func (r *Root) Charged() int64
func (r *Root) Held() int64
func (r *Root) Available() int64
func (r *Root) Audit() Audit
func (r *Root) Reserve(amount int64) (*Reservation, error)

// Reservation
func (n *Reservation) Reserve(amount int64) (*Reservation, error) // child call
func (n *Reservation) Retry() (Attempt, error)
func (n *Reservation) Timeout() error
func (n *Reservation) Settle(usage int64) (Settlement, error)
func (n *Reservation) Cancel() (int64, error)
func (n *Reservation) ReserveID() string
func (n *Reservation) Limit() int64
func (n *Reservation) State() State
func (n *Reservation) Charged() int64
func (n *Reservation) Available() int64
func (n *Reservation) Held() int64
func (n *Reservation) Attempts() []Attempt
func (n *Reservation) Settlement() (Settlement, bool)
func (n *Reservation) Children() []*Reservation
```

Records:

```go
type Attempt struct { ID, ReservationID string; Seq int; At time.Time }
type Settlement struct { ID, ReservationID string; Usage, Refund int64; At time.Time }

type State string // "active" | "unsettled" | "settled" | "cancelled"

type NodeAudit struct {
    ReserveID string; State State
    Limit, OwnUsage, Charged, Held, Available, Refunded int64
    Children []NodeAudit
}
type Audit struct {
    RootID string
    Limit, Charged, Held, Available int64
    Branches []NodeAudit
}
```

Sentinel errors (inspect with `errors.Is`):
`ErrDeadlineExceeded`, `ErrInsufficientBudget`, `ErrInvalidAmount`,
`ErrInvalidState`, `ErrChildrenUnsettled`, `ErrChildrenOutstanding`,
`ErrUsageExceedsReservation`, `ErrAlreadySettled`, `ErrInvalidConfiguration`.

## Semantics

- **Reserve before calling.** `Reserve` records an independent reservation id
  (`res-N`) and returns the first attempt with its own id (`try-N`).
  Reservations are only granted while `now < deadline` and only from the
  parent's uncommitted balance: `limit − charged − held`.
- **Children reallocate from their own quota.** `Reservation.Reserve` fails with
  `ErrInsufficientBudget` when the amount exceeds what that reservation has
  left; the parent's other children keep their holds.
- **Retry is a separate action.** Each successful `Retry` appends an attempt
  with a fresh id and `Seq+1`, and is subject to the root deadline.
- **Timeout reports unsettled.** `Timeout` moves `active → unsettled` and
  keeps the reservation's quota **held** by its parent.
- **Settle requires a valid usage and settled children.** `Settle(usage)`
  is accepted once per reservation, only in state `active`/`unsettled`, with
  `0 ≤ usage ≤ limit − childCharged`, and only when every direct child is
  closed. The refund (`limit − charged`) goes to the parent. Any later
  settle returns `ErrAlreadySettled` and changes nothing.
- **Cancel never borrows.** `Cancel` refunds only that reservation's own
  uncommitted balance, refuses while a direct child is active or unsettled, and
  never touches quota held by sibling or child reservations.
- **The deadline is absolute.** After the deadline all new actions
  (`Root.Reserve`, `Reservation.Reserve`, `Retry`) fail with
  `ErrDeadlineExceeded`, while reconciliation (`Timeout`, `Settle`, `Cancel`)
  keeps working so already consumed quota can still be squared away.

### Accounting invariants

Checked over `Audit()` snapshots and asserted by the tests:

- Root: `Limit == Charged + Held + Available` at all times.
- Open node (active/unsettled): `Limit == Charged + Held + Available`,
  `Refunded == 0`.
- Closed node (settled/cancelled): `Limit == Charged + Refunded`,
  `Held == 0`, `Available == 0`.
- Parent `Charged` includes its own accepted usage plus settled descendants;
  parent `Held` is the sum of limits of its unresolved direct children.

## Reproduce

Requires Go 1.20.2 (the `go.mod` targets `go 1.20`; no third-party modules).

```sh
cd workspace
go test ./...                        # full suite
go test -race -count=1 -v ./...      # race detector, verbose
go test -run TestInterleaved -v ./... # the required interleaved scenario
gofmt -l .                           # expect no output
```

## Test coverage

| Test | What it verifies |
| --- | --- |
| `TestNewRootValidation` | Config boundary: empty id, negative/zero limit, zero deadline; zero quota is valid but unusable. |
| `TestReserveBounds` | Zero/negative/over-limit amounts rejected; exact remaining amount accepted at root and child. |
| `TestSettleAndRefund` | Normal settle, full zero-usage refund, partial refund, root totals. |
| `TestChildCannotExceedParentRemaining` | Nested reallocation capped at parent's free quota; child refunds only its unused part; parent keeps holding. |
| `TestSettleBlockedByOpenChildren` | Parent settle/cancel blocked until every child settles; timed-out child still blocks. |
| `TestSettleUsageExceedsReservation` | Parent/leaf usage bounds; negative usage rejected. |
| `TestTimeoutKeepsQuotaHeldUntilValidSettlement` | Timeout holds quota; unreported branch cannot spawn or retry; invalid usage leaves it unsettled; valid usage refunds. |
| `TestLateSettlementAppliedOnce` | Duplicate/late settle returns `ErrAlreadySettled` and has no second effect, even after the deadline. |
| `TestCancelDoesNotBorrowSiblingReservation` | Cancel refunds only its own branch, siblings keep spendable holds, no double refund. |
| `TestCancelBlockedByOutstandingChildren` | Cancel refused while children are active or unsettled; later cancel refunds exactly the rest. |
| `TestDeadlineRejectsNewActionsButReconcilesExisting` | One nanosecond before deadline new actions pass; at the deadline they fail; `Timeout`/`Settle`/`Cancel` still reconcile and conserve. |
| `TestIdentifiersAreIndependent` | Reservation, retry and settlement ids are distinct and stable. |
| `TestConcurrentChildrenConserveQuota` | 200 goroutines reserve 20 each from a 1000 quota branch; exactly 1000 granted, tree invariant holds. |
| `TestConcurrentSettleAppliesOnce` | 50 racing settles on one reservation; exactly one accepted. |
| `TestInterleavedNestedBranchesWithPartialSettlement` | **Required scenario**: two nested branch trees (A→A1,A2 and B→B1) finish interleaved; A1 times out and stays unsettled while A2 partially settles; verifies which next call may be issued (root admits 39, rejects 40) and whole-tree quota conservation (charged 52, held 0, available 48, total 100), plus the explicit final node shape. |

### Required interleaved scenario walkthrough

Root quota 100, deadline 10s, fake clock.

1. Reserve A=40 (children A1=20, A2=10) and B=50 (child B1=30) →
   root available 10.
2. A1 times out → `unsettled`, its 20 stay held; A available 10.
3. B1 settles with usage 12 → refund 18 to B; B charged 12, available 38.
4. A2 settles with usage 5 → refund 5 to A (A charged 5, **partial
   settlement**); A cannot settle (A1 still open); A cannot hand out A1's 20
   (a 16 reservation is refused).
5. B settles with usage 9 → B charged 21, refund 29 reaches the root. Root
   charged 21, held 40 (all of A) … **next root call that may be issued is
   exactly 39**; 40 is refused, a 39 reservation proceeds and, when cancelled,
   returns all 39.
6. A1 later reports valid usage 20 → refund 0; a duplicate report is
   `ErrAlreadySettled`; A then settles with usage 6 → charged 31, refund 9.
7. Final conservation: charged 52, held 0, available 48, total 100. Every
   node's `Charged + Refunded == Limit` or `Charged + Held + Available ==
   Limit` holds.

## Verification scope

- Executed here with the local Go 1.20.2 toolchain only, no network access and
  no third-party dependencies.
- Every test listed above was actually run; results below are from real runs:

```
$ go test ./...
ok  requestbudget 0.473s

$ go test -race -count=1 -v ./...
--- PASS: TestNewRootValidation (0.00s)
--- PASS: TestReserveBounds (0.00s)
--- PASS: TestSettleAndRefund (0.00s)
--- PASS: TestChildCannotExceedParentRemaining (0.00s)
--- PASS: TestSettleBlockedByOpenChildren (0.00s)
--- PASS: TestSettleUsageExceedsReservation (0.00s)
--- PASS: TestTimeoutKeepsQuotaHeldUntilValidSettlement (0.00s)
--- PASS: TestLateSettlementAppliedOnce (0.00s)
--- PASS: TestCancelDoesNotBorrowSiblingReservation (0.00s)
--- PASS: TestCancelBlockedByOutstandingChildren (0.00s)
--- PASS: TestDeadlineRejectsNewActionsButReconcilesExisting (0.00s)
--- PASS: TestIdentifiersAreIndependent (0.00s)
--- PASS: TestConcurrentChildrenConserveQuota (0.01s)
--- PASS: TestConcurrentSettleAppliesOnce (0.00s)
--- PASS: TestInterleavedNestedBranchesWithPartialSettlement (0.00s)
PASS
ok  requestbudget 0.614s

$ go test -vet=all -count=1 ./...
ok  requestbudget 0.473s
```

- **Not covered / explicitly out of scope:** wall-clock timing under real
  concurrency (faked clock used for determinism), persistence across process
  restarts, transport-layer RPC integration, and distributed multi-node
  coordination. The module is an in-process accounting component only.
