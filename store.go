package blockcw

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"time"
)

// Store is a file-backed block compare-and-write coordinator.
//
// A Store value is safe for concurrent use. All operations synchronize through
// an flock-protected state file, so multiple Store handles in the same process
// and cooperating Unix processes pointing at the same path also observe the
// same linearizable decisions.
type Store struct {
	path     string
	lockPath string
	closed   atomic.Bool
}

// VolumeInfo describes a created volume.
type VolumeInfo struct {
	ID         string    `json:"id"`
	BlockSize  int       `json:"block_size"`
	BlockCount int       `json:"block_count"`
	CreatedAt  time.Time `json:"created_at"`
}

// BlockWrite is an unconditional replacement used for ordinary writes and test
// setup.
type BlockWrite struct {
	Index int
	Data  []byte
}

// Open opens or initializes the complete JSON state file at path. The path's
// parent directory is created when necessary. A companion "<path>.lock" file is
// used for flock coordination and is never renamed.
func Open(path string) (*Store, error) {
	if strings.TrimSpace(path) == "" {
		return nil, fmt.Errorf("%w: state path is empty", ErrInvalidRequest)
	}

	absPath, err := filepath.Abs(path)
	if err != nil {
		return nil, fmt.Errorf("resolve state path: %w", err)
	}
	dir := filepath.Dir(absPath)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("create state directory %s: %w", dir, err)
	}

	s := &Store{path: absPath, lockPath: absPath + ".lock"}
	release, err := acquireFileLock(context.Background(), s.lockPath, true)
	if err != nil {
		return nil, fmt.Errorf("lock new store: %w", err)
	}
	defer release()

	st, _, err := loadOrInitState(absPath)
	if err != nil {
		return nil, err
	}
	if err := saveState(absPath, st); err != nil {
		return nil, err
	}
	return s, nil
}

// Path returns the absolute durable state-file path.
func (s *Store) Path() string { return s.path }

// Close currently releases no long-lived descriptor because each operation
// acquires and releases its file lock independently. It marks the handle closed.
func (s *Store) Close() error {
	s.closed.Store(true)
	return nil
}

func (s *Store) checkOpen() error {
	if s == nil {
		return fmt.Errorf("%w: nil store", ErrInvalidRequest)
	}
	if s.closed.Load() {
		return fmt.Errorf("%w: store is closed", ErrInvalidRequest)
	}
	return nil
}

// CreateVolume creates a fixed-size block volume filled with zero bytes.
func (s *Store) CreateVolume(ctx context.Context, volumeID string, cfg VolumeConfig) (*VolumeInfo, error) {
	if err := s.checkOpen(); err != nil {
		return nil, err
	}
	if strings.TrimSpace(volumeID) == "" {
		return nil, fmt.Errorf("%w: volume id is empty", ErrInvalidRequest)
	}
	if cfg.BlockSize <= 0 {
		return nil, fmt.Errorf("%w: block size must be positive", ErrInvalidRequest)
	}
	if cfg.BlockCount <= 0 {
		return nil, fmt.Errorf("%w: block count must be positive", ErrInvalidRequest)
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}

	release, err := acquireFileLock(ctx, s.lockPath, true)
	if err != nil {
		return nil, err
	}
	defer release()

	st, _, err := loadOrInitState(s.path)
	if err != nil {
		return nil, err
	}
	if _, exists := st.Volumes[volumeID]; exists {
		return nil, fmt.Errorf("%w: %q", ErrVolumeExists, volumeID)
	}

	now := time.Now().UTC()
	blocks := make([][]byte, cfg.BlockCount)
	for i := range blocks {
		blocks[i] = make([]byte, cfg.BlockSize)
	}
	st.Volumes[volumeID] = &persistentVolume{
		BlockSize: cfg.BlockSize,
		Blocks:    blocks,
		CreatedAt: now,
	}
	if err := saveState(s.path, st); err != nil {
		return nil, err
	}
	return &VolumeInfo{
		ID:         volumeID,
		BlockSize:  cfg.BlockSize,
		BlockCount: cfg.BlockCount,
		CreatedAt:  now,
	}, nil
}

// VolumeConfig configures a new volume.
type VolumeConfig struct {
	BlockSize  int
	BlockCount int
}

