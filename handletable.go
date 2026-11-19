// Package handletable 实现一个通过小整数句柄访问私有对象的句柄表。
//
// 句柄是一个 uint64 值，由三个字段打包而成：
//
//	[ 类别(kindBits 位) | 代数(genBits 位) | 槽号(slotBits 位) ]
//
// 槽号位于最低位，保证在表容量范围内的句柄本身就是“小整数”。
// 调用方不直接持有对象指针，而是通过 Borrow 短时借用：借用成功返回
// 对象与一个 release 函数，归还前该槽不可被新注册复用。
package handletable

import (
	"errors"
	"sync"
)

// 句柄表可返回的哨兵错误。调用方应使用 errors.Is 判定，彼此可区分。
var (
	// ErrExhausted 表示所有槽都已被封存，句柄表永久耗尽。
	ErrExhausted = errors.New("handletable: all slots are sealed, table exhausted")
	// ErrNoSlot 表示当前没有可立即使用的槽（全部在借/未归还），
	// 但槽并未被封存，等待归还后仍可再用——属于暂时性结果。
	ErrNoSlot = errors.New("handletable: no slot currently available")
	// ErrStale 表示句柄已过期：槽号不存在、代数不匹配或槽已封存。
	ErrStale = errors.New("handletable: stale or invalid handle")
	// ErrWrongKind 表示句柄真实存在且代数正确，但对象类别不匹配。
	ErrWrongKind = errors.New("handletable: object kind mismatch")
	// ErrClosed 表示句柄对应的对象（或整张表）已经关闭。
	ErrClosed = errors.New("handletable: object is closed")
	// ErrNotBorrowed 表示归还一个当前并未处于借用状态的槽。
	ErrNotBorrowed = errors.New("handletable: slot is not currently borrowed")
)

const (
	// kindBits 固定占用句柄最高 8 位存放对象类别。
	kindBits = 8
)

// slotState 描述单个槽的生命周期状态。
type slotState uint8

const (
	slotFree     slotState = iota // 从未分配，或已关闭且无借用，可直接复用
	slotLive                      // 持有活跃对象，可接受借用
	slotDraining                  // 已请求关闭，等待所有借用归还后回收
	slotSealed                    // 代数将重复，永久封存，不可再用
)

// slot 保存每个槽的内部状态。
type slot struct {
	obj     any
	kind    uint8
	gen     uint64
	borrows int
	state   slotState
	used    bool // 该槽是否曾经安装过对象（区分全新槽与回收槽）
}

// Table 是并发安全的句柄表。
type Table struct {
	mu sync.Mutex

	slots []slot

	genBits  uint
	genMask  uint64
	slotMask uint64
	kindMask uint64

	// 预计算的字段位移。
	genShift  uint
	kindShift uint

	closed bool // 整个表是否已关闭
}

// New 创建一个容量为 slotCount、代数位宽为 genBits 的句柄表。
// genBits 在测试中允许小到 2，生产场景应取更大的位宽。
func New(slotCount int, genBits uint) (*Table, error) {
	if slotCount <= 0 {
		return nil, errors.New("handletable: slotCount must be positive")
	}
	if genBits == 0 || genBits >= 64-kindBits {
		return nil, errors.New("handletable: genBits out of range")
	}

	// 槽号需要的最小位宽，用于计算句柄布局。
	var slotBits uint
	for (1 << slotBits) < slotCount {
		slotBits++
	}
	if kindBits+genBits+slotBits > 64 {
		return nil, errors.New("handletable: handle layout exceeds 64 bits")
	}

	t := &Table{
		slots:     make([]slot, slotCount),
		genBits:   genBits,
		genMask:   (uint64(1) << genBits) - 1,
		slotMask:  (uint64(1) << slotBits) - 1,
		kindMask:  (uint64(1) << kindBits) - 1,
		genShift:  slotBits,
		kindShift: slotBits + genBits,
	}
	return t, nil
}

// Handle 是对外暴露的不透明句柄类型。
type Handle uint64

// Encode 按表的布局把槽号、代数、类别打包成句柄。
func (t *Table) Encode(slot int, gen uint64, kind uint8) Handle {
	return Handle(
		(uint64(kind)&t.kindMask)<<t.kindShift |
			(gen&t.genMask)<<t.genShift |
			(uint64(slot) & t.slotMask),
	)
}

// Decode 拆解句柄，返回槽号、代数与类别。槽号是否越界由调用方校验。
func (t *Table) Decode(h Handle) (slot int, gen uint64, kind uint8) {
	v := uint64(h)
	slot = int(v & t.slotMask)
	gen = (v >> t.genShift) & t.genMask
	kind = uint8((v >> t.kindShift) & t.kindMask)
	return
}

// SlotInfo 是槽状态的只读快照，供检查/测试使用。
type SlotInfo struct {
	Index     int
	State     string
	Gen       uint64
	Kind      uint8
	Borrows   int
	HasObject bool
}

// Inspect 返回所有槽的只读快照。
func (t *Table) Inspect() []SlotInfo {
	t.mu.Lock()
	defer t.mu.Unlock()

	stateName := map[slotState]string{
		slotFree:     "free",
		slotLive:     "live",
		slotDraining: "draining",
		slotSealed:   "sealed",
	}
	out := make([]SlotInfo, len(t.slots))
	for i := range t.slots {
		s := &t.slots[i]
		out[i] = SlotInfo{
			Index:     i,
			State:     stateName[s.state],
			Gen:       s.gen,
			Kind:      s.kind,
			Borrows:   s.borrows,
			HasObject: s.obj != nil,
		}
	}
	return out
}

