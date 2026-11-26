package sparseblockvol

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

type volVersion struct {
	id        int64
	parentID  int64
	createdAt time.Time
	size      int64
	entries   map[int64]Entry
}

type storeVolume struct {
	id        string
	blockSize int
	headID    int64
	nextVerID int64
	snapshots map[string]int64
	versions  map[int64]*volVersion
}

// Store is a directory-backed collection of sparse block volumes.
type Store struct {
	root   string
	mu     sync.Mutex
	closed bool

	nextTxnID int64
	volumes   map[string]*storeVolume

	lastReport *RecoveryReport

	// crashPoint is a test-only injection. When a named point in the commit
	// or merge flow is reached the operation stops with ErrInjectedCrash and
	// leaves the on-disk state exactly as it was at that point, so a
	// subsequent Open exercises the real recovery code.
	crashPoint string
}

// ErrInjectedCrash is returned when a configured crash point is reached.
var ErrInjectedCrash = fmt.Errorf("sparseblockvol: injected crash")

// Create opens a store at root, initializing an empty one when root is empty.
func Create(root string) (*Store, error) {
	if root == "" {
		return nil, fmt.Errorf("%w: empty root path", ErrBadArgument)
	}
	for _, d := range []string{blocksDir(root), pendingDir(root), mergeDir(root)} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			return nil, err
		}
	}
	if fileExists(filepath.Join(root, "meta.json")) {
		return nil, fmt.Errorf("%w: meta.json already exists", ErrExists)
	}
	s := &Store{
		root:      root,
		nextTxnID: 1,
		volumes:   map[string]*storeVolume{},
	}
	if err := s.persistMeta(); err != nil {
		return nil, err
	}
	s.lastReport = &RecoveryReport{}
	return s, nil
}

// Open opens an existing store and runs crash recovery. The returned report
// describes every repair that was performed.
func Open(root string) (*Store, RecoveryReport, error) {
	if root == "" {
		return nil, RecoveryReport{}, fmt.Errorf("%w: empty root path", ErrBadArgument)
	}
	var dm diskMeta
	if err := readJSON(filepath.Join(root, "meta.json"), &dm); err != nil {
		return nil, RecoveryReport{}, err
	}
	s := &Store{
		root:      root,
		nextTxnID: dm.NextTxnID,
		volumes:   map[string]*storeVolume{},
	}
	if err := s.loadMeta(&dm); err != nil {
		return nil, RecoveryReport{}, err
	}
	report, err := s.recover()
	if err != nil {
		return nil, RecoveryReport{}, err
	}
	s.lastReport = &report
	return s, report, nil
}

// LastRecoveryReport returns the report produced by the most recent Open.
func (s *Store) LastRecoveryReport() (RecoveryReport, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.checkOpen(); err != nil {
		return RecoveryReport{}, err
	}
	if s.lastReport == nil {
		return RecoveryReport{}, nil
	}
	return *s.lastReport, nil
}

// Close releases the store. It does not remove any on-disk state.
func (s *Store) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return ErrClosed
	}
	s.closed = true
	return nil
}

func (s *Store) checkOpen() error {
	if s.closed {
		return ErrClosed
	}
	return nil
}

// SetCrashPoint configures a one-shot injected crash used by tests.
func (s *Store) SetCrashPoint(point string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.crashPoint = point
}

func (s *Store) hitCrash(point string) error {
	if s.crashPoint == point {
		s.crashPoint = ""
		return ErrInjectedCrash
	}
	return nil
}

