package display

import (
	"errors"
	"math/rand"
	"sync"
	"testing"
)

// funcNew 构造一个带内存设备的协调器与设备，便于各用例复用。
func funcNew(t *testing.T, cells int) (*Coordinator, *MemoryDevice) {
	t.Helper()
	dev := NewMemoryDevice(cells)
	c, err := New(dev, cells)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return c, dev
}

func TestNewValidation(t *testing.T) {
	if _, err := New(nil, 4); !errors.Is(err, ErrNoDevice) {
		t.Fatalf("nil device: got %v", err)
	}
	dev := NewMemoryDevice(4)
	if _, err := New(dev, 0); !errors.Is(err, ErrInvalidCells) {
		t.Fatalf("zero cells: got %v", err)
	}
	c, _ := New(dev, 4)
	if got := c.Generation(); got != 1 {
		t.Fatalf("initial generation = %d, want 1", got)
	}
	for i, st := range c.Status() {
		if st.State != CellFree {
			t.Fatalf("cell %d initial state = %s, want free", i, st.State)
		}
	}
}

func TestWriteAndReceiptFields(t *testing.T) {
	c, dev := funcNew(t, 4)
	if err := c.StartReader("alice"); err != nil {
		t.Fatal(err)
	}
	seq1, err := c.WriteCell(0, Pattern(0x01), true)
	if err != nil {
		t.Fatal(err)
	}
	seq2, err := c.WriteCell(0, Pattern(0x02), true)
	if err != nil {
		t.Fatal(err)
	}
	if seq1 != 1 || seq2 != 2 {
		t.Fatalf("write seqs = %d,%d want 1,2", seq1, seq2)
	}
	st := c.Status()[0]
	if st.State != CellActive || st.Owner != "alice" || st.LastPattern != 0x02 {
		t.Fatalf("unexpected status: %+v", st)
	}
	if st.LastSensitiveSeq != 2 {
		t.Fatalf("last sensitive seq = %d, want 2", st.LastSensitiveSeq)
	}

	// 乱序确认：先确认第 2 条，再确认第 1 条。
	if !dev.Complete(seq2, true) {
		t.Fatal("complete seq2 failed")
	}
	if dev.Cell(0) != 0x02 {
		t.Fatalf("physical cell = %#x want 0x02", dev.Cell(0))
	}
	if !dev.Complete(seq1, true) {
		t.Fatal("complete seq1 failed")
	}
	if c.Outstanding() != 0 {
		t.Fatalf("outstanding = %d want 0", c.Outstanding())
	}

	// 重复回执必须被忽略且不产生副作用。
	dev.Complete(seq1, true)
	if c.Outstanding() != 0 {
		t.Fatalf("duplicate receipt changed registry: %d", c.Outstanding())
	}
}

func TestWriteCellValidationAndOwnership(t *testing.T) {
	c, _ := funcNew(t, 4)
	// 无读者。
	if _, err := c.WriteCell(0, 1, false); !errors.Is(err, ErrNoReader) {
		t.Fatalf("no reader: got %v", err)
	}
	if err := c.StartReader("alice"); err != nil {
		t.Fatal(err)
	}
	if _, err := c.WriteCell(-1, 1, false); !errors.Is(err, ErrCellOutOfRange) {
		t.Fatalf("neg cell: %v", err)
	}
	if _, err := c.WriteCell(4, 1, false); !errors.Is(err, ErrCellOutOfRange) {
		t.Fatalf("oob cell: %v", err)
	}
	if err := c.StartReader("bob"); !errors.Is(err, ErrReaderActive) {
		t.Fatalf("second reader: %v", err)
	}
}

