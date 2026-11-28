// Package display 实现保密工作站盲文点显器的设备输出协调器。
//
// 协调器把一行盲文拆成若干异步写命令，每条命令带有单调递增的写序号
// (WriteSeq) 与发出时的连接世代 (Generation)。设备可以乱序完成命令，
// 协调器在回执中核对物理格位、连接世代与写序号，并负责读者切换时的
// 格位清场安全：只有某格位上一读者最后一次可能带密的写入已经收尾，
// 且一次后续清零在当前连接世代得到确认，该格位才允许分给新读者。
package display

import (
	"errors"
	"sync"
)

// Pattern 是一个物理格位的盲文点位值，零值 Pattern(0) 表示该格位清零，
// 即所有触点落下、不携带任何盲文信息。
type Pattern uint8

// ReaderID 标识工作站上的读者。
type ReaderID string

// Generation 是设备连接世代。每次设备重连世代号加一，旧连接上迟到的
// 成功回执世代号与当前世代不同，不能推进新连接的任何状态。
type Generation uint64

// WriteSeq 是协调器范围内单调递增的写序号，从 1 开始。
type WriteSeq uint64

// CellState 描述单个物理格位在协调器视角下的状态。
type CellState int

const (
	// CellFree：格位空闲且已核验为清零，可分给读者。
	CellFree CellState = iota
	// CellActive：格位归当前读者所有，正承载其内容。
	CellActive
	// CellClearing：读者切换冻结中，正在对上一读者的格位做清场。
	CellClearing
	// CellReleasable：清场完成，格位可以安全分给新读者。
	CellReleasable
)

func (s CellState) String() string {
	switch s {
	case CellFree:
		return "free"
	case CellActive:
		return "active"
	case CellClearing:
		return "clearing"
	case CellReleasable:
		return "releasable"
	default:
		return "unknown"
	}
}

// 协调器对外的哨兵错误。
var (
	ErrNoDevice        = errors.New("display: device is nil")
	ErrInvalidCells    = errors.New("display: physical cell count must be positive")
	ErrCellOutOfRange  = errors.New("display: physical cell index out of range")
	ErrFrozen          = errors.New("display: output is frozen during reader switch")
	ErrNotOwned        = errors.New("display: cell is not owned by the current reader")
	ErrNoReader        = errors.New("display: no active reader")
	ErrReaderActive    = errors.New("display: reader already active; switch first")
	ErrNothingToSwitch = errors.New("display: no active reader to switch away from")
	ErrNotSwitching    = errors.New("display: coordinator is not in a reader switch")
	ErrCellNotFree     = errors.New("display: cell is not available for allocation")
	ErrPanOutOfRange   = errors.New("display: logical line pan offset out of range")
	ErrEmptyLine       = errors.New("display: logical line is empty")
)

// Receipt 是设备完成一条写命令后产生的回执。
type Receipt struct {
	// Cell 是被写入的物理格位。
	Cell int
	// Generation 是该命令发出时的连接世代。
	Generation Generation
	// Seq 是该命令的写序号。
	Seq WriteSeq
	// Value 是实际落到物理格位上的点位值。
	Value Pattern
	// OK 为 false 表示设备写入失败；失败的清零同样不能视为清场完成。
	OK bool
}

// WriteCommand 是发给设备的一条异步写命令。
type WriteCommand struct {
	Cell       int
	Generation Generation
	Seq        WriteSeq
	Value      Pattern
	Sensitive  bool
}

// Device 抽象点显器硬件。Submit 必须异步执行，完成后通过 sink 回送回执；
// 设备自身允许乱序完成。Submit 同步返回错误表示命令未被设备接受。
type Device interface {
	Submit(cmd WriteCommand, sink ReceiptSink) error
}

// ReceiptSink 接收设备回执。
type ReceiptSink interface {
	OnReceipt(r Receipt)
}

// CellStatus 是格位状态的只读快照。
type CellStatus struct {
	Cell             int
	State            CellState
	Owner            ReaderID
	LastPattern      Pattern
	LastSensitiveSeq WriteSeq // 当前世代最后一次可能带密写入的序号，0 表示无
	ClearSeq         WriteSeq // 当前世代后续清零的序号，0 表示尚未清零
	Outstanding      int      // 当前世代尚未确认的命令数
	Verified         bool     // 当前世代是否已有成功回执落到该格位
}

// record 跟踪一条已发出的命令，直到它的回执到达。
type record struct {
	cell      int
	gen       Generation
	seq       WriteSeq
	value     Pattern
	sensitive bool
}

