// Package rowtable 实现关系引擎中一张以主键读取的内存行表。
//
// 并发模型为 MVCC 快照隔离：
//   - 每个事务在开始时固定一个快照序号，读事务只能看到开始时已提交的版本，
//     另可看到自己尚未提交的修改（read-your-own-writes）。
//   - 写事务提交时做首提交者胜出（first-committer-wins）检查：快照建立之后
//     若已有其他事务提交过同一行，则拒绝后提交的一方。
//   - 垃圾回收（GC）清理所有活动读视图与新事务都看不到的旧提交版本；
//     每个活动读视图和此刻新开的事务仍读到原本的值或删除状态。
package rowtable

import (
	"errors"
	"sync"
)

var (
	// ErrTxnClosed 表示事务已提交或回滚，不能再操作。
	ErrTxnClosed = errors.New("rowtable: transaction already closed")
	// ErrConflict 表示快照建立后同一行已被其他事务提交，本次提交被拒绝。
	ErrConflict = errors.New("rowtable: write conflict: row committed by another transaction after snapshot")
)

// Version 是一行的一个已提交版本。Value 为 nil 表示该版本是删除墓碑。
type Version struct {
	CommitID uint64
	Value    []byte
}

// Tombstone 报告该版本是否为删除墓碑。
func (v *Version) Tombstone() bool { return v.Value == nil }

type rowChain struct {
	versions []*Version // 按 CommitID 升序
}

type pendingWrite struct {
	value []byte
	del   bool
}

// Table 是以主键读取的内存行表。
type Table struct {
	mu        sync.Mutex
	rows      map[string]*rowChain
	commitSeq uint64         // 全局提交序号，单调递增
	readers   map[uint64]int // 活动读视图：快照序号 -> 持有该快照的事务数
}

// NewTable 创建一张空行表。
func NewTable() *Table {
	return &Table{
		rows:    make(map[string]*rowChain),
		readers: make(map[uint64]int),
	}
}

// Txn 是一个事务，可同时用于读和写。
type Txn struct {
	t        *Table
	snapshot uint64
	writes   map[string]pendingWrite
	done     bool
}

// Begin 开启事务，快照固定为当前已提交的最新状态。
func (t *Table) Begin() *Txn {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.readers[t.commitSeq]++
	return &Txn{t: t, snapshot: t.commitSeq, writes: make(map[string]pendingWrite)}
}

// CommitSeq 返回当前全局提交序号（最近一次提交的版本号）。
func (t *Table) CommitSeq() uint64 {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.commitSeq
}

// Snapshot 返回事务开始时固定的快照序号。
func (tx *Txn) Snapshot() uint64 { return tx.snapshot }

// Get 按主键读取：先看到自己的未提交修改，再看快照时已提交的版本。
// 第二个返回值表示键是否存在（已删除或不存在返回 false）。
func (tx *Txn) Get(key string) ([]byte, bool) {
	tx.t.mu.Lock()
	defer tx.t.mu.Unlock()
	if tx.done {
		return nil, false
	}
	if w, ok := tx.writes[key]; ok {
		if w.del {
			return nil, false
		}
		return cloneBytes(w.value), true
	}
	v := visibleVersion(tx.t.rows[key], tx.snapshot)
	if v == nil || v.Value == nil {
		return nil, false
	}
	return cloneBytes(v.Value), true
}

// Put 写入一行，提交前仅本事务可见。
func (tx *Txn) Put(key string, value []byte) error {
	tx.t.mu.Lock()
	defer tx.t.mu.Unlock()
	if tx.done {
		return ErrTxnClosed
	}
	tx.writes[key] = pendingWrite{value: cloneBytes(value)}
	return nil
}

// Delete 删除一行，提交前仅本事务可见。
func (tx *Txn) Delete(key string) error {
	tx.t.mu.Lock()
	defer tx.t.mu.Unlock()
	if tx.done {
		return ErrTxnClosed
	}
	tx.writes[key] = pendingWrite{del: true}
	return nil
}