func TestSwitchFreezeBlocksWrites(t *testing.T) {
	c, dev := funcNew(t, 4)
	_ = c.StartReader("alice")
	seq, _ := c.WriteCell(1, 0x03, true)
	if err := c.SwitchReader(); err != nil {
		t.Fatal(err)
	}
	if !c.Switching() {
		t.Fatal("should be switching")
	}
	// 冻结后不能再追加写上一读者的内容。
	if _, err := c.WriteCell(1, 0x04, true); !errors.Is(err, ErrFrozen) {
		t.Fatalf("write while frozen: %v", err)
	}
	// 已在途的命令仍可确认。
	dev.Complete(seq, true)
	if c.Status()[1].State != CellClearing {
		t.Fatalf("state after frozen ack = %s", c.Status()[1].State)
	}
	// 承载内容的格位 1 在清零确认前不可开放（空闲格位可直接开放）。
	for _, cell := range c.ReleasableCells() {
		if cell == 1 {
			t.Fatal("content-bearing cell released before clear ack")
		}
	}
}

func TestClearanceRequiresSensitiveDrainAndClearAck(t *testing.T) {
	c, dev := funcNew(t, 2)
	_ = c.StartReader("alice")
	// 两个格位各发一条敏感写入，均不确认，模拟切换时仍有在途密信息。
	s1, _ := c.WriteCell(0, 0x05, true)
	s2, _ := c.WriteCell(1, 0x06, true)
	_ = c.SwitchReader()

	// 冻结后不能再追加写上一读者的内容。
	if _, err := c.WriteCell(1, 0x07, true); !errors.Is(err, ErrFrozen) {
		t.Fatalf("frozen write: %v", err)
	}

	// 格位 0：先确认清零，敏感写入仍在途，不能开放。
	clears, err := c.ClearAll()
	if err != nil {
		t.Fatal(err)
	}
	if len(clears) != 2 {
		t.Fatalf("clear seqs = %v, want 2 (both active cells)", clears)
	}
	c1, c2 := clears[0], clears[1]
	dev.Complete(c1, true)
	if got := c.ReleasableCells(); len(got) != 0 {
		t.Fatalf("released before sensitive drain: %v", got)
	}
	// 敏感写入收尾后，清零已确认且晚于敏感写入，格位 0 立即开放。
	dev.Complete(s1, true)
	if got := c.ReleasableCells(); len(got) != 1 || got[0] != 0 {
		t.Fatalf("releasable = %v, want [0]", got)
	}

	// 格位 1：先收尾敏感写入，清零确认前不可开放。
	dev.Complete(s2, true)
	if got := c.ReleasableCells(); len(got) != 1 {
		t.Fatalf("cell 1 must not release before clear ack: %v", got)
	}
	dev.Complete(c2, true)
	if got := c.ReleasableCells(); len(got) != 2 {
		t.Fatalf("releasable = %v, want both cells", got)
	}

	// 开放给新读者。
	opened, err := c.FinishSwitch("bob")
	if err != nil {
		t.Fatal(err)
	}
	if len(opened) != 2 {
		t.Fatalf("opened = %v", opened)
	}
	if c.CurrentReader() != "bob" || c.Switching() {
		t.Fatal("switch not finished")
	}
	for _, st := range c.Status() {
		if st.State != CellActive || st.Owner != "bob" {
			t.Fatalf("cell %d not owned by bob: %+v", st.Cell, st)
		}
	}
}

func TestFailedClearMustBeReissued(t *testing.T) {
	c, dev := funcNew(t, 1)
	_ = c.StartReader("alice")
	s, _ := c.WriteCell(0, 0x07, true)
	_ = c.SwitchReader()
	dev.Complete(s, true)
	clears, _ := c.ClearAll()
	// 清零失败：不能开放。
	dev.Complete(clears[0], false)
	if got := c.ReleasableCells(); len(got) != 0 {
		t.Fatalf("released after failed clear: %v", got)
	}
	if c.Status()[0].Verified {
		t.Fatal("cell must be unverified after failed receipt")
	}
	// 重新清零并确认后开放。
	clears2, err := c.ClearAll()
	if err != nil {
		t.Fatal(err)
	}
	dev.Complete(clears2[0], true)
	if got := c.ReleasableCells(); len(got) != 1 {
		t.Fatalf("releasable after re-clear = %v", got)
	}
}

