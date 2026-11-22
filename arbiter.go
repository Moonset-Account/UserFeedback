// Package ssiarbiter implements a serializable commit arbiter for an
// in-memory relational store.
//
// The arbiter maintains the direct serialization graph (DSG) of the
// transaction history. Three kinds of dependency edges are tracked:
//
//   - write-read (WR, "read dependency"): a committed writer's version is
//     observed by a later reader;
//   - read-write (RW, "anti-dependency"): a later writer changes the result a
//     reader observed (including phantoms and empty reads);
//   - write-write (WW): two writers cover the same key, ordered by commit
//     sequence.
//
// A candidate commit is rejected exactly when accepting it would close a
// cycle in this graph. The rejecting decision carries the witness cycle with
// transaction IDs, key/range scope and dependency kind for every edge.
package ssiarbiter

import (
	"errors"
	"fmt"
	"sort"
	"sync"
)

// TxnID identifies a transaction.
type TxnID string

// DepKind is one of the three serialization-graph dependency kinds.
type DepKind string

const (
	// DepRead is a writer -> reader (write-read) dependency.
	DepRead DepKind = "read-dependency"
	// DepAnti is a reader -> writer (read-write) anti-dependency.
	DepAnti DepKind = "anti-dependency"
	// DepWrite is a writer -> writer (write-write) ordering constraint.
	DepWrite DepKind = "write-dependency"
)

// Row is one materialized row version. A nil row denotes a deletion tombstone.
type Row map[string]string

// ReadSpec describes a read performed at a fixed snapshot.
//
// An empty EndKey selects a point read on StartKey. Otherwise EndKey selects
// the inclusive key range [StartKey, EndKey]. Predicate is a conjunction of
// column equalities applied before projection. Projection lists the columns
// that make up the query result; an empty projection means the whole row.
type ReadSpec struct {
	TxnID      TxnID
	StartKey   string
	EndKey     string
	Predicate  map[string]string
	Projection []string
	Snapshot   uint64
}

// Edge is one witness edge of the serialization graph.
type Edge struct {
	From  TxnID
	To    TxnID
	Kind  DepKind
	Scope string
}

func (e Edge) String() string {
	return fmt.Sprintf("%s --%s[%s]--> %s", e.From, e.Kind, e.Scope, e.To)
}

// ReadRecord keeps a read together with the row versions it actually saw.
// Observed always contains an entry for every key that produced a result row;
// an empty result is recorded as an empty Observed map (empty reads matter
// because later inserts create anti-dependencies).
type ReadRecord struct {
	Spec     ReadSpec
	Observed map[string]Row
}

// WriteIntent is one write of a committing transaction. Before is the row
// version visible at the transaction's snapshot (nil when the key was
// absent/deleted); After is the new row (nil for a deletion).
type WriteIntent struct {
	Key    string
	Before Row
	After  Row
}

// Decision is the verdict for a candidate commit.
type Decision struct {
	Committed bool
	// RejectSeq is the commit sequence the candidate would have received.
	RejectSeq uint64
	// Cycle is the witness cycle when the candidate was rejected. It starts
	// and ends at the candidate transaction.
	Cycle []Edge
}

// GCStats summarizes one garbage-collection pass.
type GCStats struct {
	Watermark         uint64
	ReclaimedTxns     int
	CompactedKeys     int
	CompactedVersions int
}

type txnState int

const (
	stateActive txnState = iota
	stateCommitted
	stateAborted
)

type txnInfo struct {
	id       TxnID
	snapshot uint64
	state    txnState
	// endSeq is the commit sequence for committed transactions, zero otherwise.
	endSeq uint64
	reads  []ReadRecord
	writes []WriteIntent
}

type version struct {
	seq uint64
	row Row // nil means a deletion tombstone
}

// Arbiter is the serializable commit arbiter. It is safe for concurrent use.
type Arbiter struct {
	mu sync.Mutex

	commitSeq uint64
	txns      map[TxnID]*txnInfo
	order     []TxnID // registration order, keeps iteration deterministic

	// keyVersions holds, per key, the committed version chain in commit order.
	keyVersions map[string][]version

	edges []Edge
}

// New creates an arbiter seeded with initial rows, which receive sequence 0.
func New(seed map[string]Row) *Arbiter {
	a := &Arbiter{
		txns:        map[TxnID]*txnInfo{},
		keyVersions: map[string][]version{},
	}
	keys := make([]string, 0, len(seed))
	for k := range seed {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		a.keyVersions[k] = []version{{seq: 0, row: cloneRow(seed[k])}}
	}
	return a
}

