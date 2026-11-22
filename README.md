# forwarder — fixed-bucket next-hop ring with bounded rebalancing

A small Go package implementing the described forwarder behaviour:

* a **fixed number of hash buckets**, each permanently naming one next hop;
* a new connection is hashed to a bucket and **keeps that next hop for its
  lifetime** — bucket moves never migrate live connections;
* a rebalance module that consumes **weight changes, health changes and
  drain** events, moving at most `MaxMovesPerRound` buckets per round;
* integer quota targets computed from weights and the bucket count with the
  largest-remainder method, leftover buckets assigned by descending remainder
  then ascending next-hop ID;
* each greedy move minimises the sum of `|buckets - quota|`; ties go to the
  lower bucket index, then the lexicographically smaller destination ID;
* draining nodes keep their existing connections but receive no new ones;
* buckets that still point at a draining or failed node are skipped by new
  connections until a round moves them;
* connections on hard-failed nodes are **rerouted and reported separately**,
  and a later `Close` of the old handle must not decrement the new target's
  connection count.

Module path: `example.com/forwarder`, Go 1.20, standard library only.

## Public API

```go
type Health int            // Ready, Draining, Failed
type Node struct{ ID string; Weight int; Health Health }
type Config struct{ Buckets, MaxMovesPerRound int }

func New(cfg Config, nodes []Node) (*Ring, error)

// Inputs (all safe for concurrent use):
func (r *Ring) AddNode(n Node) error
func (r *Ring) SetWeight(id string, w int) error
func (r *Ring) SetHealth(id string, h Health) error

// Per round:
func (r *Ring) Rebalance() Round

// Connections:
func (r *Ring) BucketFor(id string) int
func (r *Ring) Connect(id string) (*Conn, error)
func (r *Ring) Conn(id string) (*Conn, bool)
func (r *Ring) Connections() []ConnInfo
func (r *Ring) Snapshot() Snapshot

// Conn handle:
func (c *Conn) ID() string
func (c *Conn) Bucket() int
func (c *Conn) Target() string
func (c *Conn) Stale() bool
func (c *Conn) Close()
```

`Rebalance` returns a `Round` with the moves made (bucket, from, to), the
separately listed reroutes (`From`, `To`, `Bucket`, `Stranded`), the full
bucket map after the round, per-node stats (`Quota`, `Buckets`,
`Connections`, `Difference`), the residual `Deviation`
(`Σ |buckets - quota|`) and a `Stalled` flag for "deviation remains but no
legal move exists" (e.g. every node is failed).

### Semantics worth spelling out

* **Quotas.** `EffectiveWeight` is the configured weight when `Ready` and `0`
  otherwise, so drained and failed nodes have quota `0`. Quotas are
  `floor(B·w/W)` with the leftover buckets handed out by descending integer
  remainder, ties by ascending ID. Exact arithmetic — no floating point.
* **Order inside a round.** Reroutes happen first, against the bucket map
  that was live when the failure was noticed. The bucket moves in the same
  round only affect connections created afterwards. This is why a reroute in
  the trace below lands on a bucket that the same round then reassigns.
* **Tie-breaking.** Every legal move goes from an over-quota to an
  under-quota node and lowers `deviation` by exactly 2, so it is always the
  minimum-cost move; the "fewer moves" criterion is satisfied trivially per
  move. Remaining ties are resolved by lowest bucket index, then smallest
  destination ID.
* **Reroute handles.** A reroute does not mutate the caller's `*Conn`. The
  ring installs a new handle for the same connection ID and marks the old one
  stale. `Close` on a stale handle is a no-op — this is what stops a late
  close of the old route from decrementing the new target's counter. Fetch
  the live handle with `Ring.Conn(id)` after a reroute.
* **Stranded connections.** If no Ready bucket exists at all, the connection
  stays registered, the reroute is reported with `Stranded: true, Bucket: -1`,
  and it is retried on the next round.

## Build and test

Requires Go 1.20.2. No dependencies, no network access.

```sh
go test ./...                 # full suite
go test -race ./...           # same suite under the race detector
go test -run Example -v ./... # the printed round-by-round trace
```

## Verification scope

`go test ./...` and `go test -race ./...` were run in this workspace against
the files here; see the transcript in the final report. Coverage:

