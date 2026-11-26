package sparseblockvol

// Persisted data structures. These types are serialized to meta.json and to
// the journal / staging files. Keeping them separate from the public API
// types lets the on-disk format evolve independently of callers.

type diskEntry struct {
	Kind    string `json:"kind"`
	BlockID string `json:"block_id,omitempty"`
}

type diskVersion struct {
	ID        int64               `json:"id"`
	ParentID  int64               `json:"parent_id"`
	CreatedAt string              `json:"created_at"` // RFC3339Nano UTC
	Size      int64               `json:"size"`
	Entries   map[int64]diskEntry `json:"entries"`
}

type diskVolume struct {
	ID        string                `json:"id"`
	BlockSize int                   `json:"block_size"`
	HeadID    int64                 `json:"head_id"`
	Snapshots map[string]int64      `json:"snapshot_name_to_version"`
	Versions  map[int64]diskVersion `json:"versions"`
}

type diskMeta struct {
	NextTxnID   int64                 `json:"next_txn_id"`
	NextVersion map[string]int64      `json:"volume_to_next_version_id"`
	Volumes     map[string]diskVolume `json:"volumes"`
}

// changeOp is one logical-block modification recorded by a staged write
// transaction or by a merge journal.
type changeOp struct {
	LBA     int64     `json:"lba"`
	Kind    EntryKind `json:"kind"`
	BlockID string    `json:"block_id,omitempty"`
}

type diskPending struct {
	TxnID      int64      `json:"txn_id"`
	VolumeID   string     `json:"volume_id"`
	NewSize    int64      `json:"new_size"`
	Snapshot   string     `json:"snapshot,omitempty"`
	Changes    []changeOp `json:"changes"`
	StagedIDs  []string   `json:"staged_block_ids"`
	CrashPoint string     `json:"crash_point,omitempty"`
}

type diskMergeJournal struct {
	VolumeID   string     `json:"volume_id"`
	SourceVol  string     `json:"source_volume"`
	SourceVer  int64      `json:"source_version"`
	DstOffset  int64      `json:"dst_offset"`
	ParentID   int64      `json:"parent_id"`
	NewHeadID  int64      `json:"new_head_id"`
	NewSize    int64      `json:"new_size"`
	Changes    []changeOp `json:"changes"`
	StagedIDs  []string   `json:"staged_block_ids"`
	Committed  bool       `json:"committed"`
	CrashPoint string     `json:"crash_point,omitempty"`
}