// Current returns the latest allocated commit sequence.
func (a *Arbiter) Current() uint64 {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.commitSeq
}

// Begin registers an active transaction taking the given snapshot. Active
// transactions are allowed to hold snapshots older than already finished
// transactions.
func (a *Arbiter) Begin(id TxnID, snapshot uint64) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	if id == "" {
		return errors.New("ssiarbiter: transaction id must not be empty")
	}
	if _, ok := a.txns[id]; ok {
		return fmt.Errorf("ssiarbiter: transaction %q already exists", id)
	}
	if snapshot > a.commitSeq {
		return fmt.Errorf("ssiarbiter: snapshot %d is ahead of commit sequence %d", snapshot, a.commitSeq)
	}
	a.txns[id] = &txnInfo{id: id, snapshot: snapshot, state: stateActive}
	a.order = append(a.order, id)
	return nil
}

// Read executes and records a read. The returned map contains one entry per
// key producing a result row; an empty map records an empty read.
func (a *Arbiter) Read(spec ReadSpec) (map[string]Row, error) {
	a.mu.Lock()
	defer a.mu.Unlock()

	t, err := a.activeTxn(spec.TxnID)
	if err != nil {
		return nil, err
	}
	if spec.StartKey == "" {
		return nil, errors.New("ssiarbiter: read requires a non-empty start key")
	}
	if spec.EndKey != "" && spec.EndKey < spec.StartKey {
		return nil, fmt.Errorf("ssiarbiter: invalid range [%q, %q]", spec.StartKey, spec.EndKey)
	}

	rec := ReadRecord{
		Spec: ReadSpec{
			TxnID:      spec.TxnID,
			StartKey:   spec.StartKey,
			EndKey:     spec.EndKey,
			Predicate:  clonePredicate(spec.Predicate),
			Projection: cloneProjection(spec.Projection),
			Snapshot:   spec.Snapshot,
		},
		Observed: map[string]Row{},
	}

	keys := a.scopeKeys(spec.StartKey, spec.EndKey)
	for _, k := range keys {
		row, ok := a.visible(k, spec.Snapshot)
		if !ok {
			continue // no live row at this snapshot: empty/phantom slot
		}
		if !predicateMatches(row, spec.Predicate) {
			continue
		}
		rec.Observed[k] = projectRow(row, spec.Projection)

		// Writer -> reader dependency: the read depends on the version it saw.
		if v, ok := a.visibleVersion(k, spec.Snapshot); ok && v.seq > 0 {
			writer := a.writerAt(v.seq)
			if writer != "" && writer != t.id {
				a.addEdge(Edge{
					From:  writer,
					To:    t.id,
					Kind:  DepRead,
					Scope: scopeText(spec.StartKey, spec.EndKey),
				})
			}
		}
	}

	t.reads = append(t.reads, rec)
	return cloneObserved(rec.Observed), nil
}

// Commit tries to commit a transaction with its writes. A rejected candidate
// stays uncommitted and its edges are not installed; callers must then call
// Abort.
func (a *Arbiter) Commit(id TxnID, writes []WriteIntent) (Decision, error) {
	a.mu.Lock()
	defer a.mu.Unlock()

	t, err := a.activeTxn(id)
	if err != nil {
		return Decision{}, err
	}
	seen := map[string]bool{}
	for _, w := range writes {
		if w.Key == "" {
			return Decision{}, errors.New("ssiarbiter: write requires a non-empty key")
		}
		if seen[w.Key] {
			return Decision{}, fmt.Errorf("ssiarbiter: transaction %q writes key %q more than once", id, w.Key)
		}
		seen[w.Key] = true
	}

	seq := a.commitSeq + 1
	extra := a.candidateEdges(t, writes)

	decision := Decision{RejectSeq: seq}
	if cycle := a.findCycle(id, extra); cycle != nil {
		decision.Cycle = cycle
		return decision, nil
	}

	// Accept: install the edges and append the new versions.
	a.edges = append(a.edges, extra...)
	for _, w := range writes {
		stored := WriteIntent{Key: w.Key, Before: cloneRow(w.Before), After: cloneRow(w.After)}
		t.writes = append(t.writes, stored)
		a.keyVersions[w.Key] = append(a.keyVersions[w.Key], version{seq: seq, row: cloneRow(w.After)})
	}
	t.state = stateCommitted
	t.endSeq = seq
	a.commitSeq = seq
	decision.Committed = true
	return decision, nil
}

// Abort terminates a transaction without installing any version or edge.
func (a *Arbiter) Abort(id TxnID) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	t, ok := a.txns[id]
	if !ok {
		return fmt.Errorf("ssiarbiter: unknown transaction %q", id)
	}
	if t.state != stateActive {
		return fmt.Errorf("ssiarbiter: transaction %q is not active", id)
	}
	t.state = stateAborted
	return nil
}