| Test | What it pins down |
| --- | --- |
| `TestInitialQuotas` | integer quotas, largest-remainder, ID tie-break, exact division |
| `TestDestinationTieBreakByID` | destination chosen by smaller ID when equally under quota; lowest source bucket |
| `TestDownweightThenFailureSequence` | **required event sequence**: unconverged downweight, then failure; bucket map asserted round by round; exact reroute set; stale-close safety |
| `TestDrainingKeepsConnectionsButMovesBuckets` | drain keeps live connections; new connections skip stale buckets; buckets drift away |
| `TestFailedBucketRejectsNewConnection` | new connections never bind to a bucket still pointing at a failed node |
| `TestStrandedFailure` | all-failed ring: stranded reroute, `Stalled` round, connect rejected |
| `TestGreedyMonotonicProperty` | each move lowers deviation by exactly 2; budget respected; convergence |
| `TestValidationErrors` | constructor/setter/connect guard rails |
| `TestAddNodeMovesBucketsIn` | a new node receives quota over rounds |
| `TestConcurrentConnectCloseRebalance` | concurrent connect/close/health/weight/rebalance under `-race`; per-node counters stay consistent; close-all drains to zero |
| `TestDeterministicReplay` | identical inputs produce identical bucket maps and stats |
| `Example_downweightThenFailure` | executable trace (below) |

Everything above was executed. Nothing in this package performs network,
account or filesystem I/O.

## The required event sequence

Configuration: `Buckets: 12`, `MaxMovesPerRound: 1`, nodes
`alpha w=6`, `bravo w=3`, `charlie w=3`, all `Ready`. Bootstrap quotas are
6/3/3 and the bootstrap layout is contiguous runs:

```
bucket  0  1  2  3  4  5 | 6  7  8 | 9 10 11
        alpha ...        | bravo   | charlie
```

Connections opened before the events (one per listed bucket):
`a1@0, a2@5` on alpha; `c1@9, c2@10, c3@11` on charlie.

1. **Downweight, not yet converged.** `alpha` weight 6 → 1, so weights are
   1/3/3 (total 7). Exact quotas: `12·1/7 = 1 r5`, `12·3/7 = 5 r1` twice →
   11 buckets assigned; the leftover goes to the largest remainder, alpha.
   Quota vector: **alpha 2, bravo 5, charlie 5**.
   Round 1 (budget 1) moves bucket `0 alpha→bravo`. Deviation is still 6.

   ```
   round 1 moves=[{0 alpha bravo}] buckets=[bravo alpha alpha alpha alpha alpha bravo bravo bravo charlie charlie charlie] deviation=6
   ```

   `a1` still rides alpha — it was bound to bucket 0 before the move.

2. **Failure while unconverged.** `charlie` → `Failed` (weight retained,
   effective 0). Only alpha and bravo count now (1/3, total 4), so the quotas
   become **alpha 3, bravo 9, charlie 0**.

   Round 2 first reroutes every charlie connection. With buckets 9–11 still
   pointing at charlie, a scan from each ID's preferred bucket finds the
   first Ready bucket: bucket 0 → bravo. All three move to `bravo@0`; the old
   handles are frozen. Then the budgeted move takes bucket `1 alpha→bravo`.

   ```
   round 2 moves=[{1 alpha bravo}] buckets=[bravo bravo alpha alpha alpha alpha bravo bravo bravo charlie charlie charlie] deviation=8
   rerouted=3 stranded=0 from=charlie to=bravo bucket=0
   conn-00000 now on bravo
   conn-00007 now on bravo
   conn-00016 now on bravo
   ```

   `c1/c2/c3` are the only connections that actually changed route; `a1`,
   `a2` stayed on alpha even though their buckets moved.

3. **Convergence.** One bucket per round, deviation dropping by 2:

   ```
   round 3 moves=[{2 alpha bravo}]      buckets=[bravo bravo bravo alpha alpha alpha bravo bravo bravo charlie charlie charlie] deviation=6
   round 4 moves=[{9 charlie bravo}]    ... deviation=4
   round 5 moves=[{10 charlie bravo}]   ... deviation=2
   round 6 moves=[{11 charlie bravo}]   ... deviation=0
   ```

   Bucket map at the end: alpha owns `3 4 5`, bravo owns everything else.

4. **Stale close.** `c1`'s rerouted live handle sits on bravo. Closing the
   *old* (stale) handle changes bravo's count 3 → 3; closing the live handle
   changes it 3 → 2. The old close cannot double-decrement.

The same trace is executable as `Example_downweightThenFailure`.
