// Package mantrap 实现内外两扇门共用一个中间区的门禁通行模块。
//
// 模型：
//
//	外区 Outer —— 外门 OuterDoor —— 中间区 Middle —— 内门 InnerDoor —— 内区 Inner
//
// 中间区容量默认为 1（构造时可调整）。每张凭证（卡）登记在固定区域，
// 只能从当前所在区域发起有方向的过门请求；凭证位置仅在“有方向的过门
// 回执”到达并通过校验后才改变。
package mantrap

import (
	"errors"
	"sync"
)

// Zone 标识门禁系统中的一个区域。
type Zone string

const (
	Outer  Zone = "outer"
	Middle Zone = "middle"
	Inner  Zone = "inner"
)

// Door 标识一扇物理门。
type Door string

const (
	OuterDoor Door = "outer_door"
	InnerDoor Door = "inner_door"
)

// Direction 表示过门方向：从源区进入目标区。
type Direction string

const (
	// Inward 向内：外区->中间区，或中间区->内区。
	Inward Direction = "inward"
	// Outward 向外：中间区->外区，或内区->中间区。
	Outward Direction = "outward"
)

// doorState 是门的内部状态。
type doorState string

const (
	doorLocked   doorState = "locked"   // 关门确认后互锁
	doorReleased doorState = "released" // 已放行、等待门被打开
	doorOpen     doorState = "open"     // 门已打开，等待通过传感器/关门确认
)

// RoundState 是一轮过门的生命周期状态。
type RoundState string

const (
	StateReleased      RoundState = "released"  // 已放行，门等待打开
	StatePendingVerify RoundState = "pending"   // 门已开，凭证与名额待核对
	StateCompleted     RoundState = "completed" // 回执确认，通行完成
	StateTimedOut      RoundState = "timed_out" // 门超时未开，名额已归还
	StateCancelled     RoundState = "cancelled" // 管理员判定有疑义并撤销
)

// 各类拒绝原因。调用方可用 errors.Is 判定。
var (
	ErrUnknownCard     = errors.New("mantrap: unknown credential")
	ErrUnknownZone     = errors.New("mantrap: unknown zone")
	ErrUnknownDoor     = errors.New("mantrap: unknown door")
	ErrUnknownRound    = errors.New("mantrap: unknown crossing round")
	ErrWrongZone       = errors.New("mantrap: credential may only request from its registered/current zone")
	ErrBadDirection    = errors.New("mantrap: direction is not valid for the credential's zone")
	ErrCardBusy        = errors.New("mantrap: credential already has an unresolved crossing round")
	ErrDoorInterlocked = errors.New("mantrap: the other door is in use, doors are interlocked")
	ErrZoneFull        = errors.New("mantrap: target zone has no free slot (capacity reserved or occupied)")
	ErrRoundState      = errors.New("mantrap: round is not in the required state for this event")
	ErrStaleReceipt    = errors.New("mantrap: receipt belongs to an old or closed round and cannot start a new crossing")
	ErrReceiptMismatch = errors.New("mantrap: receipt does not match this round (card/door/direction)")
)

// Receipt 是门侧传感器产生的“有方向的过门回执”。
// 凭证位置只有在该回执被接受后才会改变。
type Receipt struct {
	Card      string
	Door      Door
	Direction Direction
}

// round 记录一轮过门的完整上下文。
type round struct {
	ID        uint64
	Card      string
	Door      Door
	Direction Direction
	From      Zone
	To        Zone
	State     RoundState
	DoorState doorState
	// reservation 为本轮在目标区暂占的名额；超时/撤销时归还，
	// 完成时转为凭证在目标区的实际占用。
	reservation bool
	// completedReceipt 记录首次被接受的回执，用于相同回执重送的幂等处理。
	completedReceipt *Receipt
}

// Snapshot 是一轮过门对外可见的只读视图。
type Snapshot struct {
	ID        uint64
	Card      string
	Door      Door
	Direction Direction
	From      Zone
	To        Zone
	State     RoundState
}

