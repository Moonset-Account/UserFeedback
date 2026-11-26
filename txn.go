package sparseblockvol

import (
	"bytes"
	"fmt"
	"os"
	"sort"
	"time"
)

func utcNow() time.Time { return time.Now().UTC() }

// Txn is a read-modify-write transaction against one volume's head.
// Payload blocks are staged in a private directory; nothing is visible to
// readers until Commit finishes its atomic metadata switch.
type Txn struct {
	store      *Store
	vol        *storeVolume
	id         int64
	baseID     int64
	size       int64
	entries    map[int64]Entry
	changes    map[int64]changeOp
	staged     map[string]bool
	stagedData map[string][]byte
	snapshot   string
	done       bool
}

// Begin starts a write transaction. A unique id is allocated and a private
// staging directory created immediately.
func (v *Volume) Begin() (*Txn, error) {
	v.store.mu.Lock()
	defer v.store.mu.Unlock()
	if err := v.store.checkOpen(); err != nil {
		return nil, err
	}
	head := v.vol.versions[v.vol.headID]
	id := v.store.nextTxnID
	v.store.nextTxnID++
	if err := v.store.persistMeta(); err != nil {
		v.store.nextTxnID--
		return nil, err
	}
	if err := os.MkdirAll(txnBlockDir(v.store.root, id), 0o755); err != nil {
		return nil, err
	}
	entries := make(map[int64]Entry, len(head.entries))
	for lba, e := range head.entries {
		entries[lba] = e
	}
	return &Txn{
		store:      v.store,
		vol:        v.vol,
		id:         id,
		baseID:     head.id,
		size:       head.size,
		entries:    entries,
		changes:    map[int64]changeOp{},
		staged:     map[string]bool{},
		stagedData: map[string][]byte{},
	}, nil
}

// ID returns the transaction id.
func (t *Txn) ID() int64 { return t.id }

// SetSnapshot asks Commit to publish the new version under this snapshot name.
func (t *Txn) SetSnapshot(name string) error {
	t.store.mu.Lock()
	defer t.store.mu.Unlock()
	if t.done {
		return ErrTxClosed
	}
	if name == "" {
		return fmt.Errorf("%w: empty snapshot name", ErrBadArgument)
	}
	if existing, ok := t.vol.snapshots[name]; ok {
		return fmt.Errorf("%w: name %q already points at version %d", ErrSnapshotName, name, existing)
	}
	t.snapshot = name
	return nil
}

// materializeBlock returns the current bs-byte contents of logical block lba
// within the transaction's working view, including blocks still staged in the
// transaction's private area.
func (t *Txn) materializeBlock(lba int64) []byte {
	bs := t.vol.blockSize
	buf := make([]byte, bs)
	if e, ok := t.entries[lba]; ok && e.Kind == KindMapped {
		if data, ok := t.stagedData[e.BlockID]; ok {
			copy(buf, data)
			return buf
		}
		if data, err := t.store.readBlock(e.BlockID); err == nil {
			copy(buf, data)
		}
	}
	return buf
}

// overlay applies bytes from src into dst at the block-relative offset.
func overlay(dst, src []byte, rel int) {
	copy(dst[rel:rel+len(src)], src)
}

// recordBlock stores the resulting bytes of one logical block, staging a new
// content block when needed, and records the mapping change.
func (t *Txn) recordBlock(lba int64, data []byte) error {
	if bytes.Count(data, []byte{0}) == len(data) {
		t.entries[lba] = Entry{Kind: KindZero}
		t.changes[lba] = changeOp{LBA: lba, Kind: KindZero}
		return nil
	}
	id := hashBlock(data)
	t.entries[lba] = Entry{Kind: KindMapped, BlockID: id}
	t.changes[lba] = changeOp{LBA: lba, Kind: KindMapped, BlockID: id}
	if t.staged[id] || t.store.blockExists(id) {
		return nil
	}
	if err := t.store.stageBlock(t.id, id, data); err != nil {
		return err
	}
	t.staged[id] = true
	t.stagedData[id] = append([]byte(nil), data...)
	return nil
}

// grow ensures the working volume covers [0, end).
func (t *Txn) grow(end int64) {
	bs := int64(t.vol.blockSize)
	need := ((end + bs - 1) / bs) * bs
	if need > t.size {
		t.size = need
	}
}

// WriteAt writes data at byte offset off, growing the volume when needed.
// Writes may start or end mid-block; affected blocks are read-modified-written
// against the transaction view, so a writer covering only the middle of a
// block preserves its unmodified edges.
func (t *Txn) WriteAt(data []byte, off int64) error {
	t.store.mu.Lock()
	defer t.store.mu.Unlock()
	if t.done {
		return ErrTxClosed
	}
	if off < 0 {
		return fmt.Errorf("%w: negative offset", ErrBadArgument)
	}
	if len(data) == 0 {
		return fmt.Errorf("%w: empty write", ErrBadArgument)
	}
	return t.writeBytes(data, off)
}

// WriteZero writes len(p) zero bytes at off. A fully covered logical block is
// recorded as an explicit KindZero segment rather than a content block.
func (t *Txn) WriteZero(p []byte, off int64) error {
	t.store.mu.Lock()
	defer t.store.mu.Unlock()
	if t.done {
		return ErrTxClosed
	}
	if off < 0 {
		return fmt.Errorf("%w: negative offset", ErrBadArgument)
	}
	if len(p) == 0 {
		return fmt.Errorf("%w: empty write", ErrBadArgument)
	}
	return t.writeBytes(make([]byte, len(p)), off)
}