// WriteBlock replaces one block without a compare phase.
func (s *Store) WriteBlock(ctx context.Context, volumeID string, index int, data []byte) error {
	return s.WriteBlocks(ctx, volumeID, []BlockWrite{{Index: index, Data: data}})
}

// WriteBlocks applies ordinary replacements as one atomic durable transition.
// All lengths and indices are checked before any block is mutated.
func (s *Store) WriteBlocks(ctx context.Context, volumeID string, writes []BlockWrite) error {
	if err := s.checkOpen(); err != nil {
		return err
	}
	if strings.TrimSpace(volumeID) == "" {
		return fmt.Errorf("%w: volume id is empty", ErrInvalidRequest)
	}
	if len(writes) == 0 {
		return fmt.Errorf("%w: at least one write is required", ErrInvalidRequest)
	}
	seen := make(map[int]struct{}, len(writes))
	for i, w := range writes {
		if w.Index < 0 {
			return fmt.Errorf("%w: writes[%d] has negative index %d", ErrInvalidRequest, i, w.Index)
		}
		if _, dup := seen[w.Index]; dup {
			return fmt.Errorf("%w: block %d appears more than once", ErrInvalidRequest, w.Index)
		}
		seen[w.Index] = struct{}{}
	}
	if err := ctx.Err(); err != nil {
		return err
	}

	release, err := acquireFileLock(ctx, s.lockPath, true)
	if err != nil {
		return err
	}
	defer release()

	st, err := loadState(s.path)
	if err != nil {
		return err
	}
	vol, ok := st.Volumes[volumeID]
	if !ok {
		return fmt.Errorf("%w: %q", ErrVolumeNotFound, volumeID)
	}
	for _, w := range writes {
		if w.Index >= len(vol.Blocks) {
			return fmt.Errorf("%w: index %d, volume %q has %d blocks", ErrBlockOutOfRange, w.Index, volumeID, len(vol.Blocks))
		}
		if len(w.Data) != vol.BlockSize {
			return fmt.Errorf("%w: block %d data length %d does not match block size %d", ErrInvalidRequest, w.Index, len(w.Data), vol.BlockSize)
		}
	}

	for _, w := range writes {
		vol.Blocks[w.Index] = cloneBytes(w.Data)
	}
	return saveState(s.path, st)
}

// ReadBlock returns one copied block.
func (s *Store) ReadBlock(ctx context.Context, volumeID string, index int) ([]byte, error) {
	blocks, err := s.ReadBlocks(ctx, volumeID, index)
	if err != nil {
		return nil, err
	}
	return blocks[index], nil
}

// ReadBlocks returns a point-in-time copy of the requested blocks. With no
// indices it returns every block. The returned map and byte slices are safe for
// callers to modify.
func (s *Store) ReadBlocks(ctx context.Context, volumeID string, indices ...int) (map[int][]byte, error) {
	if err := s.checkOpen(); err != nil {
		return nil, err
	}
	if strings.TrimSpace(volumeID) == "" {
		return nil, fmt.Errorf("%w: volume id is empty", ErrInvalidRequest)
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}

	release, err := acquireFileLock(ctx, s.lockPath, false)
	if err != nil {
		return nil, err
	}
	defer release()

	st, err := loadState(s.path)
	if err != nil {
		return nil, err
	}
	vol, ok := st.Volumes[volumeID]
	if !ok {
		return nil, fmt.Errorf("%w: %q", ErrVolumeNotFound, volumeID)
	}

	out := make(map[int][]byte)
	if len(indices) == 0 {
		for i, b := range vol.Blocks {
			out[i] = cloneBytes(b)
		}
		return out, nil
	}

	for _, index := range indices {
		if index < 0 || index >= len(vol.Blocks) {
			return nil, fmt.Errorf("%w: index %d, volume %q has %d blocks", ErrBlockOutOfRange, index, volumeID, len(vol.Blocks))
		}
		out[index] = cloneBytes(vol.Blocks[index])
	}
	return out, nil
}