func (r *round) snapshot() Snapshot {
	return Snapshot{
		ID:        r.ID,
		Card:      r.Card,
		Door:      r.Door,
		Direction: r.Direction,
		From:      r.From,
		To:        r.To,
		State:     r.State,
	}
}

// Controller 是门禁通行判定模块。所有方法并发安全。
type Controller struct {
	mu sync.Mutex

	capacity map[Zone]int // 区域容量；不存在或 <=0 表示不限容量
	cardZone map[string]Zone
	rounds   map[uint64]*round
	// 每扇门当前关联的一轮（非 locked 时非 nil），关门确认后清空。
	activeRound map[Door]*round
	// 每张凭证当前未完结的一轮（released/pending），保证同一卡不并发过门。
	cardRound map[string]*round
	doorState map[Door]doorState

	nextID uint64
}

// New 创建控制器。中间区容量为 1；外区与内区不限容量。
func New() *Controller {
	return NewWithCapacity(map[Zone]int{Middle: 1})
}

// NewWithCapacity 使用自定义区域容量创建控制器。
// cap 中未列出的区域视为不限容量；容量值 <= 0 同样表示不限容量。
func NewWithCapacity(cap map[Zone]int) *Controller {
	caps := make(map[Zone]int, len(cap))
	for z, c := range cap {
		caps[z] = c
	}
	return &Controller{
		capacity:    caps,
		cardZone:    map[string]Zone{},
		rounds:      map[uint64]*round{},
		activeRound: map[Door]*round{},
		cardRound:   map[string]*round{},
		doorState: map[Door]doorState{
			OuterDoor: doorLocked,
			InnerDoor: doorLocked,
		},
	}
}

// RegisterCard 将凭证登记在指定区域。凭证只能从登记所在区发起通行。
func (c *Controller) RegisterCard(card string, zone Zone) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.knownZone(zone) {
		return ErrUnknownZone
	}
	c.cardZone[card] = zone
	return nil
}

// CardZone 返回凭证当前所在区域。未登记凭证返回 ErrUnknownCard。
func (c *Controller) CardZone(card string) (Zone, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	z, ok := c.cardZone[card]
	if !ok {
		return "", ErrUnknownCard
	}
	return z, nil
}

// crossingEndpoints 依据凭证所在区域与方向解析门与源/目标区。
func crossingEndpoints(from Zone, dir Direction) (Door, Zone, bool) {
	switch {
	case from == Outer && dir == Inward:
		return OuterDoor, Middle, true
	case from == Middle && dir == Outward:
		return OuterDoor, Outer, true
	case from == Middle && dir == Inward:
		return InnerDoor, Inner, true
	case from == Inner && dir == Outward:
		return InnerDoor, Middle, true
	default:
		return "", "", false
	}
}

func (c *Controller) knownZone(z Zone) bool {
	return z == Outer || z == Middle || z == Inner
}

// zoneUsed 统计区域当前占用：实际在区凭证数 + 各轮暂占名额。
func (c *Controller) zoneUsed(z Zone) int {
	used := 0
	for _, pos := range c.cardZone {
		if pos == z {
			used++
		}
	}
	for _, r := range c.rounds {
		if r.reservation && r.To == z {
			used++
		}
	}
	return used
}

// ZoneUsed 返回区域当前占用数（含暂占名额）。
func (c *Controller) ZoneUsed(z Zone) (int, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.knownZone(z) {
		return 0, ErrUnknownZone
	}
	return c.zoneUsed(z), nil
}

// ZoneCapacity 返回区域容量；第二返回值为 false 表示该区域不限容量。
func (c *Controller) ZoneCapacity(z Zone) (int, bool, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.knownZone(z) {
		return 0, false, ErrUnknownZone
	}
	cap, limited := c.capacity[z]
	if !limited || cap <= 0 {
		return 0, false, nil
	}
	return cap, true, nil
}

