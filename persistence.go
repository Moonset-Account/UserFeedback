package sparseblockvol

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

// On-disk layout:
//
//	root/
//	  meta.json                 store + all volume metadata (atomic replace)
//	  blocks/
//	    ab/abcd...               one file per content-addressed physical block
//	  pending/
//	    txn-<id>/
//	      meta.json             staged transaction description
//	      blocks/<id>           staged payload blocks
//	  merge/
//	    <vol-id>.json            merge replay journal
func (s *Store) metaPath() string   { return filepath.Join(s.root, "meta.json") }
func blocksDir(root string) string  { return filepath.Join(root, "blocks") }
func pendingDir(root string) string { return filepath.Join(root, "pending") }
func mergeDir(root string) string   { return filepath.Join(root, "merge") }
func txnDir(root string, id int64) string {
	return filepath.Join(pendingDir(root), fmt.Sprintf("txn-%d", id))
}
func txnMetaPath(root string, id int64) string {
	return filepath.Join(txnDir(root, id), "meta.json")
}
func txnBlockDir(root string, id int64) string {
	return filepath.Join(txnDir(root, id), "blocks")
}
func mergeJournalPath(root, volID string) string {
	return filepath.Join(mergeDir(root), volID+".json")
}

// blockPath returns the path of a physical block, sharded by the first two
// hex characters so a single directory cannot grow without bound.
func blockPath(root, id string) string {
	if len(id) < 2 {
		return filepath.Join(blocksDir(root), id)
	}
	return filepath.Join(blocksDir(root), id[:2], id)
}

// fsyncBestEffort flushes a directory when the platform supports it.
func fsyncBestEffort(dir string) {
	f, err := os.Open(dir)
	if err != nil {
		return
	}
	_ = f.Sync()
	_ = f.Close()
}

// writeAtomicJSON marshals v and atomically replaces path via rename.
func writeAtomicJSON(path string, v interface{}) error {
	data, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, ".tmp-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer func() {
		if tmp != nil {
			_ = tmp.Close()
			_ = os.Remove(tmpName)
		}
	}()
	if _, err := tmp.Write(data); err != nil {
		return err
	}
	if err := tmp.Sync(); err != nil {
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	tmp = nil
	if err := os.Rename(tmpName, path); err != nil {
		return err
	}
	fsyncBestEffort(dir)
	return nil
}

func readJSON(path string, v interface{}) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return json.Unmarshal(data, v)
}

func writeFileAtomic(path string, data []byte, perm os.FileMode) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, ".tmp-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer func() {
		if tmp != nil {
			_ = tmp.Close()
			_ = os.Remove(tmpName)
		}
	}()
	if _, err := tmp.Write(data); err != nil {
		return err
	}
	if err := tmp.Chmod(perm); err != nil {
		return err
	}
	if err := tmp.Sync(); err != nil {
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	tmp = nil
	if err := os.Rename(tmpName, path); err != nil {
		return err
	}
	fsyncBestEffort(dir)
	return nil
}

func fileExists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}
