// Package permit 实现巡检终端在（可能断线的）本地环境下的人员登录判定。
//
// 设计前提：
//   - 输入的快照与撤销记录均已在上游完成验签，本包不做任何密码学验证，也不联网。
//   - 终端平时从上游接收带编号（Seq）和有效期的人员许可包（Snapshot）。
//   - 断线时终端按最后一份可信快照继续工作，直到许可到达期限。
//
// 关键安全语义：
//   - 时钟可注入；引擎维护单调水位 watermark=max(所有观测时钟)。时钟回拨不会
//     降低水位，因此既不能复活已经过期的许可，也不能把窗口 NotBefore 之前的
//     许可“倒带”成有效，即回拨不得延长任何已知有效期。
//   - 撤销记录按“人员+工区”（工区为空表示人员级撤销）累积，同一键只保留最大
//     编号；编号更旧的撤销记录被忽略。
//   - 收到编号更旧或相同的快照返回 ErrStaleSnapshot，且不改变任何已知状态，
//     尤其是不会改变已累积的撤销结果。
//   - 被撤销的许可只有在更新的快照中被“明确重新授权”时才可恢复：携带新授权的
//     快照编号必须严格大于对应撤销记录的编号（同编号保守地按撤销优先）。
package permit

import (
	"errors"
	"time"
)

// 人员与工区标识。空人员标识视为非法输入；空工区在撤销记录中表示人员级撤销。
type PersonID string
type AreaID string

// Grant 是一条针对“人员+工区”的明确授权及其有效窗口。
type Grant struct {
	Person    PersonID
	Area      AreaID
	NotBefore time.Time // 窗口起点（含）
	ExpiresAt time.Time // 窗口终点（不含）
}

// Snapshot 是一份已验签的、带编号的许可快照。
type Snapshot struct {
	Seq    int64 // 单调递增的快照编号
	At     time.Time
	Grants []Grant
}

// Revocation 是一条已验签的撤销记录。Area 为空字符串时表示人员级撤销
// （撤销该人员在所有工区的许可）。
type Revocation struct {
	Seq    int64 // 撤销记录编号，与快照编号处于同一单调序列
	At     time.Time
	Person PersonID
	Area   AreaID // 空表示人员级撤销
}

// Clock 是可注入的设备时钟抽象。
type Clock interface {
	Now() time.Time
}

// 输入非法或过期输入时返回的哨兵错误。
var (
	// ErrStaleSnapshot 表示快照编号不大于已知最新快照；该快照会被拒绝且
	// 不改变引擎任何状态。
	ErrStaleSnapshot = errors.New("permit: snapshot is not newer than the latest known snapshot")
	// ErrInvalidSnapshot 表示快照本身字段非法。
	ErrInvalidSnapshot = errors.New("permit: invalid snapshot")
	// ErrInvalidRevocation 表示撤销记录字段非法。
	ErrInvalidRevocation = errors.New("permit: invalid revocation")
)

// 拒绝原因，供调用方审计与测试断言使用。
const (
	ReasonClockRollback = "clock_rollback" // 观测时钟早于内部水位（登录仍按水位判定）
	ReasonNoSnapshot    = "no_snapshot"    // 从未收到任何可信快照
	ReasonNoGrant       = "no_grant"       // 最新快照未对该人员/工区明确授权
	ReasonNotYetValid   = "not_yet_valid"  // 授权窗口尚未开始（按单调水位判定）
	ReasonExpired       = "expired"        // 授权已过期（按单调水位判定）
	ReasonRevoked       = "revoked"        // 存在编号不小于快照编号的撤销记录
	ReasonEmptyPerson   = "empty_person"   // 请求的人员标识为空
)

// Decision 是一次登录判定的结果。
type Decision struct {
	Allowed bool
	Reason  string // 拒绝时为上面的 Reason* 常量；允许时为空
}

type revKey struct {
	person PersonID
	area   AreaID
}

// Engine 是本地判定引擎。零值不可用，请使用 NewEngine 构造。
type Engine struct {
	clock     Clock
	watermark time.Time // 单调时钟水位：所有观测时钟的最大值

	latestSeq int64     // 已知最新快照编号；0 表示尚无快照
	latest    *Snapshot // 最新快照（内部副本，防止外部修改）

	// 每“人员+工区”键的最大撤销编号；空工区键为人员级撤销。
	revocations map[revKey]int64
	revLatest   int64 // 已接受撤销记录的最大编号
}

// NewEngine 创建本地判定引擎。clock 为 nil 时使用设备实时时钟。
func NewEngine(clock Clock) *Engine {
	if clock == nil {
		clock = wallClock{}
	}
	return &Engine{
		clock:       clock,
		revocations: make(map[revKey]int64),
	}
}

type wallClock struct{}

func (wallClock) Now() time.Time { return time.Now() }

// ObserveClock 记录一次设备时钟读数并返回引擎当前的单调水位。
// 读数早于水位（时钟回拨）时水位保持不变。
func (e *Engine) ObserveClock(now time.Time) time.Time {
	if now.After(e.watermark) {
		e.watermark = now
	}
	return e.watermark
}