func (s *Store) loadMeta(dm *diskMeta) error {
	s.nextTxnID = dm.NextTxnID
	if dm.NextVersion == nil {
		dm.NextVersion = map[string]int64{}
	}
	s.volumes = map[string]*storeVolume{}
	for id, dv := range dm.Volumes {
		v := &storeVolume{
			id:        id,
			blockSize: dv.BlockSize,
			headID:    dv.HeadID,
			nextVerID: dm.NextVersion[id],
			snapshots: map[string]int64{},
			versions:  map[int64]*volVersion{},
		}
		if v.nextVerID == 0 {
			v.nextVerID = dv.HeadID + 1
		}
		for name, vid := range dv.Snapshots {
			v.snapshots[name] = vid
		}
		for vid, dver := range dv.Versions {
			created, err := time.Parse(time.RFC3339Nano, dver.CreatedAt)
			if err != nil {
				return fmt.Errorf("volume %s version %d: %w", id, vid, err)
			}
			vv := &volVersion{
				id:        dver.ID,
				parentID:  dver.ParentID,
				createdAt: created,
				size:      dver.Size,
				entries:   map[int64]Entry{},
			}
			for lba, de := range dver.Entries {
				ent, err := decodeEntry(de)
				if err != nil {
					return fmt.Errorf("volume %s version %d lba %d: %w", id, vid, lba, err)
				}
				vv.entries[lba] = ent
			}
			v.versions[vid] = vv
		}
		if _, ok := v.versions[v.headID]; !ok {
			return fmt.Errorf("volume %s: head version %d missing", id, v.headID)
		}
		s.volumes[id] = v
	}
	return nil
}

func decodeEntry(de diskEntry) (Entry, error) {
	switch EntryKind(de.Kind) {
	case KindMapped:
		if de.BlockID == "" {
			return Entry{}, fmt.Errorf("%w: mapped entry without block id", ErrBadArgument)
		}
		return Entry{Kind: KindMapped, BlockID: de.BlockID}, nil
	case KindZero:
		return Entry{Kind: KindZero}, nil
	case KindHole:
		return Entry{Kind: KindHole}, nil
	default:
		return Entry{}, fmt.Errorf("%w: unknown entry kind %q", ErrBadArgument, de.Kind)
	}
}

func encodeEntry(e Entry) diskEntry {
	return diskEntry{Kind: string(e.Kind), BlockID: e.BlockID}
}

func (s *Store) toDiskMeta() diskMeta {
	dm := diskMeta{
		NextTxnID:   s.nextTxnID,
		NextVersion: map[string]int64{},
		Volumes:     map[string]diskVolume{},
	}
	for id, v := range s.volumes {
		dv := diskVolume{
			ID:        id,
			BlockSize: v.blockSize,
			HeadID:    v.headID,
			Snapshots: map[string]int64{},
			Versions:  map[int64]diskVersion{},
		}
		for name, vid := range v.snapshots {
			dv.Snapshots[name] = vid
		}
		for vid, vv := range v.versions {
			dver := diskVersion{
				ID:        vv.id,
				ParentID:  vv.parentID,
				CreatedAt: vv.createdAt.Format(time.RFC3339Nano),
				Size:      vv.size,
				Entries:   map[int64]diskEntry{},
			}
			for lba, e := range vv.entries {
				dver.Entries[lba] = encodeEntry(e)
			}
			dv.Versions[vid] = dver
		}
		dm.Volumes[id] = dv
		dm.NextVersion[id] = v.nextVerID
	}
	return dm
}

func (s *Store) persistMeta() error {
	return writeAtomicJSON(s.metaPath(), s.toDiskMeta())
}

// ---- physical blocks -------------------------------------------------------

