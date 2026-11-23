// Package objstore implements a versioned, sharded object store with
// snapshot-consistent paginated listing.
//
// Objects are named by bytewise-ordered keys and stored in key-range shards.
// The shard layout may change at any time (split / merge), and during shard
// migration old and new shards with overlapping read ranges may serve reads
// concurrently. A listing started with List pins the store version visible on
// its first page; continuation pages fetched with ListNext keep reading that
// pinned snapshot through whatever shard layout is currently installed.
package objstore

import (
	"errors"
	"fmt"
	"sort"
	"strings"
	"sync"
)

// ErrSnapshotExpired is returned by ListNext when the snapshot version pinned
// by the first page is no longer retained. Continuation never silently falls
// back to a newer version.
var ErrSnapshotExpired = errors.New("objstore: snapshot expired")

// record is one versioned state of an object.
type record struct {
	version   uint64
	value     []byte
	tombstone bool
}

// shard holds objects for the key range [start, end). An empty end means the
// range is unbounded. During migration the shard list may contain overlapping
// ranges; readers must tolerate that.
type shard struct {
	start string
	end   string
	data  map[string][]record
}

func (sh *shard) contains(key string) bool {
	return key >= sh.start && (sh.end == "" || key < sh.end)
}

// Store is a versioned object store split into key-range shards. It is safe
// for concurrent use.
type Store struct {
	mu         sync.RWMutex
	version    uint64
	minVersion uint64 // oldest snapshot version still readable
	shards     []*shard
}

// NewStore returns an empty store with a single unbounded shard.
func NewStore() *Store {
	return &Store{
		version:    1,
		minVersion: 1,
		shards:     []*shard{{data: map[string][]record{}}},
	}
}

// Version returns the store's current version.
func (s *Store) Version() uint64 {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.version
}

// Put stores value under key and returns the new store version.
func (s *Store) Put(key string, value []byte) uint64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.version++
	rec := record{version: s.version, value: append([]byte(nil), value...)}
	// During migration overlapping shards may both cover key; write to all of
	// them so every read range stays complete.
	for _, sh := range s.shards {
		if sh.contains(key) {
			sh.data[key] = append(sh.data[key], rec)
		}
	}
	return s.version
}

// Delete removes key as of the new version and returns the new version.
// Snapshots pinned at older versions still see the object.
func (s *Store) Delete(key string) uint64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.version++
	rec := record{version: s.version, tombstone: true}
	for _, sh := range s.shards {
		if sh.contains(key) {
			sh.data[key] = append(sh.data[key], rec)
		}
	}
	return s.version
}

// Split divides every shard straddling boundary into two shards
// [start, boundary) and [boundary, end).
func (s *Store) Split(boundary string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []*shard
	for _, sh := range s.shards {
		if boundary > sh.start && (sh.end == "" || boundary < sh.end) {
			left, right := splitShard(sh, boundary)
			out = append(out, left, right)
			continue
		}
		out = append(out, sh)
	}
	s.shards = out
}

// BeginSplit starts a migratory split at boundary: the new half-shards are
// installed alongside the old shard, so their read ranges overlap until
// CompleteSplit retires the old shard.
func (s *Store) BeginSplit(boundary string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var extra []*shard
	for _, sh := range s.shards {
		if boundary > sh.start && (sh.end == "" || boundary < sh.end) {
			left, right := splitShard(sh, boundary)
			extra = append(extra, left, right)
		}
	}
	s.shards = append(s.shards, extra...)
}

// CompleteSplit finishes a migratory split by retiring the old shard that
// still straddles boundary, leaving the half-shards installed by BeginSplit.
func (s *Store) CompleteSplit(boundary string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []*shard
	for _, sh := range s.shards {
		if boundary > sh.start && (sh.end == "" || boundary < sh.end) {
			continue // old shard spanning the boundary: retired
		}
		out = append(out, sh)
	}
	s.shards = out
}

// MergeAll replaces the current shard layout with a single unbounded shard,
// merging version histories of keys served by multiple (overlapping) shards.
func (s *Store) MergeAll() {
	s.mu.Lock()
	defer s.mu.Unlock()
	merged := &shard{data: map[string][]record{}}
	for _, sh := range s.shards {
		for k, recs := range sh.data {
			merged.data[k] = mergeRecords(merged.data[k], recs)
		}
	}
	s.shards = []*shard{merged}
}

// Compact drops all history not needed to read the current version. Snapshots
// pinned at older versions become unreadable: ListNext will then fail with
// ErrSnapshotExpired instead of silently continuing from a newer version.
func (s *Store) Compact() {
	s.mu.Lock()
	defer s.mu.Unlock()
	retain := s.version
	for _, sh := range s.shards {
		for key, recs := range sh.data {
			best := -1
			for i := range recs {
				if recs[i].version <= retain && (best < 0 || recs[i].version > recs[best].version) {
					best = i
				}
			}
			if best < 0 {
				delete(sh.data, key)
				continue
			}
			kept := []record{recs[best]}
			for _, r := range recs {
				if r.version > retain {
					kept = append(kept, r)
				}
			}
			sh.data[key] = kept
		}
	}
	s.minVersion = retain
}

func splitShard(sh *shard, boundary string) (left, right *shard) {
	left = &shard{start: sh.start, end: boundary, data: map[string][]record{}}
	right = &shard{start: boundary, end: sh.end, data: map[string][]record{}}
	for k, recs := range sh.data {
		cp := append([]record(nil), recs...)
		if k < boundary {
			left.data[k] = cp
		} else {
			right.data[k] = cp
		}
	}
	return left, right
}

func mergeRecords(a, b []record) []record {
	seen := map[uint64]bool{}
	out := make([]record, 0, len(a)+len(b))
	for _, r := range a {
		if !seen[r.version] {
			seen[r.version] = true
			out = append(out, r)
		}
	}
	for _, r := range b {
		if !seen[r.version] {
			seen[r.version] = true
			out = append(out, r)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].version < out[j].version })
	return out
}

// visibleAt returns the value visible at snapshot version v.
func visibleAt(recs []record, v uint64) ([]byte, bool) {
	best := -1
	for i := range recs {
		if recs[i].version <= v && (best < 0 || recs[i].version > recs[best].version) {
			best = i
		}
	}
	if best < 0 || recs[best].tombstone {
		return nil, false
	}
	return recs[best].value, true
}

func (s *Store) checkSnapshotLocked(v uint64) error {
	if v < s.minVersion {
		return fmt.Errorf("%w: snapshot version %d predates oldest retained version %d",
			ErrSnapshotExpired, v, s.minVersion)
	}
	return nil
}

func (s *Store) checkSnapshot(v uint64) error {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.checkSnapshotLocked(v)
}

// snapshotKeys returns the sorted keys visible at snapshot v that are >= lower
// and carry the given prefix. It reads through whatever shard layout is
// currently installed, deduplicating keys served by overlapping shards.
func (s *Store) snapshotKeys(v uint64, lower, prefix string) ([]string, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if err := s.checkSnapshotLocked(v); err != nil {
		return nil, err
	}
	set := map[string]struct{}{}
	for _, sh := range s.shards {
		for key, recs := range sh.data {
			if key < lower || !strings.HasPrefix(key, prefix) {
				continue
			}
			if _, ok := visibleAt(recs, v); ok {
				set[key] = struct{}{}
			}
		}
	}
	keys := make([]string, 0, len(set))
	for k := range set {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys, nil
}