func TestStaleGenerationReceiptCannotAdvanceClearance(t *testing.T) {
	c, dev := funcNew(t, 2)
	_ = c.StartReader("alice")
	s1, _ := c.WriteCell(0, 0x09, true)
	s2, _ := c.WriteCell(1, 0x0a, true)
	_ = c.SwitchReader()
	clears, _ := c.ClearAll()

	// 设备重连：世代 +1，旧命令全部作废，切换中的格位在新世代重发清零。
	gen, reissued, err := c.Reconnect()
	if err != nil {
		t.Fatal(err)
	}
	if gen != 2 {
		t.Fatalf("generation after reconnect = %d want 2", gen)
	}
	if len(reissued) != 2 {
		t.Fatalf("reissued = %v, want 2 new-gen clears", reissued)
	}
	// registry 中仍保留 4 条旧世代在途记录（2 敏感 + 2 清零）。
	if c.Outstanding() != 6 {
		t.Fatalf("outstanding = %d want 6 (4 stale + 2 new)", c.Outstanding())
	}

	// 旧连接迟到的成功回执：世代不符，一律丢弃，不能推进新世代清场。
	dev.Complete(s1, true)
	dev.Complete(s2, true)
	for _, old := range clears {
		dev.Complete(old, true)
	}
	if got := c.ReleasableCells(); len(got) != 0 {
		t.Fatalf("stale receipts advanced clearance: %v", got)
	}
	// 新世代清零确认后才可开放。
	for _, seq := range reissued {
		dev.Complete(seq, true)
	}
	if got := c.ReleasableCells(); len(got) != 2 {
		t.Fatalf("releasable = %v, want 2 cells", got)
	}
	// 旧记录已在迟到回执时被摘除。
	if c.Outstanding() != 0 {
		t.Fatalf("outstanding after all acks = %d want 0", c.Outstanding())
	}
}

func TestReconnectDuringActiveMode(t *testing.T) {
	c, dev := funcNew(t, 2)
	_ = c.StartReader("alice")
	_, _ = c.WriteCell(0, 0x11, true)
	_, _ = c.WriteCell(1, 0x12, false)
	dev.CompleteAll(true)

	gen, reissued, err := c.Reconnect()
	if err != nil {
		t.Fatal(err)
	}
	if gen != 2 || len(reissued) != 2 {
		t.Fatalf("gen=%d reissued=%v", gen, reissued)
	}
	for _, st := range c.Status() {
		if st.Verified {
			t.Fatalf("cell %d verified before new-gen ack", st.Cell)
		}
	}
	// 旧世代迟到回执无效。
	dev.Complete(1, true)
	if c.Status()[0].Verified {
		t.Fatal("stale ack verified cell in new generation")
	}
	dev.CompleteAll(true)
	for _, st := range c.Status() {
		if !st.Verified {
			t.Fatalf("cell %d not verified after new-gen acks", st.Cell)
		}
	}
	if dev.Cell(0) != 0x11 || dev.Cell(1) != 0x12 {
		t.Fatalf("device content wrong: %#x %#x", dev.Cell(0), dev.Cell(1))
	}
}

func TestReconnectRevertsReleasableCell(t *testing.T) {
	c, dev := funcNew(t, 1)
	_ = c.StartReader("alice")
	s, _ := c.WriteCell(0, 0x13, true)
	_ = c.SwitchReader()
	dev.Complete(s, true)
	cl, _ := c.ClearAll()
	dev.Complete(cl[0], true)
	if len(c.ReleasableCells()) != 1 {
		t.Fatal("precondition: cell should be releasable")
	}
	_, reissued, err := c.Reconnect()
	if err != nil {
		t.Fatal(err)
	}
	if c.Status()[0].State != CellClearing {
		t.Fatalf("state after reconnect = %s, want clearing", c.Status()[0].State)
	}
	if len(c.ReleasableCells()) != 0 {
		t.Fatal("releasable must be reset by reconnect")
	}
	// 旧清零的迟到回执不能让它重新开放。
	dev.Complete(cl[0], true)
	if len(c.ReleasableCells()) != 0 {
		t.Fatal("stale clear ack reopened cell")
	}
	dev.Complete(reissued[0], true)
	if len(c.ReleasableCells()) != 1 {
		t.Fatal("new-gen clear should release cell")
	}
}