// Edges returns a snapshot of all installed graph edges in insertion order.
func (a *Arbiter) Edges() []Edge {
	a.mu.Lock()
	defer a.mu.Unlock()
	out := make([]Edge, len(a.edges))
	copy(out, a.edges)
	return out
}

// Reads returns the recorded reads of a transaction (including empty reads).
func (a *Arbiter) Reads(id TxnID) ([]ReadRecord, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	t, ok := a.txns[id]
	if !ok {
		return nil, fmt.Errorf("ssiarbiter: unknown transaction %q", id)
	}
	out := make([]ReadRecord, len(t.reads))
	for i, r := range t.reads {
		out[i] = ReadRecord{Spec: r.Spec, Observed: cloneObserved(r.Observed)}
	}
	return out, nil
}

// Collect reclaims row payloads of finished transactions below the oldest
// active snapshot and compacts per-key version chains to that watermark.
// Graph nodes, edges (with their witnesses) and read records are retained, so
// an old active transaction can still be adjudicated afterwards and replaying
// the same history still yields the same witness.
func (a *Arbiter) Collect() GCStats {
	a.mu.Lock()
	defer a.mu.Unlock()

	// Watermark: oldest active snapshot; when no active transaction exists it
	// is the current latest commit sequence, so fully obsolete payloads can be
	// reclaimed.
	stats := GCStats{Watermark: a.commitSeq}
	for _, id := range a.order {
		if t := a.txns[id]; t.state == stateActive && t.snapshot < stats.Watermark {
			stats.Watermark = t.snapshot
		}
	}

	for _, t := range a.txns {
		if t.state == stateActive {
			continue
		}
		if t.endSeq > 0 && t.endSeq <= stats.Watermark {
			t.writes = nil
			stats.ReclaimedTxns++
		}
	}

	for key, chain := range a.keyVersions {
		// Keep every version at/above the watermark plus the latest version
		// below it as the compacted base.
		base := -1
		for i, v := range chain {
			if v.seq <= stats.Watermark {
				base = i
			}
		}
		keepFrom := 0
		if base > 0 {
			keepFrom = base
			stats.CompactedVersions += base
		}
		if keepFrom > 0 {
			compacted := make([]version, 0, len(chain)-keepFrom+1)
			compacted = append(compacted, chain[keepFrom:]...)
			a.keyVersions[key] = compacted
			stats.CompactedKeys++
		}
	}
	return stats
}

// candidateEdges builds the edges a commit would install.
func (a *Arbiter) candidateEdges(c *txnInfo, writes []WriteIntent) []Edge {
	extra := make([]Edge, 0)

	// WW edges: previous committed writers of the same key precede the
	// candidate, in commit order.
	for _, w := range writes {
		if chain, ok := a.keyVersions[w.Key]; ok {
			last := chain[len(chain)-1]
			if last.seq > 0 {
				if writer := a.writerAt(last.seq); writer != "" && writer != c.id {
					extra = append(extra, Edge{
						From:  writer,
						To:    c.id,
						Kind:  DepWrite,
						Scope: "key=" + w.Key,
					})
				}
			}
		}
	}

	// RW anti-dependencies: any recorded read (active or finished transaction)
	// whose result the candidate's write changes points at the candidate.
	// Reads of aborted transactions are ignored: an aborted transaction is not
	// part of the serialization history.
	for _, readerID := range a.order {
		reader := a.txns[readerID]
		if reader == nil || reader.id == c.id || reader.state == stateAborted {
			continue
		}
		for _, rr := range reader.reads {
			for _, w := range writes {
				if !keyInScope(w.Key, rr.Spec.StartKey, rr.Spec.EndKey) {
					continue
				}
				if a.readWouldSee(rr, w) {
					continue // same query result: no anti-dependency
				}
				extra = append(extra, Edge{
					From:  reader.id,
					To:    c.id,
					Kind:  DepAnti,
					Scope: scopeText(rr.Spec.StartKey, rr.Spec.EndKey),
				})
			}
		}
	}
	return extra
}

// readWouldSee reports whether the read record already observes exactly the
// candidate write's projected result for the write's key. The query result
// changes when a row appears (insert/phantom), disappears (delete or a write
// making the predicate false), or changes a projected column.
func (a *Arbiter) readWouldSee(rr ReadRecord, w WriteIntent) bool {
	if w.After == nil || !predicateMatches(w.After, rr.Spec.Predicate) {
		// The key produces no result row after the write.
		_, present := rr.Observed[w.Key]
		return !present
	}
	current, present := rr.Observed[w.Key]
	if !present {
		return false // insert into an empty/filtered slot: phantom
	}
	return rowsEqual(current, projectRow(w.After, rr.Spec.Projection))
}

