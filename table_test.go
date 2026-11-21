package rowtable

import (
	"errors"
	"reflect"
	"testing"
)

func mustCommit(t *testing.T, tx *Txn) {
	t.Helper()
	if err := tx.Commit(); err != nil {
		t.Fatalf("commit failed: %v", err)
	}
}

func mustGet(t *testing.T, tx *Txn, key, want string) {
	t.Helper()
	got, ok := tx.Get(key)
	if !ok {
		t.Fatalf("Get(%q): key not found, want %q", key, want)
	}
	if string(got) != want {
		t.Fatalf("Get(%q) = %q, want %q", key, got, want)
	}
}

func mustNotGet(t *testing.T, tx *Txn, key string) {
	t.Helper()
	if got, ok := tx.Get(key); ok {
		t.Fatalf("Get(%q) = %q, want not found", key, got)
	}
}

func assertVersions(t *testing.T, tbl *Table, key string, want []uint64) {
	t.Helper()
	got := tbl.Versions(key)
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("Versions(%q) = %v, want %v", key, got, want)
	}
}

// 可接受结果：基本的开始事务、读写、删除、提交、回滚闭环。
func TestBasicTxnLifecycle(t *testing.T) {
	tbl := NewTable()

	// 写入并提交。
	w := tbl.Begin()
	if err := w.Put("k1", []byte("v1")); err != nil {
		t.Fatalf("Put: %v", err)
	}
	// 提交前本事务可见自己的修改。
	mustGet(t, w, "k1", "v1")
	mustCommit(t, w)

	// 新事务读到已提交的值。
	r := tbl.Begin()
	mustGet(t, r, "k1", "v1")
	r.Rollback()

	// 删除并提交后读不到。
	d := tbl.Begin()
	if err := d.Delete("k1"); err != nil {
		t.Fatalf("Delete: %v", err)
	}
	// 删除在提交前对本事务生效。
	mustNotGet(t, d, "k1")
	mustCommit(t, d)

	after := tbl.Begin()
	mustNotGet(t, after, "k1")
	after.Rollback()

	// 回滚丢弃未提交修改。
	rb := tbl.Begin()
	if err := rb.Put("k2", []byte("x")); err != nil {
		t.Fatalf("Put: %v", err)
	}
	rb.Rollback()
	check := tbl.Begin()
	mustNotGet(t, check, "k2")
	check.Rollback()

	// 已关闭的事务拒绝写入。
	if err := rb.Put("k3", []byte("y")); !errors.Is(err, ErrTxnClosed) {
		t.Fatalf("Put on closed txn: got %v, want ErrTxnClosed", err)
	}
	if err := rb.Commit(); !errors.Is(err, ErrTxnClosed) {
		t.Fatalf("Commit on closed txn: got %v, want ErrTxnClosed", err)
	}
}

// 快照隔离：读事务固定在开始时已提交的版本，另可看到自己的修改。
func TestSnapshotIsolationAndOwnWrites(t *testing.T) {
	tbl := NewTable()

	seed := tbl.Begin()
	if err := seed.Put("k", []byte("old")); err != nil {
		t.Fatalf("Put: %v", err)
	}
	mustCommit(t, seed)

	// 长读事务在写入提交前开启。
	r := tbl.Begin()

	w := tbl.Begin()
	if err := w.Put("k", []byte("new")); err != nil {
		t.Fatalf("Put: %v", err)
	}
	mustCommit(t, w)

	// 读事务仍看到旧值；新事务看到新值。
	mustGet(t, r, "k", "old")
	fresh := tbl.Begin()
	mustGet(t, fresh, "k", "new")
	fresh.Rollback()

	// 读事务自己的未提交修改对自己可见，且不影响其快照基底。
	if err := r.Put("tmp", []byte("mine")); err != nil {
		t.Fatalf("Put: %v", err)
	}
	mustGet(t, r, "tmp", "mine")
	mustGet(t, r, "k", "old")
	r.Rollback()

	// 回滚后自己的修改不外泄。
	other := tbl.Begin()
	mustNotGet(t, other, "tmp")
	other.Rollback()
}

// 应被拒绝的结果：并发写同一行，快照建立后别人已提交，后提交的一方被拒绝。
// 边界/交错：写不同键互不冲突；快照建立在对方提交之后则不冲突。
func TestWriteConflictFirstCommitterWins(t *testing.T) {
	tbl := NewTable()

	seed := tbl.Begin()
	if err := seed.Put("a", []byte("1")); err != nil {
		t.Fatalf("Put: %v", err)
	}
	if err := seed.Put("b", []byte("1")); err != nil {
		t.Fatalf("Put: %v", err)
	}
	mustCommit(t, seed)

	// 两个事务基于同一快照并发写同一行。
	tx1 := tbl.Begin()
	tx2 := tbl.Begin()
	if err := tx1.Put("a", []byte("2")); err != nil {
		t.Fatalf("Put: %v", err)
	}
	if err := tx2.Put("a", []byte("3")); err != nil {
		t.Fatalf("Put: %v", err)
	}
	mustCommit(t, tx1)
	if err := tx2.Commit(); !errors.Is(err, ErrConflict) {
		t.Fatalf("tx2.Commit: got %v, want ErrConflict", err)
	}

	// 冲突方的修改未生效。
	r := tbl.Begin()
	mustGet(t, r, "a", "2")
	r.Rollback()

	// 交错边界：两个并发事务写不同的键，都应提交成功。
	tx3 := tbl.Begin()
	tx4 := tbl.Begin()
	if err := tx3.Put("a", []byte("4")); err != nil {
		t.Fatalf("Put: %v", err)
	}
	if err := tx4.Put("b", []byte("2")); err != nil {
		t.Fatalf("Put: %v", err)
	}
	mustCommit(t, tx3)
	mustCommit(t, tx4)

	// 边界：快照建立在对方提交之后，写同一行不冲突。
	tx5 := tbl.Begin()
	if err := tx5.Put("a", []byte("5")); err != nil {
		t.Fatalf("Put: %v", err)
	}
	mustCommit(t, tx5)

	final := tbl.Begin()
	mustGet(t, final, "a", "5")
	mustGet(t, final, "b", "2")
	final.Rollback()
}