// Request 由凭证从其登记/当前所在区发起一轮有方向的过门请求。
// 成功后门被放行（released），并在目标区暂占一个名额。
func (c *Controller) Request(card string, dir Direction) (Snapshot, error) {
	c.mu.Lock()
	defer c.mu.Unlock()

	from, ok := c.cardZone[card]
	if !ok {
		return Snapshot{}, ErrUnknownCard
	}
	door, to, ok := crossingEndpoints(from, dir)
	if !ok {
		return Snapshot{}, ErrBadDirection
	}
	// 凭证只可从登记所在区（即当前所在区）发起：cardZone 同时承担
	// “登记位置”和“当前位置”，位置只随有效回执/管理员纠正改变，
	// 因此此处解析出的 from 就是凭证被允许发起通行的唯一区域。
	// 同一凭证上一轮未核对完成前，不得发起新一轮。
	if _, busy := c.cardRound[card]; busy {
		return Snapshot{}, ErrCardBusy
	}
	// 目标区名额：容量为 1 时第二张卡的同时请求在此被拒绝。
	// 容量约束优先于互锁判定，使拒绝原因确定可解释。
	if cap, limited := c.capacity[to]; limited && cap > 0 && c.zoneUsed(to) >= cap {
		return Snapshot{}, ErrZoneFull
	}
	// 互锁：一扇门从放行至关门确认（且待核对轮次被回执或管理员
	// 处理完毕）期间，另一扇门保持互锁。注意关门确认后物理门虽已
	// 锁定，但 activeRound 仍在（凭证与名额待核对），互锁不得提前解除。
	if len(c.activeRound) != 0 {
		return Snapshot{}, ErrDoorInterlocked
	}

	c.nextID++
	r := &round{
		ID:          c.nextID,
		Card:        card,
		Door:        door,
		Direction:   dir,
		From:        from,
		To:          to,
		State:       StateReleased,
		DoorState:   doorReleased,
		reservation: true,
	}
	c.rounds[r.ID] = r
	c.activeRound[door] = r
	c.cardRound[card] = r
	c.doorState[door] = doorReleased
	return r.snapshot(), nil
}

// NotifyDoorOpened 处理“门已打开”的硬件事件。
// 门打开后进入待核对：凭证仍在源区，目标区名额继续被暂占。
func (c *Controller) NotifyDoorOpened(door Door) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	r, ok := c.activeRound[door]
	if !ok {
		return ErrUnknownDoor
	}
	if r.DoorState != doorReleased {
		return ErrRoundState
	}
	r.DoorState = doorOpen
	c.doorState[door] = doorOpen
	r.State = StatePendingVerify
	return nil
}

// NotifyDoorTimeout 处理“放行后超时未开门”。
// 仅在 released 阶段有效：归还目标区暂占名额并解除互锁。
func (c *Controller) NotifyDoorTimeout(door Door) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	r, ok := c.activeRound[door]
	if !ok {
		return ErrUnknownDoor
	}
	if r.DoorState != doorReleased {
		return ErrRoundState
	}
	r.reservation = false
	r.DoorState = doorLocked
	r.State = StateTimedOut
	delete(c.activeRound, door)
	delete(c.cardRound, r.Card)
	c.doorState[door] = doorLocked
	return nil
}

// NotifyDoorClosed 处理“关门确认”。
// 若此前未收到过门回执，本轮保持待核对：凭证与名额都不释放，
// 等待迟来的回执或管理员纠正。
func (c *Controller) NotifyDoorClosed(door Door) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	r, ok := c.activeRound[door]
	if !ok {
		return ErrUnknownDoor
	}
	if r.DoorState != doorOpen {
		return ErrRoundState
	}
	// 关门本身不解除互锁：传感器未确认是否通过，互锁保持，
	// 直到回执完成通行或管理员纠正。
	r.DoorState = doorLocked
	c.doorState[door] = doorLocked
	// 注意：不删除 activeRound / cardRound，round 仍处于 PendingVerify。
	return nil
}