// cellState 跟踪每个物理格位的清场进度。
type cellState struct {
	state       CellState
	owner       ReaderID
	last        Pattern  // 最后一次发出的点位
	lastSensSeq WriteSeq // 当前世代最后一次敏感写入序号
	clearSeq    WriteSeq // 当前世代后续清零序号（0 表示无）
	pending     map[WriteSeq]bool
	verified    bool // 当前世代是否有成功回执
}

// Coordinator 是设备输出协调器。零值不可用，必须经 New 构造。
type Coordinator struct {
	mu       sync.Mutex
	dev      Device
	n        int
	gen      Generation
	mode     coordinatorMode
	reader   ReaderID
	nextSeq  WriteSeq
	cells    []cellState
	registry map[WriteSeq]*record
}

type coordinatorMode int

const (
	modeActive coordinatorMode = iota
	modeSwitching
)

// New 创建协调器。物理设备初始视为全清零，所有格位为 CellFree。
func New(dev Device, physicalCells int) (*Coordinator, error) {
	if dev == nil {
		return nil, ErrNoDevice
	}
	if physicalCells <= 0 {
		return nil, ErrInvalidCells
	}
	c := &Coordinator{
		dev:      dev,
		n:        physicalCells,
		gen:      1,
		mode:     modeActive,
		nextSeq:  1,
		cells:    make([]cellState, physicalCells),
		registry: make(map[WriteSeq]*record),
	}
	for i := range c.cells {
		c.cells[i].pending = make(map[WriteSeq]bool)
	}
	return c, nil
}

// Generation 返回当前连接世代。
func (c *Coordinator) Generation() Generation {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.gen
}

// Cells 返回物理格位数。
func (c *Coordinator) Cells() int { return c.n }

// StartReader 在协调器空闲时登记当前读者。
func (c *Coordinator) StartReader(id ReaderID) error {
	if id == "" {
		return errors.New("display: reader id must not be empty")
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.mode != modeActive || c.reader != "" {
		return ErrReaderActive
	}
	c.reader = id
	return nil
}

// CurrentReader 返回当前读者，切换冻结期间返回的是被冻结的上一读者。
func (c *Coordinator) CurrentReader() ReaderID {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.reader
}

// Switching 报告协调器是否处于读者切换冻结中。
func (c *Coordinator) Switching() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.mode == modeSwitching
}

// issue 在持锁状态下登记并发出一条写命令。
func (c *Coordinator) issue(cell int, value Pattern, sensitive bool) (WriteSeq, error) {
	if cell < 0 || cell >= c.n {
		return 0, ErrCellOutOfRange
	}
	seq := c.nextSeq
	c.nextSeq++
	rec := &record{cell: cell, gen: c.gen, seq: seq, value: value, sensitive: sensitive}
	c.registry[seq] = rec

	cs := &c.cells[cell]
	cs.pending[seq] = true
	cs.verified = false
	cs.last = value
	if sensitive {
		cs.lastSensSeq = seq
	}
	if value == 0 {
		// 任意一次清零都记录为后续清零；后发的清零序号更大。
		cs.clearSeq = seq
	}

	cmd := WriteCommand{Cell: cell, Generation: c.gen, Seq: seq, Value: value, Sensitive: sensitive}
	if err := c.dev.Submit(cmd, c); err != nil {
		delete(c.registry, seq)
		delete(cs.pending, seq)
		return 0, err
	}
	return seq, nil
}

// WriteCell 向归当前读者所有的物理格位写入。切换冻结期间一律拒绝，
// 即上一读者的待发内容被冻结、不能再追加。
func (c *Coordinator) WriteCell(cell int, value Pattern, sensitive bool) (WriteSeq, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.mode == modeSwitching {
		return 0, ErrFrozen
	}
	if c.reader == "" {
		return 0, ErrNoReader
	}
	if cell < 0 || cell >= c.n {
		return 0, ErrCellOutOfRange
	}
	cs := &c.cells[cell]
	switch cs.state {
	case CellFree:
		// 空闲格位隐式分给当前读者。
		cs.state = CellActive
		cs.owner = c.reader
	case CellActive:
		if cs.owner != c.reader {
			return 0, ErrNotOwned
		}
	default:
		return 0, ErrNotOwned
	}
	return c.issue(cell, value, sensitive)
}

