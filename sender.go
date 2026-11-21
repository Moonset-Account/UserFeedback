// Package h2sender 实现 HTTP/2 发送端的流控调度组件。
//
// 组件只负责本地、确定性的发送决策：多个流排队等待发送 DATA 帧时，按流编号
// 升序轮询活动流，每轮每个流最多产出一个非空 DATA 帧，帧大小同时受
//   - 该流待发字节数
//   - 对端公布的最大帧长
//   - 连接级流控窗口余额
//   - 流级流控窗口余额（允许为负）
//
// 限制。已产出帧对应的字节会从待发队列与窗口余额中扣减，组件不会重发任何
// 已发送字节。
package h2sender

import (
	"errors"
	"sort"
)

// HTTP/2 (RFC 7540) 相关默认值与取值边界。
const (
	// DefaultInitialWindowSize 是 RFC 7540 §6.9.2 规定的初始窗口大小。
	DefaultInitialWindowSize = 65535
	// DefaultMaxFrameSize 是 RFC 7540 §4.2 规定的最大帧长初始值。
	DefaultMaxFrameSize = 16384
	// MaxWindowSize 是窗口大小的上限 (2^31-1)。
	MaxWindowSize = 1<<31 - 1
	// MinMaxFrameSize / MaxMaxFrameSize 是 SETTINGS_MAX_FRAME_SIZE 的合法区间。
	MinMaxFrameSize = 16384
	MaxMaxFrameSize = 16777215
)

// 组件在非法输入下返回的哨兵错误，调用方可用 errors.Is 判定。
var (
	// ErrUnknownStream 表示目标流不存在或已经关闭。
	ErrUnknownStream = errors.New("h2sender: unknown stream")
	// ErrInvalidIncrement 表示窗口增量不在合法区间 (0, 2^31-1]。
	ErrInvalidIncrement = errors.New("h2sender: invalid window update increment")
	// ErrWindowOverflow 表示窗口余额加上增量后会超过 2^31-1。
	ErrWindowOverflow = errors.New("h2sender: flow control window overflow")
	// ErrInvalidMaxFrameSize 表示最大帧长超出 RFC 7540 允许的区间。
	ErrInvalidMaxFrameSize = errors.New("h2sender: invalid max frame size")
	// ErrNegativeLength 表示试图排队负数长度的数据。
	ErrNegativeLength = errors.New("h2sender: negative enqueue length")
)

// Frame 表示一次调度产出的非空 DATA 帧。
//
// 这里不关心真实线上的帧头编码，只记录该帧属于哪个流、本次实际发送了多少
// 字节以及发送后该流还剩多少字节未发。
type Frame struct {
	// StreamID 是该 DATA 帧所属的流编号。
	StreamID uint32
	// Length 是帧负载字节数，恒大于 0。
	Length int
	// Remaining 是该帧发送之后该流仍待发的字节数。
	Remaining int
}

// StreamBalance 描述单个流当前的流级窗口余额与待发字节。
type StreamBalance struct {
	// StreamID 是流编号。
	StreamID uint32
	// Window 是流级窗口余额，允许为负（初始窗口下调时可能出现）。
	Window int
	// Pending 是尚未产出 DATA 帧的排队字节数。
	Pending int
}

// Config 是构造发送端时使用的初始设置。
type Config struct {
	// InitialStreamWindow 是本地记录的 SETTINGS_INITIAL_WINDOW_SIZE 当前值。
	InitialStreamWindow int
	// ConnectionWindow 是连接级窗口的初始余额。
	ConnectionWindow int
	// MaxFrameSize 是 SETTINGS_MAX_FRAME_SIZE 当前值。
	MaxFrameSize int
}

// stream 保存单个流的发送状态。
type stream struct {
	id      uint32
	pending int // 待发字节；已产出帧的字节不会再回到这里
	window  int // 流级窗口余额，允许为负
	active  bool
}

