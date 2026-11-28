package display

import (
	"sync"
)

// MemoryDevice 是一个纯内存点显器，用于测试与演示。它缓存每条提交的命令，
// 既可以按提交顺序完成，也可以按指定序号乱序完成，或用 CompleteRandom
// 做随机乱序压力测试。物理格位初始为全清零。
type MemoryDevice struct {
	mu       sync.Mutex
	cells    []Pattern
	pending  map[WriteSeq]queuedCommand
	seqOrder []WriteSeq // 提交顺序，供 CompleteAll 顺序完成
	failNext bool
	failSeq  map[WriteSeq]bool
}

type queuedCommand struct {
	cmd  WriteCommand
	sink ReceiptSink
}

// NewMemoryDevice 创建 physicalCells 格的内存设备。
func NewMemoryDevice(physicalCells int) *MemoryDevice {
	return &MemoryDevice{
		cells:   make([]Pattern, physicalCells),
		pending: make(map[WriteSeq]queuedCommand),
		failSeq: make(map[WriteSeq]bool),
	}
}

// Submit 实现 Device：命令入队，等待显式完成。
func (m *MemoryDevice) Submit(cmd WriteCommand, sink ReceiptSink) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.failNext {
		m.failNext = false
		return errDeviceRejected
	}
	if m.failSeq[cmd.Seq] {
		delete(m.failSeq, cmd.Seq)
		return errDeviceRejected
	}
	m.pending[cmd.Seq] = queuedCommand{cmd: cmd, sink: sink}
	m.seqOrder = append(m.seqOrder, cmd.Seq)
	return nil
}

// Pending 返回尚未完成的命令序号。
func (m *MemoryDevice) Pending() []WriteSeq {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]WriteSeq, 0, len(m.pending))
	for seq := range m.pending {
		out = append(out, seq)
	}
	return out
}

// PendingAt 返回某物理格位上尚未完成的命令序号。
func (m *MemoryDevice) PendingAt(cell int) []WriteSeq {
	m.mu.Lock()
	defer m.mu.Unlock()
	var out []WriteSeq
	for seq, q := range m.pending {
		if q.cmd.Cell == cell {
			out = append(out, seq)
		}
	}
	return out
}

// Cell 读取物理格位当前点位。
func (m *MemoryDevice) Cell(cell int) Pattern {
	m.mu.Lock()
	defer m.mu.Unlock()
	if cell < 0 || cell >= len(m.cells) {
		return 0
	}
	return m.cells[cell]
}

// FailNext 让下一条 Submit 同步失败。
func (m *MemoryDevice) FailNext() {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.failNext = true
}

// FailSeq 让指定序号的 Submit 同步失败。
func (m *MemoryDevice) FailSeq(seq WriteSeq) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.failSeq[seq] = true
}

// Complete 完成指定序号的命令并回送回执，允许乱序调用。
// 成功时把点位落到物理格位；未知序号返回 false。
func (m *MemoryDevice) Complete(seq WriteSeq, ok bool) bool {
	m.mu.Lock()
	q, exists := m.pending[seq]
	if !exists {
		m.mu.Unlock()
		return false
	}
	delete(m.pending, seq)
	if ok {
		m.cells[q.cmd.Cell] = q.cmd.Value
	}
	m.mu.Unlock()

	// 回调在设备锁外进行，避免与协调器锁形成嵌套死锁。
	q.sink.OnReceipt(Receipt{
		Cell:       q.cmd.Cell,
		Generation: q.cmd.Generation,
		Seq:        q.cmd.Seq,
		Value:      q.cmd.Value,
		OK:         ok,
	})
	return true
}

// CompleteAll 按提交顺序完成全部在途命令。
func (m *MemoryDevice) CompleteAll(ok bool) int {
	m.mu.Lock()
	order := append([]WriteSeq(nil), m.seqOrder...)
	m.seqOrder = nil
	m.mu.Unlock()

	n := 0
	for _, seq := range order {
		if m.Complete(seq, ok) {
			n++
		}
	}
	return n
}

// CompleteRandom 用给定的伪随机源乱序完成全部在途命令。
func (m *MemoryDevice) CompleteRandom(rng rngShuffler, ok bool) int {
	m.mu.Lock()
	seqs := make([]WriteSeq, 0, len(m.pending))
	for seq := range m.pending {
		seqs = append(seqs, seq)
	}
	m.seqOrder = nil
	m.mu.Unlock()

	rng.Shuffle(len(seqs), func(i, j int) {
		seqs[i], seqs[j] = seqs[j], seqs[i]
	})
	n := 0
	for _, seq := range seqs {
		if m.Complete(seq, ok) {
			n++
		}
	}
	return n
}

// rngShuffler 是 math/rand.Rand 满足的最小接口，避免测试依赖具体类型。
type rngShuffler interface {
	Shuffle(n int, swap func(i, j int))
}

// errDeviceRejected 是设备同步拒单错误，暴露为 DeviceError 供上层断言。
var errDeviceRejected = DeviceError("device rejected command")

// DeviceError 是内存设备产生的错误类型。
type DeviceError string

func (e DeviceError) Error() string { return string(e) }