// SwitchReader 冻结当前读者并进入清场：所有承载上一读者内容的格位转为
// CellClearing。它只冻结，不自动清零——清零范围（整行或半行）由调用方
// 通过 ClearRange / ClearAll 决定，以便演示半行清零。
func (c *Coordinator) SwitchReader() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.mode != modeActive || c.reader == "" {
		return ErrNothingToSwitch
	}
	c.mode = modeSwitching
	for i := range c.cells {
		switch c.cells[i].state {
		case CellActive:
			// 承载上一读者内容的格位进入清场，必须等敏感写入收尾与清零确认。
			c.cells[i].state = CellClearing
		case CellFree:
			// 从未承载内容的格位本就核验为空白，可直接开放；一旦期间重连，
			// Reconnect 会把它回退为 CellClearing 并要求新世代清零确认。
			c.cells[i].state = CellReleasable
		}
	}
	return nil
}

// ClearRange 对半开区间 [from, to) 内处于清场中、或已可开放的格位发出
// 清零命令。前者推进清场，后者用于乱序回执后再补一次清零刷新物理格位；
// 空闲或已分配的格位跳过。
func (c *Coordinator) ClearRange(from, to int) ([]WriteSeq, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.mode != modeSwitching {
		return nil, ErrNotSwitching
	}
	if from < 0 || to > c.n || from > to {
		return nil, ErrCellOutOfRange
	}
	var seqs []WriteSeq
	for cell := from; cell < to; cell++ {
		switch c.cells[cell].state {
		case CellClearing, CellReleasable:
			// 清场中或已可开放（用于乱序后再补一次清零刷新物理格位）。
		default:
			continue
		}
		seq, err := c.issue(cell, 0, false)
		if err != nil {
			return seqs, err
		}
		seqs = append(seqs, seq)
	}
	return seqs, nil
}

// ClearAll 对全部仍在清场中的格位发出清零命令。
func (c *Coordinator) ClearAll() ([]WriteSeq, error) {
	return c.ClearRange(0, c.n)
}

// Allocate 把一个已完成清场（CellReleasable）的格位显式分给新读者。
func (c *Coordinator) Allocate(cell int, newReader ReaderID) error {
	if newReader == "" {
		return errors.New("display: reader id must not be empty")
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if cell < 0 || cell >= c.n {
		return ErrCellOutOfRange
	}
	cs := &c.cells[cell]
	if cs.state != CellReleasable {
		return ErrCellNotFree
	}
	cs.state = CellActive
	cs.owner = newReader
	cs.verified = true // 清场成功回执已核验过该格位为清零
	return nil
}

// FinishSwitch 结束切换：所有已清场完成的格位开放给新读者，仍在清场中的
// 格位保持冻结。返回成功开放的物理格位编号。新读者在开放格位上即变为
// 活动状态；若没有任何格位可开放则返回错误。
func (c *Coordinator) FinishSwitch(newReader ReaderID) ([]int, error) {
	if newReader == "" {
		return nil, errors.New("display: reader id must not be empty")
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.mode != modeSwitching {
		return nil, ErrNotSwitching
	}
	var opened []int
	for i := range c.cells {
		cs := &c.cells[i]
		if cs.state == CellReleasable {
			cs.state = CellActive
			cs.owner = newReader
			cs.verified = true
			opened = append(opened, i)
		}
	}
	if len(opened) == 0 {
		return nil, ErrCellNotFree
	}
	c.reader = newReader
	c.mode = modeActive
	return opened, nil
}

// ReleasableCells 返回当前已完成清场、可分给新读者的物理格位编号。
func (c *Coordinator) ReleasableCells() []int {
	c.mu.Lock()
	defer c.mu.Unlock()
	var out []int
	for i := range c.cells {
		if c.cells[i].state == CellReleasable {
			out = append(out, i)
		}
	}
	return out
}

// Status 返回全部格位的只读快照。
func (c *Coordinator) Status() []CellStatus {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]CellStatus, c.n)
	for i := range c.cells {
		cs := &c.cells[i]
		out[i] = CellStatus{
			Cell:             i,
			State:            cs.state,
			Owner:            cs.owner,
			LastPattern:      cs.last,
			LastSensitiveSeq: cs.lastSensSeq,
			ClearSeq:         cs.clearSeq,
			Outstanding:      len(cs.pending),
			Verified:         cs.verified,
		}
	}
	return out
}

// Outstanding 返回尚未收到回执的命令数（含被重连作废的旧世代命令）。
func (c *Coordinator) Outstanding() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.registry)
}

// dropPending 移除一条已结束的待确认序号，并判断当前世代的清场是否已完成。
func (cs *cellState) dropPending(seq WriteSeq) {
	delete(cs.pending, seq)
}