func TestLogicalLinePanAndShortLine(t *testing.T) {
	c, dev := funcNew(t, 3)
	_ = c.StartReader("alice")
	line := NewLogicalLine(c)
	values := []Pattern{1, 2, 3, 4, 5}
	sens := []bool{true, true, false, false, true}
	if err := line.LoadLine(values, sens); err != nil {
		t.Fatal(err)
	}
	// 初始窗口显示 1,2,3。
	initialPending := len(dev.Pending())
	if initialPending != 3 {
		t.Fatalf("pending after load = %d want 3", initialPending)
	}
	dev.CompleteAll(true)
	wantDevice(t, dev, []Pattern{1, 2, 3})

	// 平移 1 格：显示 2,3,4，三个位置点位都变了（1→2,2→3,3→4）。
	if err := line.Pan(1); err != nil {
		t.Fatal(err)
	}
	if got := len(dev.Pending()); got != 3 {
		t.Fatalf("pending after pan1 = %d want 3", got)
	}
	dev.CompleteAll(true)
	wantDevice(t, dev, []Pattern{2, 3, 4})

	// 平移到末端：逻辑行只剩 1 格，物理位置 1、2 清零。
	if err := line.Pan(4); err != nil {
		t.Fatal(err)
	}
	dev.CompleteAll(true)
	wantDevice(t, dev, []Pattern{5, 0, 0})

	// 边界：越界平移报错且状态不变。
	if err := line.Pan(5); !errors.Is(err, ErrPanOutOfRange) {
		t.Fatalf("pan oob: %v", err)
	}
	if err := line.Pan(-1); !errors.Is(err, ErrPanOutOfRange) {
		t.Fatalf("pan neg: %v", err)
	}
	if line.Offset() != 4 {
		t.Fatalf("offset after failed pan = %d", line.Offset())
	}

	// 短行加载：物理位置 1、2 发清零。
	short := []Pattern{9}
	if err := line.LoadLine(short, nil); err != nil {
		t.Fatal(err)
	}
	dev.CompleteAll(true)
	wantDevice(t, dev, []Pattern{9, 0, 0})
}

func TestLogicalLinePanSkipsUnchangedCells(t *testing.T) {
	c, dev := funcNew(t, 3)
	_ = c.StartReader("alice")
	line := NewLogicalLine(c)
	// 重复内容用于验证跳过：窗口 [1,1,2] -> 平移后 [1,2,1]。
	if err := line.LoadLine([]Pattern{1, 1, 2, 1}, nil); err != nil {
		t.Fatal(err)
	}
	dev.CompleteAll(true)
	if err := line.Pan(1); err != nil {
		t.Fatal(err)
	}
	// 位置 0: 1->1 跳过；位置 1: 1->2 写；位置 2: 2->1 写。
	if got := len(dev.Pending()); got != 2 {
		t.Fatalf("pending = %d want 2 (unchanged cell skipped)", got)
	}
	dev.CompleteAll(true)
	wantDevice(t, dev, []Pattern{1, 2, 1})
}

