package podresize

import (
	"encoding/json"
	"io"
	"os"
	"path/filepath"
)

// Store is the durability boundary for the coordination state. Save is called
// after every mutating transition with the complete snapshot, and Load is
// called once when a controller starts.
type Store interface {
	Save(snap *Snapshot) error
	Load() (*Snapshot, error)
}

// cloneSnapshot produces a deep copy so in-memory state and stored state never
// share maps or pointers.
func cloneSnapshot(snap *Snapshot) *Snapshot {
	b, err := json.Marshal(snap)
	if err != nil {
		// All fields are plain JSON-able types; a failure is a programming bug.
		panic("podresize: snapshot marshal failed: " + err.Error())
	}
	var out Snapshot
	if err := json.Unmarshal(b, &out); err != nil {
		panic("podresize: snapshot unmarshal failed: " + err.Error())
	}
	return &out
}

// MemoryStore keeps snapshots in process memory. It is useful for tests and
// still exercises the full snapshot/restore path.
type MemoryStore struct {
	snap *Snapshot
}

// NewMemoryStore returns an empty in-memory store.
func NewMemoryStore() *MemoryStore {
	return &MemoryStore{}
}

func (m *MemoryStore) Save(snap *Snapshot) error {
	m.snap = cloneSnapshot(snap)
	return nil
}

func (m *MemoryStore) Load() (*Snapshot, error) {
	if m.snap == nil {
		return nil, nil
	}
	return cloneSnapshot(m.snap), nil
}

// FileStore persists snapshots as a single JSON file. Writes go to a temp file
// in the same directory followed by an atomic rename, so a crash never leaves
// a torn snapshot behind.
type FileStore struct {
	path string
}

// NewFileStore stores snapshots at path (the parent directory must exist).
func NewFileStore(path string) *FileStore {
	return &FileStore{path: path}
}

func (f *FileStore) Save(snap *Snapshot) error {
	dir := filepath.Dir(f.path)
	tmp, err := os.CreateTemp(dir, ".podresize-*.tmp")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	enc := json.NewEncoder(tmp)
	enc.SetIndent("", "  ")
	if err := enc.Encode(snap); err != nil {
		tmp.Close()
		os.Remove(tmpName)
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		os.Remove(tmpName)
		return err
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmpName)
		return err
	}
	return os.Rename(tmpName, f.path)
}

func (f *FileStore) Load() (*Snapshot, error) {
	file, err := os.Open(f.path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	defer file.Close()
	data, err := io.ReadAll(file)
	if err != nil {
		return nil, err
	}
	if len(data) == 0 {
		return nil, nil
	}
	var snap Snapshot
	if err := json.Unmarshal(data, &snap); err != nil {
		return nil, err
	}
	return &snap, nil
}
