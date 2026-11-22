package distinctagg

import (
	"encoding/gob"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
)

// segmentStore 管理所有分区/尝试的落盘片段。
//
// 目录布局：
//
//	root/
//	  p<partition>/
//	    staging/attempt-<n>/seg-<k>.gob   未确认片段，任何合并都不会读取
//	    committed/attempt-<n>/seg-<k>.gob 获选尝试的整组片段
//
// 一次分区尝试产生的所有片段先全部写入 staging，提交时用一次原子 rename
// 整体转为 committed；未确认的旧片段因此不可能污染合并结果。
type segmentStore struct {
	root        string
	mu          sync.Mutex
	nextAttempt map[int]int // 每个分区下一个尝试编号（从 1 开始）
}

func newSegmentStore(root string) *segmentStore {
	return &segmentStore{
		root:        root,
		nextAttempt: make(map[int]int),
	}
}

func (s *segmentStore) partitionDir(partition int) string {
	return filepath.Join(s.root, fmt.Sprintf("p%d", partition))
}

func (s *segmentStore) stagingDir(partition, attempt int) string {
	return filepath.Join(s.partitionDir(partition), "staging", fmt.Sprintf("attempt-%d", attempt))
}

func (s *segmentStore) committedDir(partition, attempt int) string {
	return filepath.Join(s.partitionDir(partition), "committed", fmt.Sprintf("attempt-%d", attempt))
}

// startAttempt 分配一个新的单调递增尝试编号并准备暂存目录。
func (s *segmentStore) startAttempt(partition int) (int, string, error) {
	s.mu.Lock()
	id := s.nextAttempt[partition] + 1
	s.nextAttempt[partition] = id
	s.mu.Unlock()

	dir := s.stagingDir(partition, id)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return 0, "", err
	}
	return id, dir, nil
}

// writeSegment 把一个片段以 gob 形式写入暂存目录，返回文件大小与路径。
func (s *segmentStore) writeSegment(partition, attempt, seq int, seg *segment) (int, string, error) {
	dir := s.stagingDir(partition, attempt)
	path := filepath.Join(dir, fmt.Sprintf("seg-%d.gob", seq))

	f, err := os.Create(path)
	if err != nil {
		return 0, path, err
	}
	enc := gob.NewEncoder(f)
	if err := enc.Encode(seg); err != nil {
		_ = f.Close()
		_ = os.Remove(path)
		return 0, path, err
	}
	if err := f.Sync(); err != nil {
		_ = f.Close()
		_ = os.Remove(path)
		return 0, path, err
	}
	if err := f.Close(); err != nil {
		_ = os.Remove(path)
		return 0, path, err
	}
	info, err := os.Stat(path)
	if err != nil {
		return 0, path, err
	}
	return int(info.Size()), path, nil
}

// commitAttempt 把整个尝试目录从 staging 原子移动到 committed。
// 语义：要么整组片段全部可见，要么全部不可见。
func (s *segmentStore) commitAttempt(partition, attempt int) (string, error) {
	src := s.stagingDir(partition, attempt)
	dst := s.committedDir(partition, attempt)
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return "", err
	}
	if err := os.Rename(src, dst); err != nil {
		return "", err
	}
	return dst, nil
}

// abortAttempt 删除一次失败尝试的暂存目录（含已写完但未确认的片段）。
func (s *segmentStore) abortAttempt(partition, attempt int) error {
	return os.RemoveAll(s.stagingDir(partition, attempt))
}

// committedSegments 只读取已确认尝试目录下的片段，按溢写顺序排列。
// 暂存目录在任何情况下都不会被扫描到。
func (s *segmentStore) committedSegments(partition, attempt int) ([]string, error) {
	dir := s.committedDir(partition, attempt)
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	var paths []string
	for _, e := range entries {
		if !e.IsDir() && filepath.Ext(e.Name()) == ".gob" {
			paths = append(paths, filepath.Join(dir, e.Name()))
		}
	}
	sort.Strings(paths)
	if len(paths) == 0 {
		return nil, fmt.Errorf("distinctagg: no committed segment under %s", dir)
	}
	return paths, nil
}

// readSegment 读回一个片段。
func readSegment(path string) (*segment, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	var seg segment
	if err := gob.NewDecoder(f).Decode(&seg); err != nil {
		return nil, err
	}
	// gob 会把空切片编码/解码为 nil；空分区产生的空片段是合法的。
	if seg.Groups == nil {
		seg.Groups = []*groupState{}
	}
	return &seg, nil
}

// cleanup 清理整个落盘根目录。
func (s *segmentStore) cleanup() error {
	return os.RemoveAll(s.root)
}

// hasStaging 报告某分区是否还残留未确认的暂存尝试（测试用）。
// 注意：abort 会删除整个 staging 目录，因此目录不存在即视为干净。
func (s *segmentStore) hasStaging(partition int) (bool, error) {
	staging := filepath.Join(s.partitionDir(partition), "staging")
	entries, err := os.ReadDir(staging)
	if os.IsNotExist(err) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	for _, e := range entries {
		if e.IsDir() && strings.HasPrefix(e.Name(), "attempt-") {
			return true, nil
		}
	}
	return false, nil
}