// Sender 是 HTTP/2 发送端流控调度器。
//
// Sender 不是并发安全的：HTTP/2 发送端的 SETTINGS/WINDOW_UPDATE/数据排队
// 本就需要在同一发送循环里串行处理，调用方负责加锁。
type Sender struct {
	initialWindow int
	connWindow    int // 连接级余额，单独维护，不与任何流余额混用
	maxFrame      int
	streams       map[uint32]*stream
	order         []uint32 // 已打开流的编号，保持升序
}

// NewSender 按给定配置构造发送端。零值字段会回落到 RFC 7540 默认值。
func NewSender(cfg Config) (*Sender, error) {
	initWin := cfg.InitialStreamWindow
	if initWin == 0 {
		initWin = DefaultInitialWindowSize
	}
	connWin := cfg.ConnectionWindow
	if connWin == 0 {
		connWin = DefaultInitialWindowSize
	}
	maxFrame := cfg.MaxFrameSize
	if maxFrame == 0 {
		maxFrame = DefaultMaxFrameSize
	}
	if initWin < 0 || initWin > MaxWindowSize {
		return nil, errors.New("h2sender: initial stream window out of range")
	}
	if connWin < 0 || connWin > MaxWindowSize {
		return nil, errors.New("h2sender: connection window out of range")
	}
	if maxFrame < MinMaxFrameSize || maxFrame > MaxMaxFrameSize {
		return nil, ErrInvalidMaxFrameSize
	}
	return &Sender{
		initialWindow: initWin,
		connWindow:    connWin,
		maxFrame:      maxFrame,
		streams:       make(map[uint32]*stream),
	}, nil
}

// OpenStream 打开一个新流，其流级窗口余额取当前初始流窗口设置。
func (s *Sender) OpenStream(id uint32) error {
	if _, ok := s.streams[id]; ok {
		return errors.New("h2sender: stream already exists")
	}
	st := &stream{id: id, window: s.initialWindow, active: true}
	s.streams[id] = st
	s.order = append(s.order, id)
	sort.Slice(s.order, func(i, j int) bool { return s.order[i] < s.order[j] })
	return nil
}

// CloseStream 关闭流。关闭后初始窗口差值不再作用于它，也不能再排队或收增量。
// 关闭流不会“退回”已消耗的窗口，这与真实 HTTP/2 流结束语义一致。
func (s *Sender) CloseStream(id uint32) error {
	st, ok := s.streams[id]
	if !ok {
		return ErrUnknownStream
	}
	st.active = false
	delete(s.streams, id)
	idx := sort.Search(len(s.order), func(i int) bool { return s.order[i] >= id })
	if idx < len(s.order) && s.order[idx] == id {
		s.order = append(s.order[:idx], s.order[idx+1:]...)
	}
	return nil
}

// Enqueue 在指定流上排队 length 字节待发数据。
// 组件不持有真实负载，只记账；所有已排队字节最多被产出一次。
func (s *Sender) Enqueue(id uint32, length int) error {
	if length < 0 {
		return ErrNegativeLength
	}
	st, ok := s.streams[id]
	if !ok {
		return ErrUnknownStream
	}
	st.pending += length
	return nil
}

// SetMaxFrameSize 应用对端公布的新 SETTINGS_MAX_FRAME_SIZE。
func (s *Sender) SetMaxFrameSize(size int) error {
	if size < MinMaxFrameSize || size > MaxMaxFrameSize {
		return ErrInvalidMaxFrameSize
	}
	s.maxFrame = size
	return nil
}

// ApplyInitialWindowDelta 处理 SETTINGS_INITIAL_WINDOW_SIZE 的变化。
//
// delta 是新值减旧值（可为负），按差值作用到当前所有活动流；流余额因此
// 允许变成负数。连接级窗口不受影响。新打开的流直接使用新的初始值。
func (s *Sender) ApplyInitialWindowDelta(delta int) error {
	newInitial := s.initialWindow + delta
	if newInitial < 0 || newInitial > MaxWindowSize {
		return errors.New("h2sender: resulting initial window out of range")
	}
	for _, id := range s.order {
		st := s.streams[id]
		newWin := st.window + delta
		// 单个流窗口同样不得越过 2^31-1 上限；负值是允许的，不报错。
		if newWin > MaxWindowSize {
			return ErrWindowOverflow
		}
		st.window = newWin
	}
	s.initialWindow = newInitial
	return nil
}