func TestLogicalRewrite(t *testing.T) {
	c, dev := funcNew(t, 3)
	_ = c.StartReader("alice")
	line := NewLogicalLine(c)
	_ = line.LoadLine([]Pattern{1, 2, 3}, []bool{false, false, false})
	dev.CompleteAll(true)

	// 窗口内局部改写为敏感内容。
	if err := line.RewriteLogical(1, 0x1f, true); err != nil {
		t.Fatal(err)
	}
	if got := len(dev.Pending()); got != 1 {
		t.Fatalf("rewrite pending = %d want 1", got)
	}
	seq := dev.Pending()[0]
	dev.CompleteAll(true)
	wantDevice(t, dev, []Pattern{1, 0x1f, 3})
	st := c.Status()[1]
	if st.LastSensitiveSeq != seq || st.LastPattern != 0x1f {
		t.Fatalf("rewrite status wrong: %+v", st)
	}

	// 窗口外改写只更新逻辑行，不产生命令：先把窗口右移一格。
	if err := line.Pan(1); err != nil {
		t.Fatal(err)
	}
	dev.CompleteAll(true)
	before := len(dev.Pending())
	if err := line.RewriteLogical(0, 0x2a, true); err != nil {
		t.Fatal(err)
	}
	if len(dev.Pending()) != before {
		t.Fatal("off-window rewrite must not issue a command")
	}
	_ = line.Pan(0) // 平移回去时应带出改写后的值。
	dev.CompleteAll(true)
	wantDevice(t, dev, []Pattern{0x2a, 0x1f, 3})

	// 越界。
	if err := line.RewriteLogical(9, 1, false); !errors.Is(err, ErrPanOutOfRange) {
		t.Fatalf("rewrite oob: %v", err)
	}
}

func TestAllocateGuards(t *testing.T) {
	c, _ := funcNew(t, 2)
	_ = c.StartReader("alice")
	_, _ = c.WriteCell(0, 1, true)
	// 活动格位不能直接分配。
	if err := c.Allocate(0, "bob"); !errors.Is(err, ErrCellNotFree) {
		t.Fatalf("allocate active: %v", err)
	}
	// 空闲格位也不能在非切换态分配（必须走清场流程）。
	if err := c.Allocate(1, "bob"); !errors.Is(err, ErrCellNotFree) {
		t.Fatalf("allocate free without switch: %v", err)
	}
	if _, err := c.FinishSwitch("bob"); !errors.Is(err, ErrNotSwitching) {
		t.Fatalf("finish without switch: %v", err)
	}
}

func TestDeviceRejectAndFailureReceipt(t *testing.T) {
	c, dev := funcNew(t, 1)
	_ = c.StartReader("alice")
	dev.FailNext()
	if _, err := c.WriteCell(0, 1, true); !errors.Is(err, errDeviceRejected) {
		t.Fatalf("rejected submit: %v", err)
	}
	if c.Outstanding() != 0 {
		t.Fatal("rejected command must not remain tracked")
	}
	// 拒单后协调器仍可正常工作。
	seq, err := c.WriteCell(0, 2, true)
	if err != nil {
		t.Fatal(err)
	}
	dev.Complete(seq, false)
	if c.Status()[0].Verified {
		t.Fatal("failed receipt must not verify")
	}
}