func hashBlock(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func (s *Store) blockExists(id string) bool {
	return fileExists(blockPath(s.root, id))
}

// putBlock writes a physical block. Existing content blocks are shared, so a
// duplicate id simply keeps the single on-disk copy.
func (s *Store) putBlock(id string, data []byte) error {
	if hashBlock(data) != id {
		return fmt.Errorf("%w: block content does not hash to %s", ErrBadArgument, id)
	}
	path := blockPath(s.root, id)
	if fileExists(path) {
		return nil
	}
	return writeFileAtomic(path, data, 0o644)
}

func (s *Store) readBlock(id string) ([]byte, error) {
	data, err := os.ReadFile(blockPath(s.root, id))
	if err != nil {
		return nil, fmt.Errorf("reading block %s: %w", id, err)
	}
	return data, nil
}

func stageBlockPath(root string, txnID int64, id string) string {
	return filepath.Join(txnBlockDir(root, txnID), id)
}

// stageBlock writes payload into the private staging area of a transaction.
func (s *Store) stageBlock(txnID int64, id string, data []byte) error {
	path := stageBlockPath(s.root, txnID, id)
	if fileExists(path) {
		return nil
	}
	return writeFileAtomic(path, data, 0o644)
}

// promote moves a staged block into the shared block directory. It is
// idempotent: a block promoted by an earlier attempt (or shared with another
// writer) is left in place.
func promoteStagedBlock(root string, txnID int64, id string) error {
	src := stageBlockPath(root, txnID, id)
	dst := blockPath(root, id)
	if fileExists(dst) {
		_ = os.Remove(src)
		return nil
	}
	if !fileExists(src) {
		// Already promoted in an earlier attempt.
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return err
	}
	if err := os.Rename(src, dst); err != nil {
		return err
	}
	fsyncBestEffort(filepath.Dir(dst))
	return nil
}

func mergeStageDir(root, volID string) string {
	return filepath.Join(mergeDir(root), volID, "blocks")
}

func (s *Store) stageMergeBlock(volID, id string, data []byte) error {
	path := filepath.Join(mergeStageDir(s.root, volID), id)
	if fileExists(path) || s.blockExists(id) {
		return nil
	}
	return writeFileAtomic(path, data, 0o644)
}

func promoteMergeBlock(root, volID, id string) error {
	src := filepath.Join(mergeStageDir(root, volID), id)
	dst := blockPath(root, id)
	if fileExists(dst) {
		_ = os.Remove(src)
		return nil
	}
	if !fileExists(src) {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return err
	}
	if err := os.Rename(src, dst); err != nil {
		return err
	}
	fsyncBestEffort(filepath.Dir(dst))
	return nil
}

// ---- reference counting ----------------------------------------------------

// recomputeRefs walks every reachable version of every volume and counts how
// many logical blocks reference each physical block id.
func (s *Store) recomputeRefs() map[string]int {
	refs := map[string]int{}
	for _, v := range s.volumes {
		for _, vv := range v.versions {
			for _, e := range vv.entries {
				if e.Kind == KindMapped {
					refs[e.BlockID]++
				}
			}
		}
	}
	return refs
}

// listDiskBlocks returns the ids of every physical block file on disk.
func listDiskBlocks(root string) ([]string, error) {
	base := blocksDir(root)
	var ids []string
	err := filepath.Walk(base, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			if os.IsNotExist(err) {
				return nil
			}
			return err
		}
		if info.IsDir() {
			return nil
		}
		ids = append(ids, info.Name())
		return nil
	})
	if err != nil {
		return nil, err
	}
	sort.Strings(ids)
	return ids, nil
}

// audit compares committed references with the block directory.
func (s *Store) audit() (orphans, missing []string, err error) {
	refs := s.recomputeRefs()
	disk, err := listDiskBlocks(s.root)
	if err != nil {
		return nil, nil, err
	}
	onDisk := map[string]bool{}
	for _, id := range disk {
		onDisk[id] = true
		if refs[id] == 0 {
			orphans = append(orphans, id)
		}
	}
	for id := range refs {
		if !onDisk[id] {
			missing = append(missing, id)
		}
	}
	sort.Strings(orphans)
	sort.Strings(missing)
	return orphans, missing, nil
}

// Audit returns physical blocks that are unreachable (safe to reclaim) and
// referenced blocks whose files are missing.
func (s *Store) Audit() (orphans, missing []string, err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.checkOpen(); err != nil {
		return nil, nil, err
	}
	return s.audit()
}

// Reclaim deletes unreachable physical blocks and returns their ids. Zero
// segments and holes have no physical block and therefore never appear.
func (s *Store) Reclaim() ([]string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.checkOpen(); err != nil {
		return nil, err
	}
	orphans, missing, err := s.audit()
	if err != nil {
		return nil, err
	}
	if len(missing) > 0 {
		return nil, fmt.Errorf("%w: %d referenced blocks are missing", ErrBadArgument, len(missing))
	}
	reclaimed := make([]string, 0, len(orphans))
	for _, id := range orphans {
		if err := os.Remove(blockPath(s.root, id)); err != nil && !os.IsNotExist(err) {
			return reclaimed, err
		}
		reclaimed = append(reclaimed, id)
	}
	return reclaimed, nil
}

// ---- recovery --------------------------------------------------------------