// UpdateStreamWindow 处理某一个流的 WINDOW_UPDATE 帧，只恢复该流的余额。
func (s *Sender) UpdateStreamWindow(id uint32, increment int) error {
	if increment <= 0 || increment > MaxWindowSize {
		return ErrInvalidIncrement
	}
	st, ok := s.streams[id]
	if !ok {
		return ErrUnknownStream
	}
	if st.window > MaxWindowSize-increment {
		return ErrWindowOverflow
	}
	st.window += increment
	return nil
}

// UpdateConnectionWindow 处理连接级 WINDOW_UPDATE 帧，只恢复连接余额。
func (s *Sender) UpdateConnectionWindow(increment int) error {
	if increment <= 0 || increment > MaxWindowSize {
		return ErrInvalidIncrement
	}
	if s.connWindow > MaxWindowSize-increment {
		return ErrWindowOverflow
	}
	s.connWindow += increment
	return nil
}

// Flush 执行一轮完整的轮询调度，返回本次产出的非空 DATA 帧序列。
//
// 调度按流编号升序逐轮扫描：每一轮中，每个仍可发送的流最多产出一个非空
// DATA 帧；某一轮没有任何帧产出时调度结束（说明所有流都被待发数据或某个
// 窗口卡住）。帧大小是以下四者的最小值：待发字节、最大帧长、连接余额、
// 流余额。流余额为负或为零的流本轮跳过，等待后续 WINDOW_UPDATE 或初始
// 窗口调整恢复。
func (s *Sender) Flush() []Frame {
	frames := make([]Frame, 0)
	for {
		emitted := false
		for _, id := range s.order {
			st := s.streams[id]
			if st.pending <= 0 || st.window <= 0 || s.connWindow <= 0 {
				continue
			}
			n := minInt(st.pending, s.maxFrame)
			n = minInt(n, s.connWindow)
			n = minInt(n, st.window)
			if n <= 0 {
				continue
			}
			// 扣减：字节一旦产出就离开待发队列，绝不重发。
			st.pending -= n
			st.window -= n
			s.connWindow -= n
			frames = append(frames, Frame{
				StreamID:  id,
				Length:    n,
				Remaining: st.pending,
			})
			emitted = true
		}
		if !emitted {
			return frames
		}
	}
}

// ConnectionWindow 返回当前连接级窗口余额。
func (s *Sender) ConnectionWindow() int { return s.connWindow }

// InitialStreamWindow 返回当前生效的初始流窗口设置。
func (s *Sender) InitialStreamWindow() int { return s.initialWindow }

// MaxFrameSize 返回当前最大帧长设置。
func (s *Sender) MaxFrameSize() int { return s.maxFrame }

// StreamBalance 返回指定流当前的流级余额与待发字节。
func (s *Sender) StreamBalance(id uint32) (StreamBalance, error) {
	st, ok := s.streams[id]
	if !ok {
		return StreamBalance{}, ErrUnknownStream
	}
	return StreamBalance{StreamID: id, Window: st.window, Pending: st.pending}, nil
}

// Balances 按流编号升序返回全部活动流的余额快照，并附上连接级余额，
// 供调用方在每次调度后核对窗口状态。
func (s *Sender) Balances() (connectionWindow int, streams []StreamBalance) {
	streams = make([]StreamBalance, 0, len(s.order))
	for _, id := range s.order {
		st := s.streams[id]
		streams = append(streams, StreamBalance{
			StreamID: id,
			Window:   st.window,
			Pending:  st.pending,
		})
	}
	return s.connWindow, streams
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}