func TestHalfLineClearanceOutOfOrder(t *testing.T) {
	const cells = 8
	c, dev := funcNew(t, cells)
	_ = c.StartReader("alice")
	line := NewLogicalLine(c)
	vals := make([]Pattern, cells)
	sens := make([]bool, cells)
	for i := range vals {
		vals[i] = Pattern(0x20 + i)
		sens[i] = true // 整行都可能带密
	}
	if err := line.LoadLine(vals, sens); err != nil {
		t.Fatal(err)
	}
	// 留下格位 3 的敏感写入不确认，其余全部确认。
	held := dev.PendingAt(3)
	if len(held) != 1 {
		t.Fatalf("cell 3 pending = %v, want 1", held)
	}
	heldSeq := held[0]
	for _, seq := range dev.Pending() {
		if seq != heldSeq {
			dev.Complete(seq, true)
		}
	}

	if err := c.SwitchReader(); err != nil {
		t.Fatal(err)
	}
	// 只清零左半行 [0,4)。
	clears, err := c.ClearRange(0, cells/2)
	if err != nil {
		t.Fatal(err)
	}
	if len(clears) != 4 {
		t.Fatalf("half-line clears = %d want 4", len(clears))
	}
	// 右半行没有任何清零命令。
	if seqs := dev.PendingAt(5); len(seqs) != 0 {
		t.Fatalf("right half must have no clear: %v", seqs)
	}

	// 乱序确认左半行清零：先发的最后确认。
	for i := len(clears) - 1; i >= 0; i-- {
		dev.Complete(clears[i], true)
	}
	// 格位 3 的敏感写入仍在途：即使清零已确认，也不能开放。
	rel := c.ReleasableCells()
	if len(rel) != 3 || rel[0] != 0 || rel[1] != 1 || rel[2] != 2 {
		t.Fatalf("releasable while cell3 draining = %v, want [0 1 2]", rel)
	}
	// 被持有的敏感写入收尾：格位 3 立即开放。
	dev.Complete(heldSeq, true)
	rel = c.ReleasableCells()
	if len(rel) != 4 {
		t.Fatalf("releasable after drain = %v, want 4 left-half cells", rel)
	}
	for _, cell := range rel {
		if cell >= cells/2 {
			t.Fatalf("right-half cell %d must not be open", cell)
		}
	}
	// 格位 0..2 的敏感写入先于清零完成，物理上已归零；格位 3 的清零先于
	// 敏感写入完成，乱序下设备最终保留旧点位，需要再补一次清零。
	for cell := 0; cell < cells/2-1; cell++ {
		if dev.Cell(cell) != 0 {
			t.Fatalf("cell %d physical value = %#x want 0", cell, dev.Cell(cell))
		}
	}
	if dev.Cell(3) != vals[3] {
		t.Fatalf("cell 3 should retain stale value after out-of-order acks, got %#x", dev.Cell(3))
	}
	// 对格位 3 重发清零并确认后，物理格位归零且仍处于可开放状态。
	refreshed, err := c.ClearRange(3, 4)
	if err != nil {
		t.Fatal(err)
	}
	if len(refreshed) != 1 {
		t.Fatalf("re-clear seqs = %v want 1", refreshed)
	}
	dev.Complete(refreshed[0], true)
	if dev.Cell(3) != 0 {
		t.Fatalf("cell 3 after re-clear = %#x want 0", dev.Cell(3))
	}
	if len(c.ReleasableCells()) != 4 {
		t.Fatalf("releasable after re-clear = %v", c.ReleasableCells())
	}
	// 右半行仍处于清场中、物理上保留旧密信息。
	for cell := cells / 2; cell < cells; cell++ {
		if c.Status()[cell].State != CellClearing {
			t.Fatalf("right-half cell %d state = %s want clearing", cell, c.Status()[cell].State)
		}
		if dev.Cell(cell) == 0 {
			t.Fatalf("right-half cell %d should still hold old content", cell)
		}
	}

	// 新读者只能拿到左半行；FinishSwitch 后右半行仍冻结。
	opened, err := c.FinishSwitch("bob")
	if err != nil {
		t.Fatal(err)
	}
	if len(opened) != 4 {
		t.Fatalf("opened = %v want 4", opened)
	}
	if _, err := c.WriteCell(5, 1, false); !errors.Is(err, ErrNotOwned) {
		t.Fatalf("write to frozen right half: %v", err)
	}
}