// resolve 校验句柄并定位槽，返回可区分的错误：
// 先判过期（槽号/代数/封存），再判类别，最后判关闭。
func (t *Table) resolve(h Handle, wantKind uint8) (*slot, error) {
	idx, gen, kind := t.Decode(h)
	if idx < 0 || idx >= len(t.slots) {
		return nil, ErrStale
	}
	s := &t.slots[idx]
	if s.state == slotFree || s.state == slotSealed || s.gen != gen {
		return nil, ErrStale
	}
	if kind != wantKind || s.kind != wantKind {
		return nil, ErrWrongKind
	}
	if s.state == slotDraining {
		return nil, ErrClosed
	}
	return s, nil
}

// Register 在表中登记一个对象，返回后续借用所用的句柄。
//
// 复用规则：
//  1. 优先使用从未分配过的全新槽；
//  2. 其次复用已关闭回收的槽，代数按模 2^genBits 递增；若递增后回绕
//     （下一次复用将重复该槽曾经发出过的代数），封存该槽并继续尝试
//     其他回收槽；
//  3. 所有非封存槽都在借用中 -> ErrNoSlot（暂时性）；
//  4. 所有槽都被封存 -> ErrExhausted（永久性）。
func (t *Table) Register(obj any, kind uint8) (Handle, error) {
	if obj == nil {
		return 0, errors.New("handletable: cannot register nil object")
	}

	t.mu.Lock()
	defer t.mu.Unlock()

	if t.closed {
		return 0, ErrClosed
	}

	// 第一轮：全新槽优先，推迟代数回绕。
	for i := range t.slots {
		s := &t.slots[i]
		if s.state == slotFree && !s.used {
			s.gen = 0
			s.obj = obj
			s.kind = kind
			s.borrows = 0
			s.state = slotLive
			s.used = true
			return t.Encode(i, 0, kind), nil
		}
	}

	// 第二轮：回收槽复用；回绕则封存并尝试下一个槽。
	for i := range t.slots {
		s := &t.slots[i]
		if s.state != slotFree || !s.used {
			continue
		}
		nextGen := (s.gen + 1) & t.genMask
		if nextGen <= s.gen { // 递增回绕：0 是本槽最初发出的代数，必然重复
			s.state = slotSealed
			s.obj = nil
			continue
		}
		s.gen = nextGen
		s.obj = obj
		s.kind = kind
		s.borrows = 0
		s.state = slotLive
		return t.Encode(i, s.gen, kind), nil
	}

	// 没有任何可安装的槽：只要还有未封存的槽，就只是暂时性短缺。
	for i := range t.slots {
		if t.slots[i].state != slotSealed {
			return 0, ErrNoSlot
		}
	}
	return 0, ErrExhausted
}

// Borrow 按期望类别短时借用对象。返回对象与归还函数 release；
// release 幂等，可重复调用。
//
// 表关闭后新借用立即被拒绝（ErrClosed）；已经开始的借用不受影响，
// 在最后一个借用归还前，该槽不会被复用。
func (t *Table) Borrow(h Handle, wantKind uint8) (any, func(), error) {
	t.mu.Lock()
	defer t.mu.Unlock()

	if t.closed {
		return nil, nil, ErrClosed
	}

	s, err := t.resolve(h, wantKind)
	if err != nil {
		return nil, nil, err
	}

	s.borrows++
	released := false
	release := func() {
		t.mu.Lock()
		defer t.mu.Unlock()
		if released {
			return
		}
		released = true
		s.borrows--
		if s.borrows == 0 && s.state == slotDraining {
			// 最后一个借用归还：回收槽，允许后续注册复用（或封存）。
			s.state = slotFree
			s.obj = nil
		}
	}
	return s.obj, release, nil
}

// Return 显式归还一个借用（与 Borrow 返回的 release 等价，供不使用
// 闭包的调用方使用）。对未处于借用状态的槽返回 ErrNotBorrowed。
func (t *Table) Return(h Handle) error {
	t.mu.Lock()
	defer t.mu.Unlock()

	idx, gen, _ := t.Decode(h)
	if idx < 0 || idx >= len(t.slots) {
		return ErrStale
	}
	s := &t.slots[idx]
	if s.state == slotFree || s.state == slotSealed || s.gen != gen {
		return ErrStale
	}
	if s.borrows <= 0 {
		return ErrNotBorrowed
	}
	s.borrows--
	if s.borrows == 0 && s.state == slotDraining {
		s.state = slotFree
		s.obj = nil
	}
	return nil
}

// Close 关闭句柄对应的对象。关闭后立即拒绝新的借用（ErrClosed），
// 已经开始的借用可以继续完成；最后一个借用归还之前，该槽禁止复用。
// 对过期句柄返回 ErrStale；重复关闭已关闭的对象返回 ErrClosed。
func (t *Table) Close(h Handle) error {
	t.mu.Lock()
	defer t.mu.Unlock()

	idx, gen, _ := t.Decode(h)
	if idx < 0 || idx >= len(t.slots) {
		return ErrStale
	}
	s := &t.slots[idx]
	if s.state == slotFree || s.state == slotSealed || s.gen != gen {
		return ErrStale
	}
	if s.state == slotDraining {
		return ErrClosed
	}
	if s.borrows == 0 {
		// 无在途借用：立即回收为可复用空闲槽。
		s.state = slotFree
		s.obj = nil
		return nil
	}
	// 仍有借用：进入排空状态，拒绝新借用，等待最后一次归还。
	s.state = slotDraining
	return nil
}

// CloseTable 关闭整张表：之后所有新的注册与借用都被拒绝，
// 已在途的借用仍可正常归还。
func (t *Table) CloseTable() {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.closed = true
}