// Watermark 返回当前单调时钟水位。
func (e *Engine) Watermark() time.Time { return e.watermark }

// LatestSnapshotSeq 返回已知最新快照编号；尚无快照时返回 0。
func (e *Engine) LatestSnapshotSeq() int64 { return e.latestSeq }

// RevocationSeq 返回某“人员+工区”键当前生效的最大撤销编号，不存在时返回 0。
// 传空 area 读取人员级撤销记录。
func (e *Engine) RevocationSeq(person PersonID, area AreaID) int64 {
	return e.revocations[revKey{person: person, area: area}]
}

// IngestSnapshot 接收一份已验签快照。
//
// 编号不大于已知最新编号时返回 ErrStaleSnapshot，且不改变任何状态（包括已
// 累积的撤销结果）。字段非法时返回 ErrInvalidSnapshot。引擎内部保存快照副本，
// 调用方之后修改入参不会影响判定。
func (e *Engine) IngestSnapshot(s Snapshot) error {
	if s.Seq <= 0 {
		return ErrInvalidSnapshot
	}
	for _, g := range s.Grants {
		if g.Person == "" {
			return ErrInvalidSnapshot
		}
		if !g.ExpiresAt.After(g.NotBefore) {
			return ErrInvalidSnapshot
		}
	}
	if s.Seq <= e.latestSeq {
		return ErrStaleSnapshot
	}

	cp := Snapshot{
		Seq:    s.Seq,
		At:     s.At,
		Grants: append([]Grant(nil), s.Grants...),
	}
	e.latest = &cp
	e.latestSeq = s.Seq
	return nil
}

// IngestRevocation 接收一条已验签撤销记录。
//
// 编号不大于该键已知撤销编号的旧记录会被忽略（返回 nil，状态不变）；字段非法
// 时返回 ErrInvalidRevocation。人员级撤销（Area 为空）与工区级撤销分键存储。
func (e *Engine) IngestRevocation(r Revocation) error {
	if r.Seq <= 0 || r.Person == "" {
		return ErrInvalidRevocation
	}
	key := revKey{person: r.Person, area: r.Area}
	if r.Seq <= e.revocations[key] {
		return nil // 旧记录：忽略，不改变已知撤销结果
	}
	e.revocations[key] = r.Seq
	if r.Seq > e.revLatest {
		e.revLatest = r.Seq
	}
	return nil
}

// Login 判定请求人员此刻是否允许登录指定工区。
//
// 判定顺序：
//  1. 读取注入时钟并推进单调水位（回拨不降低水位）。
//  2. 必须存在已接收的可信快照。
//  3. 最新快照中必须存在针对该人员+工区的明确授权，且按水位判定处于有效窗口内。
//  4. 不存在编号 >= 携带该授权快照编号的工区级或人员级撤销记录。
func (e *Engine) Login(person PersonID, area AreaID) Decision {
	if person == "" {
		return Decision{Allowed: false, Reason: ReasonEmptyPerson}
	}

	now := e.clock.Now()
	e.ObserveClock(now)
	effectiveNow := e.watermark // 回拨后仍按水位判定，有效期不得被延长

	if e.latest == nil {
		return Decision{Allowed: false, Reason: ReasonNoSnapshot}
	}

	grant, found := e.findGrant(person, area)
	if !found {
		return Decision{Allowed: false, Reason: ReasonNoGrant}
	}
	if effectiveNow.Before(grant.NotBefore) {
		return Decision{Allowed: false, Reason: ReasonNotYetValid}
	}
	if !effectiveNow.Before(grant.ExpiresAt) {
		return Decision{Allowed: false, Reason: ReasonExpired}
	}

	if e.isRevoked(person, area, e.latest.Seq) {
		return Decision{Allowed: false, Reason: ReasonRevoked}
	}

	return Decision{Allowed: true}
}

// findGrant 在最新快照中查找人员+工区的明确授权；存在多条时取最早过期的一条。
func (e *Engine) findGrant(person PersonID, area AreaID) (Grant, bool) {
	var best Grant
	found := false
	for _, g := range e.latest.Grants {
		if g.Person != person || g.Area != area {
			continue
		}
		if !found || g.ExpiresAt.Before(best.ExpiresAt) {
			best, found = g, true
		}
	}
	return best, found
}

// isRevoked 判断携带于编号 snapshotSeq 的快照中的授权是否仍被撤销。
// 工区级与人员级（空工区）撤销任一编号 >= snapshotSeq 即视为被撤销；
// 同编号保守地按撤销优先，因此要求重新授权的快照编号严格更大。
func (e *Engine) isRevoked(person PersonID, area AreaID, snapshotSeq int64) bool {
	if seq := e.revocations[revKey{person: person, area: area}]; seq >= snapshotSeq {
		return true
	}
	if seq := e.revocations[revKey{person: person, area: ""}]; seq >= snapshotSeq {
		return true
	}
	return false
}