// Commit 提交事务。若快照建立后同一行已被其他事务提交，返回 ErrConflict，
// 事务随之作废（等价于回滚）。
func (tx *Txn) Commit() error {
	t := tx.t
	t.mu.Lock()
	defer t.mu.Unlock()
	if tx.done {
		return ErrTxnClosed
	}
	// 首提交者胜出：任一待写行的最新提交版本晚于本事务快照即冲突。
	for key := range tx.writes {
		if rc := t.rows[key]; rc != nil && len(rc.versions) > 0 {
			if last := rc.versions[len(rc.versions)-1]; last.CommitID > tx.snapshot {
				tx.done = true
				t.releaseReaderLocked(tx.snapshot)
				return ErrConflict
			}
		}
	}
	t.commitSeq++
	id := t.commitSeq
	for key, w := range tx.writes {
		rc := t.rows[key]
		if rc == nil {
			rc = &rowChain{}
			t.rows[key] = rc
		}
		v := &Version{CommitID: id}
		if !w.del {
			v.Value = cloneBytes(w.value)
		}
		rc.versions = append(rc.versions, v)
	}
	tx.done = true
	t.releaseReaderLocked(tx.snapshot)
	return nil
}

// Rollback 回滚事务，丢弃全部未提交修改并释放读视图。
func (tx *Txn) Rollback() {
	t := tx.t
	t.mu.Lock()
	defer t.mu.Unlock()
	if tx.done {
		return
	}
	tx.writes = nil
	tx.done = true
	t.releaseReaderLocked(tx.snapshot)
}

func (t *Table) releaseReaderLocked(snapshot uint64) {
	if n := t.readers[snapshot]; n <= 1 {
		delete(t.readers, snapshot)
	} else {
		t.readers[snapshot] = n - 1
	}
}

// visibleVersion 返回快照 snapshot 下可见的版本：CommitID <= snapshot 的最新版本。
func visibleVersion(rc *rowChain, snapshot uint64) *Version {
	if rc == nil {
		return nil
	}
	vers := rc.versions
	lo, hi := 0, len(vers) // 找最后一个 CommitID <= snapshot
	for lo < hi {
		mid := (lo + hi) / 2
		if vers[mid].CommitID <= snapshot {
			lo = mid + 1
		} else {
			hi = mid
		}
	}
	if lo == 0 {
		return nil
	}
	return vers[lo-1]
}

// GC 回收所有活动读视图与“此刻新开的事务”都看不到的旧提交版本。
// 回收不改变任何活动读视图或新事务能读到的值/删除状态。
func (t *Table) GC() {
	t.mu.Lock()
	defer t.mu.Unlock()
	views := make([]uint64, 0, len(t.readers)+1)
	for s := range t.readers {
		views = append(views, s)
	}
	// 此刻新开的事务以当前提交序号为快照，必须纳入保留集合。
	views = append(views, t.commitSeq)
	for key, rc := range t.rows {
		rc.versions = gcVersions(rc.versions, views)
		if len(rc.versions) == 0 {
			delete(t.rows, key)
		}
	}
}

// gcVersions 保留至少被一个视图可见的版本。版本按 CommitID 升序，
// 版本 i 对视图 s 可见当且仅当 vers[i].CommitID <= s < vers[i+1].CommitID。
func gcVersions(vers []*Version, views []uint64) []*Version {
	kept := vers[:0]
	for i, v := range vers {
		upper := ^uint64(0) // 最后一个版本对所有更晚的视图可见
		if i+1 < len(vers) {
			upper = vers[i+1].CommitID - 1
		}
		for _, s := range views {
			if s >= v.CommitID && s <= upper {
				kept = append(kept, v)
				break
			}
		}
	}
	return kept
}

// Versions 返回主键 key 当前保留的已提交版本号（升序），供调用方查看。
func (t *Table) Versions(key string) []uint64 {
	t.mu.Lock()
	defer t.mu.Unlock()
	rc := t.rows[key]
	if rc == nil {
		return nil
	}
	ids := make([]uint64, len(rc.versions))
	for i, v := range rc.versions {
		ids[i] = v.CommitID
	}
	return ids
}

func cloneBytes(b []byte) []byte {
	if b == nil {
		return nil
	}
	c := make([]byte, len(b))
	copy(c, b)
	return c
}