// ReadVolume returns a point-in-time copy of every block in index order.
func (s *Store) ReadVolume(ctx context.Context, volumeID string) ([][]byte, error) {
	if err := s.checkOpen(); err != nil {
		return nil, err
	}
	if strings.TrimSpace(volumeID) == "" {
		return nil, fmt.Errorf("%w: volume id is empty", ErrInvalidRequest)
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}

	release, err := acquireFileLock(ctx, s.lockPath, false)
	if err != nil {
		return nil, err
	}
	defer release()

	st, err := loadState(s.path)
	if err != nil {
		return nil, err
	}
	vol, ok := st.Volumes[volumeID]
	if !ok {
		return nil, fmt.Errorf("%w: %q", ErrVolumeNotFound, volumeID)
	}
	out := make([][]byte, len(vol.Blocks))
	for i, b := range vol.Blocks {
		out[i] = cloneBytes(b)
	}
	return out, nil
}

// CompareAndWrite performs the durable group compare-and-write.
//
// Semantics:
//   - A new request ID is compared against every current named block.
//   - If all comparisons match, all replacements are applied in one atomic
//     state transition.
//   - If any comparison fails, no block changes and every mismatch is returned.
//   - Retrying an existing request ID with the same canonical content returns
//     the originally persisted decision and mismatch bytes.
//   - Reusing an ID with different content wraps ErrRequestIDReused.
func (s *Store) CompareAndWrite(ctx context.Context, req Request) (*Result, error) {
	if err := s.checkOpen(); err != nil {
		return nil, err
	}
	if err := req.Validate(); err != nil {
		return nil, err
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}

	fingerprint := Fingerprint(req)
	release, err := acquireFileLock(ctx, s.lockPath, true)
	if err != nil {
		return nil, err
	}
	defer release()

	st, _, err := loadOrInitState(s.path)
	if err != nil {
		return nil, err
	}

	// The idempotency decision is authoritative before touching volume data.
	if record, exists := st.Requests[req.RequestID]; exists {
		if record.Fingerprint != fingerprint {
			return nil, &RequestReuseError{
				VolumeID:            req.VolumeID,
				RequestID:           req.RequestID,
				ExistingFingerprint: record.Fingerprint,
				ProvidedFingerprint: fingerprint,
			}
		}
		return record.toResult(true), nil
	}

	vol, ok := st.Volumes[req.VolumeID]
	if !ok {
		return nil, fmt.Errorf("%w: %q", ErrVolumeNotFound, req.VolumeID)
	}

	ops := req.canonicalOperations()
	for _, op := range ops {
		if op.Index >= len(vol.Blocks) {
			return nil, fmt.Errorf(
				"%w: index %d, volume %q has %d blocks",
				ErrBlockOutOfRange, op.Index, req.VolumeID, len(vol.Blocks),
			)
		}
		if len(op.Expected) != vol.BlockSize || len(op.Replacement) != vol.BlockSize {
			return nil, fmt.Errorf(
				"%w: block %d expected/replacement lengths must both be block size %d",
				ErrInvalidRequest, op.Index, vol.BlockSize,
			)
		}
	}

	mismatches := make([]Mismatch, 0)
	for _, op := range ops {
		actual := vol.Blocks[op.Index]
		if !equalBytes(op.Expected, actual) {
			mismatches = append(mismatches, Mismatch{
				Index:    op.Index,
				Expected: cloneBytes(op.Expected),
				Actual:   cloneBytes(actual),
			})
		}
	}

	now := time.Now().UTC()
	record := &requestRecord{
		RequestID:   req.RequestID,
		VolumeID:    req.VolumeID,
		Fingerprint: fingerprint,
		CreatedAt:   now,
		CompletedAt: now,
	}
	result := &Result{
		VolumeID:    req.VolumeID,
		RequestID:   req.RequestID,
		CompletedAt: now,
	}

	if len(mismatches) > 0 {
		record.Decision = DecisionConflict
		record.Mismatches = cloneMismatches(mismatches)
		result.Decision = DecisionConflict
		result.Mismatches = mismatches
	} else {
		record.Decision = DecisionCommitted
		result.Decision = DecisionCommitted
		for _, op := range ops {
			vol.Blocks[op.Index] = cloneBytes(op.Replacement)
		}
	}

	// Request record and all block bytes enter the same JSON state and are
	// published by one atomic rename. Crash/restart recovery therefore has no
	// separate uncertain pending transaction to reconstruct.
	st.Requests[req.RequestID] = record
	if err := saveState(s.path, st); err != nil {
		return nil, fmt.Errorf("blockcw: persist compare-and-write decision: %w", err)
	}
	return result, nil
}

func equalBytes(a, b []byte) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
