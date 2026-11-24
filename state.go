package blockcw

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"time"
)

const stateVersion = 1

// persistentState is the complete atomically replaced durable state.
type persistentState struct {
	Version  int                          `json:"version"`
	Volumes  map[string]*persistentVolume `json:"volumes"`
	Requests map[string]*requestRecord    `json:"requests"`
}

type persistentVolume struct {
	BlockSize int       `json:"block_size"`
	Blocks    [][]byte  `json:"blocks"`
	CreatedAt time.Time `json:"created_at"`
}

type requestRecord struct {
	RequestID   string     `json:"request_id"`
	VolumeID    string     `json:"volume_id"`
	Fingerprint string     `json:"fingerprint"`
	Decision    Decision   `json:"decision"`
	Mismatches  []Mismatch `json:"mismatches,omitempty"`
	CreatedAt   time.Time  `json:"created_at"`
	CompletedAt time.Time  `json:"completed_at"`
}

func newPersistentState() *persistentState {
	return &persistentState{
		Version:  stateVersion,
		Volumes:  make(map[string]*persistentVolume),
		Requests: make(map[string]*requestRecord),
	}
}

func (r *requestRecord) toResult(replayed bool) *Result {
	return &Result{
		VolumeID:    r.VolumeID,
		RequestID:   r.RequestID,
		Decision:    r.Decision,
		Mismatches:  cloneMismatches(r.Mismatches),
		Replayed:    replayed,
		CompletedAt: r.CompletedAt,
	}
}

func loadState(path string) (*persistentState, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}

	var st persistentState
	if err := json.Unmarshal(data, &st); err != nil {
		return nil, fmt.Errorf("%w: decode %s: %v", ErrCorruptState, path, err)
	}
	if st.Version != stateVersion {
		return nil, fmt.Errorf("%w: unsupported version %d in %s", ErrCorruptState, st.Version, path)
	}
	if st.Volumes == nil {
		st.Volumes = make(map[string]*persistentVolume)
	}
	if st.Requests == nil {
		st.Requests = make(map[string]*requestRecord)
	}
	return &st, nil
}

// loadOrInitState returns a fresh state when the state file has never been
// created. Other errors, including a corrupt file, are returned unchanged.
func loadOrInitState(path string) (*persistentState, bool, error) {
	st, err := loadState(path)
	if err == nil {
		return st, false, nil
	}
	if errors.Is(err, fs.ErrNotExist) {
		return newPersistentState(), true, nil
	}
	return nil, false, err
}

// saveState writes a temporary file, fsyncs it, atomically renames it over the
// active state file, and fsyncs the directory. A reader therefore sees either
// the complete old state or the complete new state, never a half-written JSON
// document or a partially applied group.
func saveState(path string, st *persistentState) error {
	st.Version = stateVersion
	if st.Volumes == nil {
		st.Volumes = make(map[string]*persistentVolume)
	}
	if st.Requests == nil {
		st.Requests = make(map[string]*requestRecord)
	}

	data, err := json.MarshalIndent(st, "", "  ")
	if err != nil {
		return fmt.Errorf("encode state: %w", err)
	}

	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".blockcw-state-*.tmp")
	if err != nil {
		return fmt.Errorf("create temporary state file: %w", err)
	}
	tmpName := tmp.Name()
	cleanup := func() { _ = os.Remove(tmpName) }

	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		cleanup()
		return fmt.Errorf("write temporary state file: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		cleanup()
		return fmt.Errorf("sync temporary state file: %w", err)
	}
	if err := tmp.Close(); err != nil {
		cleanup()
		return fmt.Errorf("close temporary state file: %w", err)
	}
	if err := os.Chmod(tmpName, 0o600); err != nil {
		cleanup()
		return fmt.Errorf("chmod temporary state file: %w", err)
	}
	if err := os.Rename(tmpName, path); err != nil {
		cleanup()
		return fmt.Errorf("atomically replace state file: %w", err)
	}
	if err := syncDir(dir); err != nil {
		// The rename has already happened. Returning this error lets a caller
		// retry; the durable record is normally visible and the retry will
		// return the original decision.
		return fmt.Errorf("sync state directory: %w", err)
	}
	return nil
}

func syncDir(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return err
	}
	syncErr := d.Sync()
	closeErr := d.Close()
	if syncErr != nil {
		return syncErr
	}
	return closeErr
}