// 并发删除与并发写入同一行同样构成冲突。
func TestDeleteWriteConflict(t *testing.T) {
	tbl := NewTable()

	seed := tbl.Begin()
	if err := seed.Put("k", []byte("v")); err != nil {
		t.Fatalf("Put: %v", err)
	}
	mustCommit(t, seed)

	w := tbl.Begin()
	d := tbl.Begin()
	if err := w.Put("k", []byte("v2")); err != nil {
		t.Fatalf("Put: %v", err)
	}
	if err := d.Delete("k"); err != nil {
		t.Fatalf("Delete: %v", err)
	}
	mustCommit(t, d)
	if err := w.Commit(); !errors.Is(err, ErrConflict) {
		t.Fatalf("w.Commit: got %v, want ErrConflict", err)
	}

	r := tbl.Begin()
	mustNotGet(t, r, "k")
	r.Rollback()
}

// 回收语义：两个长读事务夹着一次删除和一次原键重建，
// 分别结束读事务应释放不同的版本；任何视图都看不到的版本被清掉。
func TestGCReleasesVersionsAsReadersFinish(t *testing.T) {
	tbl := NewTable()

	// 版本1：原始值。
	w1 := tbl.Begin()
	if err := w1.Put("k", []byte("v1")); err != nil {
		t.Fatalf("Put: %v", err)
	}
	mustCommit(t, w1)

	// 长读事务 R1：快照含原始值。
	r1 := tbl.Begin()

	// 版本2：删除墓碑。
	d := tbl.Begin()
	if err := d.Delete("k"); err != nil {
		t.Fatalf("Delete: %v", err)
	}
	mustCommit(t, d)

	// 长读事务 R2：快照含删除状态。
	r2 := tbl.Begin()

	// 版本3：原键重建。
	w3 := tbl.Begin()
	if err := w3.Put("k", []byte("v3")); err != nil {
		t.Fatalf("Put: %v", err)
	}
	mustCommit(t, w3)

	// 回收前三个版本都在。
	assertVersions(t, tbl, "k", []uint64{1, 2, 3})

	// 回收后：R1、R2 与新事务各自读到原本的值/删除状态，三个版本都被保留。
	tbl.GC()
	assertVersions(t, tbl, "k", []uint64{1, 2, 3})
	mustGet(t, r1, "k", "v1")
	mustNotGet(t, r2, "k")
	fresh := tbl.Begin()
	mustGet(t, fresh, "k", "v3")
	fresh.Rollback()

	// 结束 R1：原始值版本（1）不再被任何视图需要，被清掉；
	// R2 仍读到删除状态。
	r1.Rollback()
	tbl.GC()
	assertVersions(t, tbl, "k", []uint64{2, 3})
	mustNotGet(t, r2, "k")

	// 结束 R2：删除墓碑版本（2）也被清掉，只剩最新版本。
	r2.Rollback()
	tbl.GC()
	assertVersions(t, tbl, "k", []uint64{3})

	// 此刻新开的事务读到重建后的值。
	n := tbl.Begin()
	mustGet(t, n, "k", "v3")
	n.Rollback()
}

// 回收后新事务同样能看到“删除状态”：全部读视图结束后，
// 最新版本是墓碑时，新事务读到键不存在。
func TestGCKeepsLatestTombstoneForNewTxns(t *testing.T) {
	tbl := NewTable()

	w := tbl.Begin()
	if err := w.Put("k", []byte("v")); err != nil {
		t.Fatalf("Put: %v", err)
	}
	mustCommit(t, w)

	r := tbl.Begin() // 快照含值

	d := tbl.Begin()
	if err := d.Delete("k"); err != nil {
		t.Fatalf("Delete: %v", err)
	}
	mustCommit(t, d)

	tbl.GC()
	// R 仍需要旧值，墓碑是新事务的可见版本：两个都保留。
	assertVersions(t, tbl, "k", []uint64{1, 2})
	mustGet(t, r, "k", "v")

	r.Rollback()
	tbl.GC()
	// 只剩墓碑，新事务读到删除状态。
	assertVersions(t, tbl, "k", []uint64{2})
	n := tbl.Begin()
	mustNotGet(t, n, "k")
	n.Rollback()
}

// 多视图共用同一快照序号时，回收按视图计数正确保留版本。
func TestGCWithSharedSnapshot(t *testing.T) {
	tbl := NewTable()

	w1 := tbl.Begin()
	if err := w1.Put("k", []byte("v1")); err != nil {
		t.Fatalf("Put: %v", err)
	}
	mustCommit(t, w1)

	// 两个读事务共享同一快照。
	r1 := tbl.Begin()
	r2 := tbl.Begin()

	w2 := tbl.Begin()
	if err := w2.Put("k", []byte("v2")); err != nil {
		t.Fatalf("Put: %v", err)
	}
	mustCommit(t, w2)

	tbl.GC()
	assertVersions(t, tbl, "k", []uint64{1, 2})

	// 只结束一个读者，旧版本仍被另一个读者需要。
	r1.Rollback()
	tbl.GC()
	assertVersions(t, tbl, "k", []uint64{1, 2})
	mustGet(t, r2, "k", "v1")

	r2.Rollback()
	tbl.GC()
	assertVersions(t, tbl, "k", []uint64{2})
}