func TestReconnectAfterPartialSwitchNeverResendsSecret(t *testing.T) {
	c, dev := funcNew(t, 4)
	_ = c.StartReader("alice")
	// 格位 0、1 承载带密内容；格位 2、3 从未使用。
	s0, _ := c.WriteCell(0, 0x41, true)
	s1, _ := c.WriteCell(1, 0x42, true)
	dev.Complete(s0, true)
	dev.Complete(s1, true)

	_ = c.SwitchReader()
	// 只清零左半行，右半行（含空闲格位 2、3）先不动。
	clears, _ := c.ClearRange(0, 2)
	dev.Complete(clears[0], true)
	dev.Complete(clears[1], true)
	opened, err := c.FinishSwitch("bob")
	if err != nil {
		t.Fatal(err)
	}
	if len(opened) != 4 {
		// 格位 0、1 清场完成；2、3 本就空白。
		t.Fatalf("opened = %v want 4", opened)
	}
	if c.Status()[1].State != CellActive || c.Status()[1].Owner != "bob" {
		t.Fatal("left half should now belong to bob")
	}

	// bob 在自己的格位上写入新内容并确认。
	b0, _ := c.WriteCell(0, 0x51, false)
	b1, _ := c.WriteCell(1, 0x52, true)
	dev.Complete(b0, true)
	dev.Complete(b1, true)

	// 新连接上重连：bob 的格位重发其新内容，空闲格位只能重发清零。
	gen, reissued, err := c.Reconnect()
	if err != nil {
		t.Fatal(err)
	}
	if gen != 2 || len(reissued) != 4 {
		t.Fatalf("gen=%d reissued=%v want 4 commands", gen, reissued)
	}
	dev.CompleteAll(true)
	if dev.Cell(0) != 0x51 || dev.Cell(1) != 0x52 {
		t.Fatalf("bob cells = %#x %#x want 0x51 0x52", dev.Cell(0), dev.Cell(1))
	}
	if dev.Cell(2) != 0 || dev.Cell(3) != 0 {
		t.Fatalf("unused cells = %#x %#x want zero", dev.Cell(2), dev.Cell(3))
	}
}

func TestReconnectFrozenCellGetsZeroNotSecret(t *testing.T) {
	c, dev := funcNew(t, 3)
	_ = c.StartReader("alice")
	// 格位 0 的敏感写入保持在途；格位 1 写入并确认；格位 2 从不使用。
	s, _ := c.WriteCell(0, 0x77, true)
	w1, _ := c.WriteCell(1, 0x88, true)
	dev.Complete(w1, true)

	_ = c.SwitchReader()
	// 清格格位 1，让切换至少有一个可开放格位（格位 0 因在途敏感写入仍冻结）。
	clears1, _ := c.ClearRange(1, 2)
	dev.Complete(clears1[0], true)
	opened, err := c.FinishSwitch("bob")
	if err != nil {
		t.Fatal(err)
	}
	if len(opened) != 2 {
		// 格位 1 清场完成，格位 2 本就空白。
		t.Fatalf("opened = %v want [1 2]", opened)
	}
	if c.Status()[0].State != CellClearing {
		t.Fatalf("cell 0 state = %s want clearing", c.Status()[0].State)
	}

	// 此时设备重连，新世代同步：冻结格位 0 绝不允许重发旧密 0x77。
	_, reissued, err := c.Reconnect()
	if err != nil {
		t.Fatal(err)
	}
	if len(reissued) != 3 {
		t.Fatalf("reissued = %v want 3", reissued)
	}
	// 只确认重连后重发的新世代命令，旧世代在途命令仍留在设备队列里。
	for _, seq := range reissued {
		if !dev.Complete(seq, true) {
			t.Fatalf("reissued seq %d missing from device", seq)
		}
	}
	if dev.Cell(0) != 0 {
		t.Fatalf("frozen cell resynced to secret %#x, want 0", dev.Cell(0))
	}
	// 新世代清零确认后格位 0 可开放（依据的是新连接上的清零，而非旧回执）。
	stateBefore := c.Status()[0].State
	if stateBefore != CellReleasable {
		t.Fatalf("cell 0 state after new-gen clear = %s want releasable", stateBefore)
	}
	outstandingBefore := c.Outstanding()
	// 旧敏感写入的迟到成功回执（世代 1）被世代检查丢弃：状态不变，
	// 仅其登记项被回收。
	if !dev.Complete(s, true) {
		t.Fatal("stale command should still be queued in the memory device")
	}
	if c.Status()[0].State != stateBefore {
		t.Fatalf("stale receipt changed cell state: before=%s after=%s",
			stateBefore, c.Status()[0].State)
	}
	if c.Outstanding() != outstandingBefore-1 {
		t.Fatalf("stale receipt registry handling: before=%d after=%d",
			outstandingBefore, c.Outstanding())
	}
	if c.Generation() != 2 {
		t.Fatalf("generation = %d want 2", c.Generation())
	}
}