func (t *Txn) writeBytes(data []byte, off int64) error {
	bs := int64(t.vol.blockSize)
	end := off + int64(len(data))
	t.grow(end)
	pos := off
	for len(data) > 0 {
		lba := pos / bs
		rel := pos - lba*bs
		take := bs - rel
		if take > int64(len(data)) {
			take = int64(len(data))
		}
		block := t.materializeBlock(lba)
		overlay(block, data[:take], int(rel))
		if err := t.recordBlock(lba, block); err != nil {
			return err
		}
		pos += take
		data = data[take:]
	}
	return nil
}

// PunchHole deallocates [off, off+length). Fully covered logical blocks become
// KindHole entries (no physical block); blocks touched only at their edges are
// read-modified-written with zero bytes at block granularity.
func (t *Txn) PunchHole(off, length int64) error {
	t.store.mu.Lock()
	defer t.store.mu.Unlock()
	if t.done {
		return ErrTxClosed
	}
	if off < 0 || length < 0 {
		return fmt.Errorf("%w: negative range", ErrBadArgument)
	}
	if length == 0 {
		return nil
	}
	bs := int64(t.vol.blockSize)
	end := off + length
	// Punching beyond the current volume extends the tracked range so the
	// new holes are observable via Size and Extents.
	t.grow(end)
	first := off / bs
	last := (end - 1) / bs
	for lba := first; lba <= last; lba++ {
		blockStart := lba * bs
		blockEnd := blockStart + bs
		rangeStart := max64(off, blockStart)
		rangeEnd := min64(end, blockEnd)
		if rangeStart == blockStart && rangeEnd == blockEnd {
			delete(t.entries, lba)
			t.changes[lba] = changeOp{LBA: lba, Kind: KindHole}
			continue
		}
		block := t.materializeBlock(lba)
		for i := rangeStart - blockStart; i < rangeEnd-blockStart; i++ {
			block[i] = 0
		}
		if err := t.recordBlock(lba, block); err != nil {
			return err
		}
	}
	return nil
}

func max64(a, b int64) int64 {
	if a > b {
		return a
	}
	return b
}

func min64(a, b int64) int64 {
	if a < b {
		return a
	}
	return b
}

// Abort discards the transaction and removes all staged payloads.
func (t *Txn) Abort() error {
	t.store.mu.Lock()
	defer t.store.mu.Unlock()
	if t.done {
		return ErrTxClosed
	}
	t.done = true
	return os.RemoveAll(txnDir(t.store.root, t.id))
}

// Commit publishes a new immutable version from the transaction view.
// Staged blocks are promoted into the shared content store, then metadata is
// replaced atomically; previously published versions stay untouched and
// readable throughout.
func (t *Txn) Commit() (*View, error) {
	t.store.mu.Lock()
	defer t.store.mu.Unlock()
	if t.done {
		return nil, ErrTxClosed
	}
	if err := t.store.checkOpen(); err != nil {
		return nil, err
	}
	if t.snapshot != "" {
		if _, ok := t.vol.snapshots[t.snapshot]; ok {
			return nil, fmt.Errorf("%w: snapshot %q", ErrSnapshotName, t.snapshot)
		}
	}

	// 1. Persist the staged description next to the payload blocks.
	changes := make([]changeOp, 0, len(t.changes))
	for _, c := range t.changes {
		changes = append(changes, c)
	}
	sort.Slice(changes, func(i, j int) bool { return changes[i].LBA < changes[j].LBA })
	stagedIDs := make([]string, 0, len(t.staged))
	for id := range t.staged {
		stagedIDs = append(stagedIDs, id)
	}
	sort.Strings(stagedIDs)
	pm := diskPending{
		TxnID:     t.id,
		VolumeID:  t.vol.id,
		NewSize:   t.size,
		Snapshot:  t.snapshot,
		Changes:   changes,
		StagedIDs: stagedIDs,
	}
	if err := writeAtomicJSON(txnMetaPath(t.store.root, t.id), &pm); err != nil {
		return nil, err
	}
	if err := t.store.hitCrash("txn.after_stage"); err != nil {
		t.done = true
		return nil, err
	}

	// 2. Promote payload blocks into the shared, content-addressed store.
	for _, id := range stagedIDs {
		if err := promoteStagedBlock(t.store.root, t.id, id); err != nil {
			return nil, err
		}
	}

	// 3. Build and publish the new immutable version.
	newVer := t.publishVersion(changes)
	if err := t.store.persistMeta(); err != nil {
		return nil, err
	}
	if err := t.store.hitCrash("txn.after_meta"); err != nil {
		// Metadata is durable and complete; only staging cleanup was lost.
		t.done = true
		_ = os.RemoveAll(txnDir(t.store.root, t.id))
		return nil, err
	}

	t.done = true
	if err := os.RemoveAll(txnDir(t.store.root, t.id)); err != nil {
		return nil, err
	}
	return &View{store: t.store, vol: t.vol, versionID: newVer.id, ver: newVer}, nil
}

func (t *Txn) publishVersion(changes []changeOp) *volVersion {
	newID := t.vol.nextVerID
	t.vol.nextVerID++
	vv := &volVersion{
		id:        newID,
		parentID:  t.vol.headID,
		createdAt: utcNow(),
		size:      t.size,
		entries:   map[int64]Entry{},
	}
	for lba, e := range t.entries {
		vv.entries[lba] = e
	}
	for _, c := range changes {
		// t.entries already reflects every change, including hole deletes;
		// the loop over changes exists to keep the rule explicit.
		if c.Kind == KindHole {
			delete(vv.entries, c.LBA)
		} else {
			vv.entries[c.LBA] = Entry{Kind: c.Kind, BlockID: c.BlockID}
		}
	}
	t.vol.versions[newID] = vv
	t.vol.headID = newID
	if t.snapshot != "" {
		t.vol.snapshots[t.snapshot] = newID
	}
	return vv
}