// recompute 在每次成功回执后重新评估格位是否满足开放条件：
//  1. 格位处于清场中；
//  2. 当前世代没有任何未确认命令（最后一次敏感写入已收尾）；
//  3. 当前世代存在一次晚于最后敏感写入的清零命令。
//
// 条件 2 已保证条件 3 中的清零命令（以及所有当前世代命令）都已收尾；
// 不能用“序号不在 pending 中”判断确认，因为重连后旧序号会被摘除，
// 未发出的新清零同样不在集合中。
func (cs *cellState) recompute() {
	if cs.state != CellClearing {
		return
	}
	if len(cs.pending) != 0 {
		return
	}
	if cs.clearSeq == 0 {
		return
	}
	if cs.lastSensSeq != 0 && cs.lastSensSeq >= cs.clearSeq {
		return
	}
	cs.state = CellReleasable
}

// OnReceipt 实现 ReceiptSink。设备（含乱序、迟到的回执）统一从这里进入。
func (c *Coordinator) OnReceipt(r Receipt) {
	c.mu.Lock()
	defer c.mu.Unlock()

	rec, ok := c.registry[r.Seq]
	if !ok {
		// 未知或重复回执：忽略。
		return
	}
	if rec.cell != r.Cell || rec.gen != r.Generation {
		// 回执与命令登记不符，忽略，不推进任何状态。
		return
	}

	// 旧连接的迟到回执（无论成功与否）不能推进新连接清场：登记项可以回收
	// （这条回执已经收到），但格位的待确认集合与清场状态一律不触碰——
	// 重连时旧序号已从格位集合中摘除，这里也不会再做任何修改。
	if r.Generation != c.gen {
		delete(c.registry, r.Seq)
		return
	}
	delete(c.registry, r.Seq)

	cs := &c.cells[r.Cell]
	cs.dropPending(r.Seq)
	if !r.OK {
		// 写入失败：格位保持未核验，清场不视为完成，需要重新发清零。
		cs.verified = false
		return
	}
	cs.verified = true
	cs.recompute()
}

// Reconnect 处理设备重连：连接世代加一，所有在途旧命令作废，所有格位在
// 新世代重新变为未核验。切换冻结中的格位自动在新世代重发清零；活动模式
// 下重发各格位最后一次内容（清零格位重发清零），保证新连接状态可确认。
// 返回新世代重发命令的序号。
func (c *Coordinator) Reconnect() (Generation, []WriteSeq, error) {
	c.mu.Lock()
	defer c.mu.Unlock()

	c.gen++
	newGen := c.gen
	for _, rec := range c.registry {
		// 旧记录保留在 registry 中直到其迟到回执到达并被世代核对丢弃，
		// 但要从格位的当前世代待确认集合中摘除。
		if cs := &c.cells[rec.cell]; rec.gen != newGen {
			delete(cs.pending, rec.seq)
		}
	}
	var reissued []WriteSeq
	for i := range c.cells {
		cs := &c.cells[i]
		// 新世代：清场进度全部作废，需要新的清零确认。切换中已经判定为
		// 可释放的格位同样回退——物理设备在新连接上的状态未知。
		if c.mode == modeSwitching && cs.state == CellReleasable {
			cs.state = CellClearing
		}
		cs.lastSensSeq = 0
		cs.clearSeq = 0
		cs.verified = false
		cs.pending = make(map[WriteSeq]bool)
	}
	var err error
	reissued, err = c.resyncLocked()
	return newGen, reissued, err
}

// resyncLocked 在当前世代重新发出各格位所需命令。
func (c *Coordinator) resyncLocked() ([]WriteSeq, error) {
	var seqs []WriteSeq
	for i := range c.cells {
		cs := &c.cells[i]
		switch c.mode {
		case modeSwitching:
			// 切换中的所有非活动归属格位（清场中、已回退、空闲）都重发清零，
			// 确保新世代每个待开放格位都有一次本世代的清零确认。
			if cs.state == CellActive {
				continue
			}
			seq, err := c.issue(i, 0, false)
			if err != nil {
				return seqs, err
			}
			seqs = append(seqs, seq)
		case modeActive:
			// 只重发当前读者所有格位的最后一次内容。未清场完的冻结格位
			// (CellClearing) 绝不重发旧密信息，而是发清零；从未写过的
			// 空闲格位同样发清零以核验空白。
			value := Pattern(0)
			if cs.state == CellActive && cs.owner == c.reader {
				value = cs.last
			}
			seq, err := c.issue(i, value, false)
			if err != nil {
				return seqs, err
			}
			seqs = append(seqs, seq)
		}
	}
	return seqs, nil
}