// SubmitReceipt 提交有方向的过门回执。凭证位置仅在回执被接受时改变。
// 相同回执重送是幂等的；上一轮（或已关闭轮次）的回执不能替新一轮过门。
func (c *Controller) SubmitReceipt(roundID uint64, rcpt Receipt) (Snapshot, error) {
	c.mu.Lock()
	defer c.mu.Unlock()

	r, ok := c.rounds[roundID]
	if !ok {
		return Snapshot{}, ErrUnknownRound
	}
	// 已完结的轮次（完成/超时/撤销）不再接受任何“新”回执。
	if r.State == StateCompleted {
		// 相同回执重送：幂等返回当前快照，不重复移动凭证或名额。
		if r.completedReceipt != nil && *r.completedReceipt == rcpt {
			return r.snapshot(), nil
		}
		return Snapshot{}, ErrStaleReceipt
	}
	if r.State == StateTimedOut || r.State == StateCancelled {
		// 上一轮的回执不能替新一轮过门。
		return Snapshot{}, ErrStaleReceipt
	}
	// released / pending 阶段校验回执三要素必须与本轮一致，
	// 防止用别的门/方向/卡的回执冒名完成。
	if rcpt.Card != r.Card || rcpt.Door != r.Door || rcpt.Direction != r.Direction {
		return Snapshot{}, ErrReceiptMismatch
	}

	c.completeRound(r, &rcpt)
	return r.snapshot(), nil
}

// completeRound 在锁内完成一轮通行：暂占名额转为实际占用，
// 凭证移动到目标区，解除互锁。
func (c *Controller) completeRound(r *round, rcpt *Receipt) {
	// reservation 仍占用目标区名额；移动凭证后凭证本身占用该名额，
	// 因此先撤掉暂占标记，再更新位置，zoneUsed 总量保持一致。
	r.reservation = false
	c.cardZone[r.Card] = r.To
	r.State = StateCompleted
	r.completedReceipt = rcpt
	r.DoorState = doorLocked
	c.doorState[r.Door] = doorLocked
	delete(c.activeRound, r.Door)
	delete(c.cardRound, r.Card)
}

// AdminResolve 由管理员对一轮有疑义的待核对通行作出纠正。
//   - confirm=true：确认通行，等价于接受过门回执；
//   - confirm=false：撤销本轮，凭证留在源区，归还目标区暂占名额，
//     位置与空余名额一起恢复一致。
//
// 仅待核对（pending，含关门后未确认）轮次可被纠正。
func (c *Controller) AdminResolve(roundID uint64, confirm bool) (Snapshot, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	r, ok := c.rounds[roundID]
	if !ok {
		return Snapshot{}, ErrUnknownRound
	}
	if r.State != StatePendingVerify {
		return Snapshot{}, ErrRoundState
	}
	if confirm {
		c.completeRound(r, nil)
	} else {
		r.reservation = false
		r.State = StateCancelled
		r.DoorState = doorLocked
		c.doorState[r.Door] = doorLocked
		delete(c.activeRound, r.Door)
		delete(c.cardRound, r.Card)
	}
	return r.snapshot(), nil
}

// RoundSnapshot 查询一轮过门的当前状态；未知轮次返回 ErrUnknownRound。
func (c *Controller) RoundSnapshot(roundID uint64) (Snapshot, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	r, ok := c.rounds[roundID]
	if !ok {
		return Snapshot{}, ErrUnknownRound
	}
	return r.snapshot(), nil
}

// DoorLocked 报告门当前是否处于互锁关闭态。
func (c *Controller) DoorLocked(door Door) (bool, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	s, ok := c.doorState[door]
	if !ok {
		return false, ErrUnknownDoor
	}
	return s == doorLocked, nil
}
