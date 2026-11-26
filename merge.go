package sparseblockvol

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
)

// MergeOptions controls a Merge call.
type MergeOptions struct {
	// DstOffset places the source bytes at this byte offset of the target.
	DstOffset int64
	// CrashPoint optionally injects an interruption:
	//   "merge.after_stage" - journal and blocks are durable, nothing applied
	//   "merge.after_meta"  - new version published, journal cleanup pending
	CrashPoint string
}

// Merge copies one source view into the target volume, producing a new target
// version. The source and target may have different block sizes: every source
// extent (mapped, zero or hole) is translated into byte-level changes, and
// target blocks touched only partially are read-modified-written.
//
// The full plan is journaled before any target metadata changes, so a crash
// at either injection point is completed deterministically on the next Open,
// while all earlier target versions stay readable.
func (v *Volume) Merge(src *View, opts MergeOptions) (int64, error) {
	v.store.mu.Lock()
	defer v.store.mu.Unlock()
	if err := v.store.checkOpen(); err != nil {
		return 0, err
	}
	if src == nil {
		return 0, fmt.Errorf("%w: nil source view", ErrBadArgument)
	}
	if opts.DstOffset < 0 {
		return 0, fmt.Errorf("%w: negative destination offset", ErrBadArgument)
	}

	head := v.vol.versions[v.vol.headID]
	bs := int64(v.vol.blockSize)
	entries := map[int64]Entry{}
	for lba, e := range head.entries {
		entries[lba] = e
	}
	newSize := head.size
	if end := opts.DstOffset + src.Size(); end > newSize {
		newSize = ((end + bs - 1) / bs) * bs
	}

	// Build the byte-level change plan from the source's full extent list,
	// staging every new physical block in the merge's private area.
	extents := src.extentsLocked()
	changes := map[int64]changeOp{}
	stagedSet := map[string]bool{}
	stagedData := map[string][]byte{}
	stage := func(lba int64, data []byte) error {
		id := hashBlock(data)
		entries[lba] = Entry{Kind: KindMapped, BlockID: id}
		changes[lba] = changeOp{LBA: lba, Kind: KindMapped, BlockID: id}
		if stagedSet[id] || v.store.blockExists(id) {
			return nil
		}
		if err := v.store.stageMergeBlock(v.vol.id, id, data); err != nil {
			return err
		}
		stagedSet[id] = true
		stagedData[id] = append([]byte(nil), data...)
		return nil
	}
	materialize := func(lba int64) []byte {
		buf := make([]byte, bs)
		if e, ok := entries[lba]; ok && e.Kind == KindMapped {
			if data, ok := stagedData[e.BlockID]; ok {
				copy(buf, data)
				return buf
			}
			if data, rerr := v.store.readBlock(e.BlockID); rerr == nil {
				copy(buf, data)
			}
		}
		return buf
	}
	srcBuf := make([]byte, 0, src.vol.blockSize)
	for _, ex := range extents {
		cursorSrc := ex.Offset
		remaining := ex.Length
		dstOff := opts.DstOffset + ex.Offset
		for remaining > 0 {
			dstLBA := dstOff / bs
			rel := dstOff - dstLBA*bs
			take := bs - rel
			if take > remaining {
				take = remaining
			}
			rangeEnd := dstOff + take
			blockStart := dstLBA * bs
			blockEnd := blockStart + bs
			wholeBlock := dstOff == blockStart && rangeEnd == blockEnd
			switch {
			case ex.Kind == KindHole && wholeBlock:
				delete(entries, dstLBA)
				changes[dstLBA] = changeOp{LBA: dstLBA, Kind: KindHole}
			case ex.Kind == KindHole:
				block := materialize(dstLBA)
				for i := dstOff - blockStart; i < rangeEnd-blockStart; i++ {
					block[i] = 0
				}
				if err := stage(dstLBA, block); err != nil {
					return 0, err
				}
			case ex.Kind == KindZero && wholeBlock:
				entries[dstLBA] = Entry{Kind: KindZero}
				changes[dstLBA] = changeOp{LBA: dstLBA, Kind: KindZero}
			case ex.Kind == KindZero:
				block := materialize(dstLBA)
				for i := dstOff - blockStart; i < rangeEnd-blockStart; i++ {
					block[i] = 0
				}
				if err := stage(dstLBA, block); err != nil {
					return 0, err
				}
			default:
				if cap(srcBuf) < int(take) {
					srcBuf = make([]byte, take)
				}
				buf := srcBuf[:take]
				for i := range buf {
					buf[i] = 0
				}
				if _, err := src.readAtLocked(buf, cursorSrc); err != nil {
					return 0, err
				}
				if wholeBlock {
					if err := stage(dstLBA, buf); err != nil {
						return 0, err
					}
				} else {
					block := materialize(dstLBA)
					copy(block[rel:rel+take], buf)
					if err := stage(dstLBA, block); err != nil {
						return 0, err
					}
				}
			}
			dstOff += take
			cursorSrc += take
			remaining -= take
		}
	}

	changeList := make([]changeOp, 0, len(changes))
	for _, c := range changes {
		changeList = append(changeList, c)
	}
	sort.Slice(changeList, func(i, j int) bool { return changeList[i].LBA < changeList[j].LBA })
	stagedIDs := make([]string, 0, len(stagedSet))
	for id := range stagedSet {
		stagedIDs = append(stagedIDs, id)
	}
	sort.Strings(stagedIDs)

	jpath := mergeJournalPath(v.store.root, v.vol.id)
	if fileExists(jpath) {
		return 0, fmt.Errorf("%w: merge journal for %q already exists", ErrBadArgument, v.vol.id)
	}
	journal := diskMergeJournal{
		VolumeID:   v.vol.id,
		SourceVol:  src.vol.id,
		SourceVer:  src.versionID,
		DstOffset:  opts.DstOffset,
		NewSize:    newSize,
		Changes:    changeList,
		StagedIDs:  stagedIDs,
		CrashPoint: opts.CrashPoint,
	}
	if err := os.MkdirAll(mergeStageDir(v.store.root, v.vol.id), 0o755); err != nil {
		return 0, err
	}
	if err := writeAtomicJSON(jpath, &journal); err != nil {
		return 0, err
	}
	if err := v.store.hitCrash("merge.after_stage"); err != nil {
		return 0, err
	}

	for _, id := range stagedIDs {
		if err := promoteMergeBlock(v.store.root, v.vol.id, id); err != nil {
			return 0, err
		}
	}
	journal.ParentID = v.vol.headID
	v.store.publishMerge(v.vol, &journal)
	journal.NewHeadID = v.vol.headID
	// Marking the journal committed before the metadata switch means that a
	// crash in either of the next two durable steps is replayed idempotently.
	journal.Committed = true
	if err := writeAtomicJSON(jpath, &journal); err != nil {
		return 0, err
	}
	if err := v.store.persistMeta(); err != nil {
		return 0, err
	}
	if err := v.store.hitCrash("merge.after_meta"); err != nil {
		return 0, err
	}
	if err := os.RemoveAll(filepath.Join(mergeDir(v.store.root), v.vol.id)); err != nil {
		return 0, err
	}
	if err := os.Remove(jpath); err != nil && !os.IsNotExist(err) {
		return 0, err
	}
	return v.vol.headID, nil
}

// publishMerge turns a merge journal's change list into a new immutable head
// version. It is used both by a live Merge and by recovery replay.
func (s *Store) publishMerge(vol *storeVolume, journal *diskMergeJournal) {
	newID := vol.nextVerID
	vol.nextVerID++
	parent := vol.versions[vol.headID]
	vv := &volVersion{
		id:        newID,
		parentID:  parent.id,
		createdAt: utcNow(),
		size:      journal.NewSize,
		entries:   map[int64]Entry{},
	}
	for lba, e := range parent.entries {
		vv.entries[lba] = e
	}
	for _, c := range journal.Changes {
		switch c.Kind {
		case KindHole:
			delete(vv.entries, c.LBA)
		default:
			vv.entries[c.LBA] = Entry{Kind: c.Kind, BlockID: c.BlockID}
		}
	}
	vol.versions[newID] = vv
	vol.headID = newID
}
