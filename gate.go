// Package sendgate 实现专网数据报服务的内存发送门禁。
//
// 门禁按“路径”（连接 + 当前对端返回地址）记账：
//   - 未验证路径最多可发送该路径上“唯一已收数据报字节数”的三倍；
//   - 路径在收到验证回执后解除额度限制（UnlimitedRemaining）；
//   - 同一数据报编号的多次解析事件只入账一次；
//   - 多个响应拼成一包时，连同固定包头整体预留额度；额度不足时整包拒绝，
//     一个字节都不预留、不提交；
//   - 只有发送明确失败时才归还预留额度；成功确认只核销在途记录；
//   - 连接更换对端地址后新路径从零开始；旧路径的验证回执与迟到的发送
//     确认仍归属原路径，不影响新路径。
package sendgate

import (
	"errors"
)

// UnlimitedRemaining 是已验证路径在 PathReport.Remaining 中返回的哨兵值，
// 表示剩余额度不受限制。
const UnlimitedRemaining int64 = -1

// ErrUnknownPath 表示事件引用了门禁中不存在的路径（该连接从未在该地址上
// 收过数据报，或相关状态已不可用）。此类事件被静默丢弃，不改动任何额度。
var ErrUnknownPath = errors.New("sendgate: unknown path")

// ErrPacketInflight 表示包号在该连接上仍有一笔在途（已获准但未出最终
// 发送结果）的预留。调用方必须先结束在途包，才能复用同一包号。
var ErrPacketInflight = errors.New("sendgate: packet number already inflight")

// ErrPacketUnknown 表示发送结果引用的包号不属于给定路径（从未获准、已
// 核销，或属于其他路径）。该结果被丢弃。
var ErrPacketUnknown = errors.New("sendgate: unknown packet for path")

// ErrEmptyPacket 表示申请发送的包没有任何响应数据，且调用方要求零长包
// 也必须显式拒绝。默认策略下零长包只占用包头字节。
var ErrEmptyPacket = errors.New("sendgate: empty packet")

// pathKey 唯一标识一条返回路径：连接标识 + 对端返回地址。
type pathKey struct {
	conn string
	addr string
}

// inflightPacket 记录一笔已获准发送、尚在传输中的预留。
type inflightPacket struct {
	number int64
	path   pathKey
	size   int64
}

// pathState 是单条路径的内存账本。
type pathState struct {
	key pathKey

	// uniqueReceived 是该路径上已入账的唯一数据报字节总数。
	uniqueReceived int64
	// seen 是已入账数据报编号集合，用于解析事件去重。
	seen map[int64]struct{}

	// charged 是当前已占用额度：已获准且尚未因明确失败归还的全部包字节。
	charged int64
	// verified 为 true 时路径已通过验证，额度不再受限。
	verified bool

	// approved 按获准顺序记录本路径所有获准发送的包号。
	approved []int64
}

// limit 返回路径当前的发送额度上限；已验证路径不受限。
func (p *pathState) limit() int64 {
	return p.uniqueReceived * 3
}

// remaining 返回剩余额度；已验证路径返回 UnlimitedRemaining。
func (p *pathState) remaining() int64 {
	if p.verified {
		return UnlimitedRemaining
	}
	return p.limit() - p.charged
}

// Gate 是内存发送门禁。零值不可用，请使用 New 构造。
type Gate struct {
	headerSize int64

	// paths 保存所有路径（包括连接换地址后保留的旧路径），
	// 以便迟到事件仍能归还原路径。
	paths map[pathKey]*pathState

	// inflight 以 (连接, 包号) 为键记录在途预留。包号在连接维度唯一，
	// 因此迟到确认可直接定位其原路径。
	inflight map[struct {
		conn   string
		number int64
	}]inflightPacket
}

// New 创建门禁。headerSize 是每个响应包固定包头的字节数，拼包预留时
// 整体计入（包头只计一次）。
func New(headerSize int64) *Gate {
	if headerSize < 0 {
		headerSize = 0
	}
	return &Gate{
		headerSize: headerSize,
		paths:      make(map[pathKey]*pathState),
		inflight: make(map[struct {
			conn   string
			number int64
		}]inflightPacket),
	}
}

// pathOrNil 取路径；不存在时返回 nil（不隐式创建，避免无关事件污染账本）。
func (g *Gate) pathOrNil(conn, addr string) *pathState {
	return g.paths[pathKey{conn: conn, addr: addr}]
}

// OnReceive 处理一个入站数据报解析事件。
//
// datagramNumber 为数据报编号，size 为该数据报的字节数。同一编号在同一
// 路径上的重复解析事件只入账一次：首次返回 true，重复事件返回 false 且
// 不改动任何计数。连接换地址后新路径独立记账，同一编号在新路径上仍可
// 入账一次。
func (g *Gate) OnReceive(conn, addr string, datagramNumber, size int64) (counted bool) {
	if size < 0 {
		size = 0
	}
	key := pathKey{conn: conn, addr: addr}
	p, ok := g.paths[key]
	if !ok {
		// 首个解析事件创建路径，账本从零开始。
		p = &pathState{key: key, seen: make(map[int64]struct{})}
		g.paths[key] = p
	}
	if _, dup := p.seen[datagramNumber]; dup {
		return false
	}
	p.seen[datagramNumber] = struct{}{}
	p.uniqueReceived += size
	return true
}