// findCycle searches deterministically for a path from start back to start
// using installed edges plus the candidate extra edges.
func (a *Arbiter) findCycle(start TxnID, extra []Edge) []Edge {
	adj := map[TxnID][]Edge{}
	for _, e := range a.edges {
		adj[e.From] = append(adj[e.From], e)
	}
	for _, e := range extra {
		adj[e.From] = append(adj[e.From], e)
	}

	visiting := map[TxnID]bool{start: true}
	stack := make([]Edge, 0)

	var dfs func(u TxnID) []Edge
	dfs = func(u TxnID) []Edge {
		for _, e := range adj[u] {
			if e.To == start {
				cycle := make([]Edge, 0, len(stack)+1)
				cycle = append(cycle, stack...)
				return append(cycle, e)
			}
			if visiting[e.To] {
				continue
			}
			visiting[e.To] = true
			stack = append(stack, e)
			if found := dfs(e.To); found != nil {
				return found
			}
			stack = stack[:len(stack)-1]
		}
		return nil
	}
	return dfs(start)
}

func (a *Arbiter) addEdge(e Edge) {
	for _, existing := range a.edges {
		if existing == e {
			return
		}
	}
	a.edges = append(a.edges, e)
}

func (a *Arbiter) activeTxn(id TxnID) (*txnInfo, error) {
	t, ok := a.txns[id]
	if !ok {
		return nil, fmt.Errorf("ssiarbiter: unknown transaction %q", id)
	}
	if t.state != stateActive {
		return nil, fmt.Errorf("ssiarbiter: transaction %q is not active", id)
	}
	return t, nil
}

func (a *Arbiter) scopeKeys(start, end string) []string {
	keys := make([]string, 0)
	for k := range a.keyVersions {
		if keyInScope(k, start, end) {
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)
	return keys
}

func keyInScope(k, start, end string) bool {
	if end == "" {
		return k == start
	}
	return k >= start && k <= end
}

// visible returns the live row for a key at a snapshot. Tombstones and
// pre-snapshot absence report ok=false.
func (a *Arbiter) visible(key string, snapshot uint64) (Row, bool) {
	v, ok := a.visibleVersion(key, snapshot)
	if !ok || v.row == nil {
		return nil, false
	}
	return v.row, true
}

func (a *Arbiter) visibleVersion(key string, snapshot uint64) (version, bool) {
	chain, ok := a.keyVersions[key]
	if !ok {
		return version{}, false
	}
	var found version
	hit := false
	for _, v := range chain {
		if v.seq <= snapshot {
			found, hit = v, true
		} else {
			break
		}
	}
	return found, hit
}

// writerAt maps a commit sequence back to its transaction.
func (a *Arbiter) writerAt(seq uint64) TxnID {
	for _, id := range a.order {
		if t := a.txns[id]; t != nil && t.endSeq == seq {
			return id
		}
	}
	return ""
}

func predicateMatches(row Row, predicate map[string]string) bool {
	for col, want := range predicate {
		if row[col] != want {
			return false
		}
	}
	return true
}

func projectRow(row Row, projection []string) Row {
	if len(projection) == 0 {
		return cloneRow(row)
	}
	out := make(Row, len(projection))
	for _, col := range projection {
		if v, ok := row[col]; ok {
			out[col] = v
		}
	}
	return out
}

func scopeText(start, end string) string {
	if end == "" {
		return "key=" + start
	}
	return fmt.Sprintf("range=[%s,%s]", start, end)
}

func cloneRow(r Row) Row {
	if r == nil {
		return nil
	}
	out := make(Row, len(r))
	for k, v := range r {
		out[k] = v
	}
	return out
}

func clonePredicate(p map[string]string) map[string]string {
	if p == nil {
		return nil
	}
	out := make(map[string]string, len(p))
	for k, v := range p {
		out[k] = v
	}
	return out
}

func cloneProjection(p []string) []string {
	if p == nil {
		return nil
	}
	out := append([]string(nil), p...)
	sort.Strings(out)
	return out
}

func cloneObserved(in map[string]Row) map[string]Row {
	out := make(map[string]Row, len(in))
	for k, r := range in {
		out[k] = cloneRow(r)
	}
	return out
}

func rowsEqual(a, b Row) bool {
	if len(a) != len(b) {
		return false
	}
	for k, v := range a {
		if b[k] != v {
			return false
		}
	}
	return true
}