func TestConcurrentSwitchAndOutOfOrderAcks(t *testing.T) {
	const cells = 12
	dev := NewMemoryDevice(cells)
	c, err := New(dev, cells)
	if err != nil {
		t.Fatal(err)
	}
	_ = c.StartReader("alice")

	var wg sync.WaitGroup
	// 并发写入各格位。
	for cell := 0; cell < cells; cell++ {
		wg.Add(1)
		go func(cell int) {
			defer wg.Done()
			for k := 0; k < 5; k++ {
				if _, err := c.WriteCell(cell, Pattern(cell+1), k == 4); err != nil {
					return // 切换后写入被冻结是预期行为
				}
			}
		}(cell)
	}
	wg.Wait()

	_ = c.SwitchReader()
	wg.Add(1)
	go func() {
		defer wg.Done()
		_, _ = c.ClearAll()
	}()

	// 随机乱序确认，直到全部完成。
	rng := rand.New(rand.NewSource(42))
	wg.Wait()
	for range [200]struct{}{} {
		pending := dev.Pending()
		if len(pending) == 0 {
			break
		}
		seq := pending[rng.Intn(len(pending))]
		dev.Complete(seq, true)
	}
	if len(dev.Pending()) != 0 {
		t.Fatalf("still %d pending after drain loop", len(dev.Pending()))
	}
	if len(c.ReleasableCells()) != cells {
		t.Fatalf("releasable = %v, want all %d cells", c.ReleasableCells(), cells)
	}
	opened, err := c.FinishSwitch("bob")
	if err != nil || len(opened) != cells {
		t.Fatalf("finish switch: opened=%v err=%v", opened, err)
	}
}

func TestRandomReconnectStress(t *testing.T) {
	const cells = 6
	dev := NewMemoryDevice(cells)
	c, _ := New(dev, cells)
	_ = c.StartReader("alice")
	rng := rand.New(rand.NewSource(7))

	for round := 0; round < 8; round++ {
		for cell := 0; cell < cells; cell++ {
			if rng.Intn(2) == 0 {
				_, _ = c.WriteCell(cell, Pattern(rng.Intn(63)+1), rng.Intn(2) == 0)
			}
		}
		// 随机乱序确认一部分。
		pending := dev.Pending()
		rng.Shuffle(len(pending), func(i, j int) { pending[i], pending[j] = pending[j], pending[i] })
		for _, seq := range pending[:len(pending)/2] {
			dev.Complete(seq, rng.Intn(10) != 0)
		}
		// 中途重连：旧回执之后再陆续到达。
		gen, _, err := c.Reconnect()
		if err != nil {
			t.Fatal(err)
		}
		for _, seq := range dev.Pending() {
			// 全部在途里混有旧世代命令；完成它们不应改变新世代核验结果。
			dev.Complete(seq, true)
		}
		if c.Generation() != gen {
			t.Fatalf("generation changed unexpectedly: %d", c.Generation())
		}
	}
}

func wantDevice(t *testing.T, dev *MemoryDevice, want []Pattern) {
	t.Helper()
	for i, w := range want {
		if got := dev.Cell(i); got != w {
			t.Fatalf("physical cell %d = %#x want %#x", i, got, w)
		}
	}
}
