package sparseblockvol

import (
	"fmt"
	"sort"
)

// Volume is a handle to one logical volume inside a Store.
type Volume struct {
	store *Store
	vol   *storeVolume
}

// View is an immutable read handle of one volume version.
type View struct {
	store     *Store
	vol       *storeVolume
	versionID int64
	ver       *volVersion
}

// CreateVolume adds a volume with the given logical block size. The block
// size must be a positive power of two; different volumes may use different
// block sizes, which is how pre- and post-upgrade alignments coexist.
func (s *Store) CreateVolume(id string, blockSize int) (*Volume, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.checkOpen(); err != nil {
		return nil, err
	}
	if id == "" {
		return nil, fmt.Errorf("%w: empty volume id", ErrBadArgument)
	}
	if _, ok := s.volumes[id]; ok {
		return nil, fmt.Errorf("%w: volume %q", ErrExists, id)
	}
	if blockSize <= 0 || blockSize&(blockSize-1) != 0 {
		return nil, fmt.Errorf("%w: %d", ErrBlockSize, blockSize)
	}
	now := utcNow()
	v := &storeVolume{
		id:        id,
		blockSize: blockSize,
		headID:    1,
		nextVerID: 2,
		snapshots: map[string]int64{},
		versions: map[int64]*volVersion{
			1: {
				id:        1,
				parentID:  0,
				createdAt: now,
				size:      0,
				entries:   map[int64]Entry{},
			},
		},
	}
	s.volumes[id] = v
	if err := s.persistMeta(); err != nil {
		delete(s.volumes, id)
		return nil, err
	}
	return &Volume{store: s, vol: v}, nil
}

// Volume looks up an existing volume.
func (s *Store) Volume(id string) (*Volume, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.checkOpen(); err != nil {
		return nil, err
	}
	v, ok := s.volumes[id]
	if !ok {
		return nil, fmt.Errorf("%w: volume %q", ErrNotFound, id)
	}
	return &Volume{store: s, vol: v}, nil
}