// OnVerified 处理路径验证回执。验证只作用于事件指明的 (连接, 地址)；
// 连接已换地址后，旧路径的验证回执不会作用于新路径。
//
// 路径未知时返回 ErrUnknownPath，账本不变。
func (g *Gate) OnVerified(conn, addr string) error {
	p := g.pathOrNil(conn, addr)
	if p == nil {
		return ErrUnknownPath
	}
	p.verified = true
	return nil
}

// TrySend 申请在指定路径上发送一个响应包。
//
// number 为包号（连接维度唯一），responseSizes 为拼入同一包的各响应
// 字节数。整包大小 = 固定包头 + 各响应字节之和；额度检查与预留一次性
// 原子完成：未验证路径若剩余额度不足以容纳整包，则整包拒绝，一个字节
// 也不预留。获准后包进入在途状态，直到 OnSendResult 给出最终结果。
//
// 已验证路径不受额度限制，但仍登记在途与获准记录。
func (g *Gate) TrySend(conn, addr string, number int64, responseSizes []int64) (bool, error) {
	key := pathKey{conn: conn, addr: addr}
	p, ok := g.paths[key]
	if !ok {
		// 没有任何入站数据报的路径额度为零：显式建账并拒绝，
		// 同时让后续迟到事件有明确的归属状态。
		p = &pathState{key: key, seen: make(map[int64]struct{})}
		g.paths[key] = p
	}

	inflightKey := struct {
		conn   string
		number int64
	}{conn: conn, number: number}
	if _, exists := g.inflight[inflightKey]; exists {
		return false, ErrPacketInflight
	}

	total := g.headerSize
	for _, n := range responseSizes {
		if n > 0 {
			total += n
		}
	}

	if !p.verified && total > p.remaining() {
		// 整包拒绝：不登记在途、不占用额度、不记录获准包号。
		return false, nil
	}

	p.charged += total
	p.approved = append(p.approved, number)
	g.inflight[inflightKey] = inflightPacket{number: number, path: key, size: total}
	return true, nil
}

// OnSendResult 处理一个发送最终结果。
//
// 结果严格按包号登记的原路径归属：即使连接已经换到新地址，迟到的确认
// 也只影响旧路径。success 为 true 表示发送明确成功（仅核销在途记录，
// 预留额度不归还，因为字节确实已经发出）；success 为 false 表示明确
// 失败，核销在途记录并把整包预留字节归还原路径。
//
// 包号未知（从未获准或已核销）时返回 ErrPacketUnknown，账本不变。
func (g *Gate) OnSendResult(conn string, number int64, success bool) error {
	inflightKey := struct {
		conn   string
		number int64
	}{conn: conn, number: number}
	pkt, ok := g.inflight[inflightKey]
	if !ok {
		return ErrPacketUnknown
	}
	delete(g.inflight, inflightKey)

	p := g.paths[pkt.path]
	if p == nil {
		// 理论上不可达：在途包必然持有有效路径。保守处理为未知包。
		return ErrPacketUnknown
	}
	if !success {
		p.charged -= pkt.size
		if p.charged < 0 {
			p.charged = 0
		}
	}
	return nil
}

// PathReport 是一条路径的对外快照。
type PathReport struct {
	Conn string
	Addr string

	// UniqueReceived 是已入账的唯一数据报字节数。
	UniqueReceived int64
	// Charged 是当前已占用（在途预留）字节数。
	Charged int64
	// Remaining 是剩余可发额度；已验证路径为 UnlimitedRemaining。
	Remaining int64
	// Verified 表示路径是否已通过验证。
	Verified bool
	// ApprovedPackets 是本路径实际获准发送的包号，按获准顺序排列。
	ApprovedPackets []int64
}

// Report 返回所有已知路径的快照（含连接换地址后保留的旧路径）。
// 返回顺序不保证稳定，调用方应按 Conn/Addr 自行定位。
func (g *Gate) Report() []PathReport {
	out := make([]PathReport, 0, len(g.paths))
	for _, p := range g.paths {
		approved := make([]int64, len(p.approved))
		copy(approved, p.approved)
		out = append(out, PathReport{
			Conn:            p.key.conn,
			Addr:            p.key.addr,
			UniqueReceived:  p.uniqueReceived,
			Charged:         p.charged,
			Remaining:       p.remaining(),
			Verified:        p.verified,
			ApprovedPackets: approved,
		})
	}
	return out
}

// ReportPath 返回单条路径快照；路径未知时返回 ok=false。
func (g *Gate) ReportPath(conn, addr string) (PathReport, bool) {
	p := g.pathOrNil(conn, addr)
	if p == nil {
		return PathReport{}, false
	}
	approved := make([]int64, len(p.approved))
	copy(approved, p.approved)
	return PathReport{
		Conn:            p.key.conn,
		Addr:            p.key.addr,
		UniqueReceived:  p.uniqueReceived,
		Charged:         p.charged,
		Remaining:       p.remaining(),
		Verified:        p.verified,
		ApprovedPackets: approved,
	}, true
}