func (s *Store) recover() (RecoveryReport, error) {
	report := RecoveryReport{}

	// 1. Roll back every staged, uncommitted write transaction. Staged
	//    payloads live entirely inside the transaction directory, so no
	//    committed mapping can reference them yet.
	pending := pendingDir(s.root)
	entries, err := os.ReadDir(pending)
	if err != nil && !os.IsNotExist(err) {
		return report, err
	}
	type pendingItem struct {
		id   int64
		meta diskPending
	}
	var pendings []pendingItem
	for _, ent := range entries {
		if !ent.IsDir() || !strings.HasPrefix(ent.Name(), "txn-") {
			continue
		}
		var id int64
		if _, err := fmt.Sscanf(ent.Name(), "txn-%d", &id); err != nil {
			continue
		}
		var pm diskPending
		mp := txnMetaPath(s.root, id)
		if err := readJSON(mp, &pm); err != nil {
			// A half-written staging directory: remove wholesale.
			if os.IsNotExist(err) {
				report.AbortedTxn = append(report.AbortedTxn, id)
				_ = os.RemoveAll(txnDir(s.root, id))
				continue
			}
			return report, err
		}
		pendings = append(pendings, pendingItem{id: id, meta: pm})
	}
	sort.Slice(pendings, func(i, j int) bool { return pendings[i].id < pendings[j].id })
	for _, p := range pendings {
		report.AbortedTxn = append(report.AbortedTxn, p.id)
		if err := os.RemoveAll(txnDir(s.root, p.id)); err != nil {
			return report, err
		}
	}

	// 2. Resume or finish interrupted merges.
	mEntries, err := os.ReadDir(mergeDir(s.root))
	if err != nil && !os.IsNotExist(err) {
		return report, err
	}
	for _, ent := range mEntries {
		if !strings.HasSuffix(ent.Name(), ".json") {
			continue
		}
		jpath := filepath.Join(mergeDir(s.root), ent.Name())
		var journal diskMergeJournal
		if err := readJSON(jpath, &journal); err != nil {
			return report, err
		}
		vol, ok := s.volumes[journal.VolumeID]
		if !ok {
			// Volume was deleted out from under the journal; drop it.
			_ = os.RemoveAll(filepath.Join(mergeDir(s.root), journal.VolumeID))
			_ = os.Remove(jpath)
			continue
		}
		for _, id := range journal.StagedIDs {
			if err := promoteMergeBlock(s.root, vol.id, id); err != nil {
				return report, err
			}
		}
		if journal.Committed {
			// The journal was committed. Two sub-cases:
			//  1. meta.json already carries NewHeadID: only cleanup was lost.
			//  2. meta.json still ends at ParentID: rebuild the published
			//     version deterministically from the journaled changes.
			if _, ok := vol.versions[journal.NewHeadID]; !ok {
				parent, ok := vol.versions[journal.ParentID]
				if !ok {
					return report, fmt.Errorf("merge replay: parent version %d missing", journal.ParentID)
				}
				vv := &volVersion{
					id:        journal.NewHeadID,
					parentID:  parent.id,
					createdAt: utcNow(),
					size:      journal.NewSize,
					entries:   map[int64]Entry{},
				}
				for lba, e := range parent.entries {
					vv.entries[lba] = e
				}
				for _, c := range journal.Changes {
					if c.Kind == KindHole {
						delete(vv.entries, c.LBA)
					} else {
						vv.entries[c.LBA] = Entry{Kind: c.Kind, BlockID: c.BlockID}
					}
				}
				vol.versions[journal.NewHeadID] = vv
				vol.headID = journal.NewHeadID
				if journal.NewHeadID >= vol.nextVerID {
					vol.nextVerID = journal.NewHeadID + 1
				}
				if err := s.persistMeta(); err != nil {
					return report, err
				}
			}
			report.MergeCommitted = true
			report.ResumedMerge = append(report.ResumedMerge, vol.id)
		} else {
			s.publishMerge(vol, &journal)
			journal.Committed = true
			if err := writeAtomicJSON(jpath, &journal); err != nil {
				return report, err
			}
			if err := s.persistMeta(); err != nil {
				return report, err
			}
			report.ResumedMerge = append(report.ResumedMerge, vol.id)
		}
		if err := os.RemoveAll(filepath.Join(mergeDir(s.root), vol.id)); err != nil {
			return report, err
		}
		if err := os.Remove(jpath); err != nil && !os.IsNotExist(err) {
			return report, err
		}
	}

	// 3. Reconcile physical blocks against all committed references.
	orphans, missing, err := s.audit()
	if err != nil {
		return report, err
	}
	report.OrphanedBlocks = orphans
	report.MissingBlocks = missing
	return report, nil
}