// VolumeIDs lists all volume ids in deterministic order.
func (s *Store) VolumeIDs() ([]string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.checkOpen(); err != nil {
		return nil, err
	}
	ids := make([]string, 0, len(s.volumes))
	for id := range s.volumes {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids, nil
}

// DeleteVolume removes a volume and all of its versions. Physical blocks are
// only un-referenced; call Reclaim afterwards to free their bytes.
func (s *Store) DeleteVolume(id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.checkOpen(); err != nil {
		return err
	}
	if _, ok := s.volumes[id]; !ok {
		return fmt.Errorf("%w: volume %q", ErrNotFound, id)
	}
	delete(s.volumes, id)
	return s.persistMeta()
}

// ID returns the volume id.
func (v *Volume) ID() string { return v.vol.id }

// BlockSize returns the volume's logical block size in bytes.
func (v *Volume) BlockSize() int { return v.vol.blockSize }

// Head returns a read view of the current head version.
func (v *Volume) Head() (*View, error) {
	v.store.mu.Lock()
	defer v.store.mu.Unlock()
	if err := v.store.checkOpen(); err != nil {
		return nil, err
	}
	return v.viewLocked(v.vol.headID)
}

// Version returns a read view of any published version.
func (v *Volume) Version(id int64) (*View, error) {
	v.store.mu.Lock()
	defer v.store.mu.Unlock()
	if err := v.store.checkOpen(); err != nil {
		return nil, err
	}
	return v.viewLocked(id)
}

// Snapshot returns the view of a named snapshot.
func (v *Volume) Snapshot(name string) (*View, error) {
	v.store.mu.Lock()
	defer v.store.mu.Unlock()
	if err := v.store.checkOpen(); err != nil {
		return nil, err
	}
	vid, ok := v.vol.snapshots[name]
	if !ok {
		return nil, fmt.Errorf("%w: snapshot %q", ErrNotFound, name)
	}
	return v.viewLocked(vid)
}

func (v *Volume) viewLocked(id int64) (*View, error) {
	ver, ok := v.vol.versions[id]
	if !ok {
		return nil, fmt.Errorf("%w: version %d", ErrNotFound, id)
	}
	return &View{store: v.store, vol: v.vol, versionID: id, ver: ver}, nil
}

// Versions describes all published versions, oldest first.
func (v *Volume) Versions() ([]Version, error) {
	v.store.mu.Lock()
	defer v.store.mu.Unlock()
	if err := v.store.checkOpen(); err != nil {
		return nil, err
	}
	ids := make([]int64, 0, len(v.vol.versions))
	for id := range v.vol.versions {
		ids = append(ids, id)
	}
	sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
	out := make([]Version, 0, len(ids))
	for _, id := range ids {
		ver := v.vol.versions[id]
		out = append(out, Version{
			ID:        ver.id,
			ParentID:  ver.parentID,
			CreatedAt: ver.createdAt,
			Size:      ver.size,
			Snapshots: v.snapshotNamesLocked(id),
		})
	}
	return out, nil
}

func (v *Volume) snapshotNamesLocked(versionID int64) []string {
	var names []string
	for name, vid := range v.vol.snapshots {
		if vid == versionID {
			names = append(names, name)
		}
	}
	sort.Strings(names)
	return names
}

// SnapshotNames lists all named snapshots.
func (v *Volume) SnapshotNames() ([]string, error) {
	v.store.mu.Lock()
	defer v.store.mu.Unlock()
	if err := v.store.checkOpen(); err != nil {
		return nil, err
	}
	names := make([]string, 0, len(v.vol.snapshots))
	for n := range v.vol.snapshots {
		names = append(names, n)
	}
	sort.Strings(names)
	return names, nil
}

// TagSnapshot pins an existing version under a name so DeleteVersion cannot
// remove it and callers can find it later.
func (v *Volume) TagSnapshot(name string, versionID int64) error {
	v.store.mu.Lock()
	defer v.store.mu.Unlock()
	if err := v.store.checkOpen(); err != nil {
		return err
	}
	if name == "" {
		return fmt.Errorf("%w: empty snapshot name", ErrBadArgument)
	}
	if _, ok := v.vol.versions[versionID]; !ok {
		return fmt.Errorf("%w: version %d", ErrNotFound, versionID)
	}
	if existing, ok := v.vol.snapshots[name]; ok && existing != versionID {
		return fmt.Errorf("%w: name %q already points at version %d", ErrSnapshotName, name, existing)
	}
	v.vol.snapshots[name] = versionID
	return v.store.persistMeta()
}

// DeleteSnapshot removes a snapshot name. The underlying version is kept.
func (v *Volume) DeleteSnapshot(name string) error {
	v.store.mu.Lock()
	defer v.store.mu.Unlock()
	if err := v.store.checkOpen(); err != nil {
		return err
	}
	if _, ok := v.vol.snapshots[name]; !ok {
		return fmt.Errorf("%w: snapshot %q", ErrNotFound, name)
	}
	delete(v.vol.snapshots, name)
	return v.store.persistMeta()
}

// DeleteVersion removes an old non-head, unpinned version. Blocks referenced
// only by it become reclaimable.
func (v *Volume) DeleteVersion(versionID int64) error {
	v.store.mu.Lock()
	defer v.store.mu.Unlock()
	if err := v.store.checkOpen(); err != nil {
		return err
	}
	if versionID == v.vol.headID {
		return fmt.Errorf("%w: cannot delete head version %d", ErrBadArgument, versionID)
	}
	if _, ok := v.vol.versions[versionID]; !ok {
		return fmt.Errorf("%w: version %d", ErrNotFound, versionID)
	}
	for name, vid := range v.vol.snapshots {
		if vid == versionID {
			return fmt.Errorf("%w: version %d is snapshot %q", ErrPinnedVersion, versionID, name)
		}
	}
	delete(v.vol.versions, versionID)
	return v.store.persistMeta()
}

// VersionID returns the version this view reads.
func (w *View) VersionID() int64 { return w.versionID }

// Size returns the logical volume size in bytes at this version.
func (w *View) Size() int64 { return w.ver.size }

// LookupEntry returns the mapping of one logical block number. A block beyond
// the volume size and a never-written or punched block both report KindHole;
// explicit zero writes report KindZero so callers can tell the states apart.
func (w *View) LookupEntry(lba int64) (Entry, error) {
	w.store.mu.Lock()
	defer w.store.mu.Unlock()
	if err := w.store.checkOpen(); err != nil {
		return Entry{}, err
	}
	if lba < 0 {
		return Entry{}, fmt.Errorf("%w: negative lba %d", ErrBadArgument, lba)
	}
	if lba*int64(w.vol.blockSize) >= w.ver.size {
		return Entry{Kind: KindHole}, nil
	}
	if e, ok := w.ver.entries[lba]; ok {
		return e, nil
	}
	return Entry{Kind: KindHole}, nil
}

// Extents enumerates the whole version as coalesced, type-homogeneous extents
// in byte offsets. Holes, zero segments and mapped ranges are reported
// separately, including the leading, trailing and internal holes.
func (w *View) Extents() ([]Extent, error) {
	w.store.mu.Lock()
	defer w.store.mu.Unlock()
	if err := w.store.checkOpen(); err != nil {
		return nil, err
	}
	return w.extentsLocked(), nil
}

func (w *View) extentsLocked() []Extent {
	bs := int64(w.vol.blockSize)
	nBlocks := w.ver.size / bs
	if nBlocks == 0 {
		return []Extent{}
	}
	lbas := make([]int64, 0, len(w.ver.entries))
	for lba := range w.ver.entries {
		if lba < nBlocks {
			lbas = append(lbas, lba)
		}
	}
	sort.Slice(lbas, func(i, j int) bool { return lbas[i] < lbas[j] })

	var out []Extent
	var cur *Extent
	prev := int64(-1)
	for _, lba := range lbas {
		e := w.ver.entries[lba]
		if cur != nil && (lba != prev+1 || e.Kind != cur.Kind) {
			out = append(out, *cur)
			cur = nil
		}
		if cur == nil {
			cur = &Extent{Kind: e.Kind, Offset: lba * bs, Length: bs}
		} else {
			cur.Length += bs
		}
		prev = lba
	}
	if cur != nil {
		out = append(out, *cur)
	}

	// Fill gaps (including holes) so the result covers [0, Size()).
	filled := make([]Extent, 0, len(out)+2)
	pos := int64(0)
	for _, ex := range out {
		if ex.Offset > pos {
			filled = append(filled, Extent{Kind: KindHole, Offset: pos, Length: ex.Offset - pos})
		}
		filled = append(filled, ex)
		pos = ex.Offset + ex.Length
	}
	if pos < w.ver.size {
		filled = append(filled, Extent{Kind: KindHole, Offset: pos, Length: w.ver.size - pos})
	}
	return filled
}

// MappedRanges returns only mapped (non-hole, non-zero) extents.
func (w *View) MappedRanges() ([]Extent, error) {
	w.store.mu.Lock()
	defer w.store.mu.Unlock()
	if err := w.store.checkOpen(); err != nil {
		return nil, err
	}
	var out []Extent
	for _, ex := range w.extentsLocked() {
		if ex.Kind == KindMapped {
			out = append(out, ex)
		}
	}
	return out, nil
}

// ReadAt reads len(p) bytes at the given byte offset. Holes and zero segments
// return zero bytes. Reads past the volume size are zero-filled as far as the
// request reaches; the number of bytes placed into p is always len(p).
func (w *View) ReadAt(p []byte, off int64) (int, error) {
	w.store.mu.Lock()
	defer w.store.mu.Unlock()
	if err := w.store.checkOpen(); err != nil {
		return 0, err
	}
	if off < 0 {
		return 0, fmt.Errorf("%w: negative offset", ErrBadArgument)
	}
	return w.readAtLocked(p, off)
}

// readAtLocked performs the read without taking the store lock. Callers that
// already hold the lock (for example Merge planning) must use this variant.
func (w *View) readAtLocked(p []byte, off int64) (int, error) {
	bs := int64(w.vol.blockSize)
	for i := 0; i < len(p); {
		pos := off + int64(i)
		lba := pos / bs
		inBlock := pos - lba*bs
		take := int(bs - inBlock)
		if take > len(p)-i {
			take = len(p) - i
		}
		if pos >= w.ver.size {
			for j := 0; j < take; j++ {
				p[i+j] = 0
			}
		} else if e, ok := w.ver.entries[lba]; ok && e.Kind == KindMapped {
			data, err := w.store.readBlock(e.BlockID)
			if err != nil {
				return i, err
			}
			copy(p[i:i+take], data[inBlock:inBlock+int64(take)])
		} else {
			// KindZero and KindHole both read as zero bytes.
			for j := 0; j < take; j++ {
				p[i+j] = 0
			}
		}
		i += take
	}
	return len(p), nil
}
