package h2sender

import (
	"errors"
	"testing"
)

// 汇总帧序列里每个流实际发送的字节数。
func sentByStream(frames []Frame) map[uint32]int {
	got := make(map[uint32]int)
	for _, f := range frames {
		if f.Length <= 0 {
			panic("DATA frame must be non-empty")
		}
		got[f.StreamID] += f.Length
	}
	return got
}

func assertEq(t *testing.T, name string, got, want int) {
	t.Helper()
	if got != want {
		t.Fatalf("%s = %d, want %d", name, got, want)
	}
}

func assertErr(t *testing.T, name string, got, want error) {
	t.Helper()
	if !errors.Is(got, want) {
		t.Fatalf("%s: got error %v, want %v", name, got, want)
	}
}

// 场景一（可接受结果）：多流轮询，帧大小同时受待发字节、最大帧长、
// 连接窗口与流窗口限制；每轮每个流最多一个非空 DATA 帧，按流编号升序。
func TestRoundRobinAndFrameLimits(t *testing.T) {
	s, err := NewSender(Config{
		InitialStreamWindow: 65535,
		ConnectionWindow:    100000,
		MaxFrameSize:        16384,
	})
	if err != nil {
		t.Fatalf("NewSender: %v", err)
	}
	if err := s.OpenStream(5); err != nil {
		t.Fatal(err)
	}
	if err := s.OpenStream(1); err != nil {
		t.Fatal(err)
	}
	if err := s.OpenStream(3); err != nil {
		t.Fatal(err)
	}
	if err := s.Enqueue(1, 40000); err != nil {
		t.Fatal(err)
	}
	if err := s.Enqueue(3, 40000); err != nil {
		t.Fatal(err)
	}
	if err := s.Enqueue(5, 1000); err != nil {
		t.Fatal(err)
	}

	frames := s.Flush()

	// 期望的逐帧序列：每轮按 1,3,5 升序，大流每轮最多 16384。
	want := []Frame{
		{StreamID: 1, Length: 16384, Remaining: 23616},
		{StreamID: 3, Length: 16384, Remaining: 23616},
		{StreamID: 5, Length: 1000, Remaining: 0},
		{StreamID: 1, Length: 16384, Remaining: 7232},
		{StreamID: 3, Length: 16384, Remaining: 7232},
		{StreamID: 1, Length: 7232, Remaining: 0},
		{StreamID: 3, Length: 7232, Remaining: 0},
	}
	if len(frames) != len(want) {
		t.Fatalf("frame count = %d, want %d (frames=%v)", len(frames), len(want), frames)
	}
	for i, w := range want {
		if frames[i] != w {
			t.Fatalf("frame[%d] = %+v, want %+v", i, frames[i], w)
		}
	}

	sent := sentByStream(frames)
	assertEq(t, "stream1 sent", sent[1], 40000)
	assertEq(t, "stream3 sent", sent[3], 40000)
	assertEq(t, "stream5 sent", sent[5], 1000)

	// 连接余额单独维护：100000 - 81000。
	conn, balances := s.Balances()
	assertEq(t, "connection window", conn, 19000)
	wantBal := map[uint32]StreamBalance{
		1: {StreamID: 1, Window: 25535, Pending: 0},
		3: {StreamID: 3, Window: 25535, Pending: 0},
		5: {StreamID: 5, Window: 64535, Pending: 0},
	}
	if len(balances) != 3 || balances[0].StreamID != 1 || balances[1].StreamID != 3 || balances[2].StreamID != 5 {
		t.Fatalf("balances not sorted by stream id: %+v", balances)
	}
	for _, b := range balances {
		w := wantBal[b.StreamID]
		if b != w {
			t.Fatalf("balance of stream %d = %+v, want %+v", b.StreamID, b, w)
		}
	}

	// 没有待发字节后再次调度必须产出空序列，而不是重发。
	if again := s.Flush(); len(again) != 0 {
		t.Fatalf("re-flush emitted %d frames, want 0", len(again))
	}
}

// 场景二（应被阻止 + 题面差值例子）：流余额 2 时初始窗口下调 8 -> -6，
// 再补 5 仍欠 1，该流被阻止；其他仍有额度的流照常发送。
func TestInitialWindowDeltaCanGoNegativeAndBlocks(t *testing.T) {
	s, err := NewSender(Config{
		InitialStreamWindow: 10,
		ConnectionWindow:    1000,
		MaxFrameSize:        16384,
	})
	if err != nil {
		t.Fatalf("NewSender: %v", err)
	}
	if err := s.OpenStream(1); err != nil {
		t.Fatal(err)
	}
	if err := s.OpenStream(3); err != nil {
		t.Fatal(err)
	}
	// 流 1 先消费掉 8 字节，余额变成 2；流 3 不消费，余额保持 10。
	if err := s.Enqueue(1, 8); err != nil {
		t.Fatal(err)
	}
	if first := s.Flush(); len(first) != 1 || first[0] != (Frame{1, 8, 0}) {
		t.Fatalf("first flush = %v, want one 8-byte frame", first)
	}

	// 初始设置下调 8：流 1 余额 2-8=-6，流 3 余额 10-8=2，设置本身变为 2。
	if err := s.ApplyInitialWindowDelta(-8); err != nil {
		t.Fatalf("delta -8: %v", err)
	}
	b1, _ := s.StreamBalance(1)
	b3, _ := s.StreamBalance(3)
	assertEq(t, "stream1 window after -8", b1.Window, -6)
	assertEq(t, "stream3 window after -8", b3.Window, 2)
	assertEq(t, "initial window setting after -8", s.InitialStreamWindow(), 2)

	// 再补 5：流 1 仍欠 1（-1），流 3 回到 7。
	if err := s.ApplyInitialWindowDelta(5); err != nil {
		t.Fatalf("delta +5: %v", err)
	}
	b1, _ = s.StreamBalance(1)
	b3, _ = s.StreamBalance(3)
	assertEq(t, "stream1 window after +5", b1.Window, -1)
	assertEq(t, "stream3 window after +5", b3.Window, 7)
	assertEq(t, "initial window setting after +5", s.InitialStreamWindow(), 7)

	// 两个流都排队数据：流 1 因负余额被阻止，流 3 照常发送 7 字节。
	if err := s.Enqueue(1, 10); err != nil {
		t.Fatal(err)
	}
	if err := s.Enqueue(3, 10); err != nil {
		t.Fatal(err)
	}
	frames := s.Flush()
	if len(frames) != 1 || frames[0].StreamID != 3 || frames[0].Length != 7 {
		t.Fatalf("flush = %v, want only stream3 sending 7 bytes", frames)
	}
	b1, _ = s.StreamBalance(1)
	b3, _ = s.StreamBalance(3)
	assertEq(t, "stream1 still pending", b1.Pending, 10)
	assertEq(t, "stream1 still negative", b1.Window, -1)
	assertEq(t, "stream3 pending", b3.Pending, 3)
	assertEq(t, "stream3 window", b3.Window, 0)

	// 只恢复流 1 自己：补到余额 0 仍不能发，再补 1 才能发 1 字节。
	if err := s.UpdateStreamWindow(1, 1); err != nil {
		t.Fatal(err)
	}
	if frames := s.Flush(); len(frames) != 0 {
		t.Fatalf("flush at zero window = %v, want no frames", frames)
	}
	if err := s.UpdateStreamWindow(1, 1); err != nil {
		t.Fatal(err)
	}
	frames = s.Flush()
	if len(frames) != 1 || frames[0] != (Frame{1, 1, 9}) {
		t.Fatalf("flush = %v, want stream1 one byte", frames)
	}
	b1, _ = s.StreamBalance(1)
	assertEq(t, "stream1 window after one byte", b1.Window, 0)
	assertEq(t, "stream1 pending after one byte", b1.Pending, 9)

	// 调整之后新打开的流直接使用新的初始设置 7，不继承老流的负余额。
	if err := s.OpenStream(5); err != nil {
		t.Fatal(err)
	}
	b5, _ := s.StreamBalance(5)
	assertEq(t, "new stream initial window", b5.Window, 7)
}

// 场景三（交错 + 作用域）：流级 WINDOW_UPDATE 只恢复该流，连接级更新
// 只恢复连接余额；连接窗口为 0 时所有流都发不出，恢复后从最小余额处再次受限。
func TestScopedWindowUpdatesAndConnectionInterleave(t *testing.T) {
	s, err := NewSender(Config{
		InitialStreamWindow: 10,
		ConnectionWindow:    10,
		MaxFrameSize:        16384,
	})
	if err != nil {
		t.Fatalf("NewSender: %v", err)
	}
	if err := s.OpenStream(1); err != nil {
		t.Fatal(err)
	}
	if err := s.OpenStream(3); err != nil {
		t.Fatal(err)
	}
	if err := s.Enqueue(1, 20); err != nil {
		t.Fatal(err)
	}
	if err := s.Enqueue(3, 20); err != nil {
		t.Fatal(err)
	}

	// 第一轮：流 1 吃满连接窗口 10，流 3 因连接余额 0 被跳过。
	frames := s.Flush()
	if len(frames) != 1 || frames[0] != (Frame{1, 10, 10}) {
		t.Fatalf("first flush = %v, want stream1 10 bytes", frames)
	}
	b1, _ := s.StreamBalance(1)
	b3, _ := s.StreamBalance(3)
	assertEq(t, "stream1 window", b1.Window, 0)
	assertEq(t, "stream1 pending", b1.Pending, 10)
	assertEq(t, "stream3 untouched window", b3.Window, 10)
	assertEq(t, "stream3 pending", b3.Pending, 20)
	assertEq(t, "connection window zero", s.ConnectionWindow(), 0)

	// 连接窗口为 0，即使给流 1 增量也发不出去。
	if err := s.UpdateStreamWindow(1, 5); err != nil {
		t.Fatal(err)
	}
	if frames := s.Flush(); len(frames) != 0 {
		t.Fatalf("flush with zero connection window = %v, want none", frames)
	}

	// 连接补 7：流 1 受自身余额 5 限制发 5，流 3 受连接余额 2 限制发 2。
	if err := s.UpdateConnectionWindow(7); err != nil {
		t.Fatal(err)
	}
	frames = s.Flush()
	want := []Frame{{1, 5, 5}, {3, 2, 18}}
	if len(frames) != len(want) {
		t.Fatalf("flush = %v, want %v", frames, want)
	}
	for i := range want {
		if frames[i] != want[i] {
			t.Fatalf("frame[%d] = %+v, want %+v", i, frames[i], want[i])
		}
	}
	b1, _ = s.StreamBalance(1)
	b3, _ = s.StreamBalance(3)
	assertEq(t, "stream1 window after scoped send", b1.Window, 0)
	assertEq(t, "stream3 window only reduced by its own send", b3.Window, 8)
	assertEq(t, "connection window exhausted again", s.ConnectionWindow(), 0)

	// 流 3 的增量绝不作用到流 1；连接增量绝不作用到任何流余额。
	if err := s.UpdateStreamWindow(3, 100); err != nil {
		t.Fatal(err)
	}
	if err := s.UpdateConnectionWindow(8); err != nil {
		t.Fatal(err)
	}
	frames = s.Flush()
	if len(frames) != 1 || frames[0] != (Frame{3, 8, 10}) {
		t.Fatalf("flush = %v, want stream3 8 bytes limited by connection", frames)
	}
	b1, _ = s.StreamBalance(1)
	b3, _ = s.StreamBalance(3)
	// 流 1 在连接补 7 那轮已发掉 5 字节，窗口仍为 0、pending 还剩 5。
	assertEq(t, "stream1 still blocked", b1.Window, 0)
	assertEq(t, "stream1 pending retained", b1.Pending, 5)
	assertEq(t, "stream3 window", b3.Window, 100)
	assertEq(t, "connection window", s.ConnectionWindow(), 0)

	// 拒绝类结果：未知流、非法增量、窗口溢出。
	assertErr(t, "update unknown stream", s.UpdateStreamWindow(99, 1), ErrUnknownStream)
	assertErr(t, "zero increment", s.UpdateStreamWindow(1, 0), ErrInvalidIncrement)
	assertErr(t, "negative increment", s.UpdateConnectionWindow(-1), ErrInvalidIncrement)
	assertErr(t, "increment too large", s.UpdateConnectionWindow(MaxWindowSize+1), ErrInvalidIncrement)
	assertErr(t, "stream overflow", s.UpdateStreamWindow(3, MaxWindowSize), ErrWindowOverflow)
	// 当前连接余额为 0，给 MaxWindowSize 合法；再给任何正增量都应溢出。
	if err := s.UpdateConnectionWindow(MaxWindowSize); err != nil {
		t.Fatalf("restore connection to max: %v", err)
	}
	assertErr(t, "connection overflow", s.UpdateConnectionWindow(1), ErrWindowOverflow)
}

// 场景四（边界）：逐字节级零余额边界、最大帧长切分，以及设置类拒绝路径。
func TestZeroBoundaryMaxFrameAndRejectedInputs(t *testing.T) {
	s, err := NewSender(Config{
		InitialStreamWindow: 5,
		ConnectionWindow:    5,
		MaxFrameSize:        16384,
	})
	if err != nil {
		t.Fatalf("NewSender: %v", err)
	}
	if err := s.OpenStream(1); err != nil {
		t.Fatal(err)
	}
	if err := s.OpenStream(3); err != nil {
		t.Fatal(err)
	}
	if err := s.Enqueue(1, 5); err != nil {
		t.Fatal(err)
	}
	if err := s.Enqueue(3, 5); err != nil {
		t.Fatal(err)
	}

	frames := s.Flush()
	if len(frames) != 1 || frames[0] != (Frame{1, 5, 0}) {
		t.Fatalf("flush = %v, want stream1 consuming full connection window", frames)
	}

	// 连接窗口按 3、2 两次补，流 3 分两个非空帧发完，边界余额精确归零。
	if err := s.UpdateConnectionWindow(3); err != nil {
		t.Fatal(err)
	}
	frames = s.Flush()
	if len(frames) != 1 || frames[0] != (Frame{3, 3, 2}) {
		t.Fatalf("flush = %v, want stream3 3 bytes", frames)
	}
	if err := s.UpdateConnectionWindow(2); err != nil {
		t.Fatal(err)
	}
	frames = s.Flush()
	if len(frames) != 1 || frames[0] != (Frame{3, 2, 0}) {
		t.Fatalf("flush = %v, want stream3 final 2 bytes", frames)
	}
	assertEq(t, "connection window exact zero", s.ConnectionWindow(), 0)
	if frames := s.Flush(); len(frames) != 0 {
		t.Fatalf("drained flush = %v, want empty", frames)
	}

	// 最大帧长边界：16385 字节必须切成 16384 + 1 两个帧。
	big, err := NewSender(Config{
		InitialStreamWindow: MaxWindowSize,
		ConnectionWindow:    MaxWindowSize,
		MaxFrameSize:        16384,
	})
	if err != nil {
		t.Fatalf("NewSender big: %v", err)
	}
	if err := big.OpenStream(1); err != nil {
		t.Fatal(err)
	}
	if err := big.Enqueue(1, 16385); err != nil {
		t.Fatal(err)
	}
	frames = big.Flush()
	if len(frames) != 2 || frames[0].Length != 16384 || frames[1].Length != 1 {
		t.Fatalf("max-frame split = %v, want 16384 then 1", frames)
	}
	assertErr(t, "shrink max frame below minimum", big.SetMaxFrameSize(16383), ErrInvalidMaxFrameSize)
	assertErr(t, "grow max frame above maximum", big.SetMaxFrameSize(MaxMaxFrameSize+1), ErrInvalidMaxFrameSize)
	if err := big.SetMaxFrameSize(MinMaxFrameSize); err != nil {
		t.Fatalf("legal SetMaxFrameSize: %v", err)
	}
}

// 场景五（不重发 + 流生命周期拒绝路径）：所有已发字节绝不重发，关闭的流
// 不再接受数据/增量，初始窗口差值不作用于已关闭流，也不会让组件崩溃。
func TestNoRetransmissionAndStreamLifecycle(t *testing.T) {
	s, err := NewSender(Config{
		InitialStreamWindow: 4,
		ConnectionWindow:    6,
		MaxFrameSize:        16384,
	})
	if err != nil {
		t.Fatalf("NewSender: %v", err)
	}
	if err := s.OpenStream(1); err != nil {
		t.Fatal(err)
	}
	if err := s.OpenStream(3); err != nil {
		t.Fatal(err)
	}
	if err := s.Enqueue(1, 10); err != nil {
		t.Fatal(err)
	}
	if err := s.Enqueue(3, 10); err != nil {
		t.Fatal(err)
	}

	var all []Frame
	all = append(all, s.Flush()...) // 连接 6：流1 发4（窗0）、流3 发2（窗2）
	// 交错恢复：只补连接窗口，让流 3 先耗尽自身窗口，再分别补流级窗口。
	if err := s.UpdateConnectionWindow(6); err != nil {
		t.Fatal(err)
	}
	all = append(all, s.Flush()...) // 流1 窗0跳过；流3 受自身窗2限制发2，连接余4
	// 流级增量只恢复对应流：流 1 拿 6，流 3 拿 6 补足其剩余待发。
	if err := s.UpdateStreamWindow(1, 6); err != nil {
		t.Fatal(err)
	}
	if err := s.UpdateStreamWindow(3, 6); err != nil {
		t.Fatal(err)
	}
	all = append(all, s.Flush()...) // 连接仅4：流1 发4，流3 因连接归零跳过
	if err := s.UpdateConnectionWindow(8); err != nil {
		t.Fatal(err)
	}
	all = append(all, s.Flush()...) // 流1 发2、流3 发6，两个流同时清空

	sent := sentByStream(all)
	assertEq(t, "stream1 total sent once", sent[1], 10)
	assertEq(t, "stream3 total sent once", sent[3], 10)
	b1, _ := s.StreamBalance(1)
	b3, _ := s.StreamBalance(3)
	assertEq(t, "stream1 no pending", b1.Pending, 0)
	assertEq(t, "stream3 no pending", b3.Pending, 0)
	assertEq(t, "connection fully consumed", s.ConnectionWindow(), 0)
	if frames := s.Flush(); len(frames) != 0 {
		t.Fatalf("final flush must be empty, got %v", frames)
	}

	// 拒绝路径汇总。
	assertErr(t, "enqueue unknown stream", s.Enqueue(99, 1), ErrUnknownStream)
	assertErr(t, "negative enqueue", s.Enqueue(1, -1), ErrNegativeLength)
	if err := s.OpenStream(7); err != nil {
		t.Fatal(err)
	}
	if err := s.OpenStream(7); err == nil {
		t.Fatal("duplicate OpenStream should fail")
	}
	if err := s.CloseStream(7); err != nil {
		t.Fatalf("CloseStream: %v", err)
	}
	assertErr(t, "enqueue closed stream", s.Enqueue(7, 1), ErrUnknownStream)
	assertErr(t, "update closed stream", s.UpdateStreamWindow(7, 1), ErrUnknownStream)
	assertErr(t, "close unknown stream", s.CloseStream(7), ErrUnknownStream)

	// 对活动流应用差值后，新流取新初始值；已关闭流不受影响且不会被遍历。
	if err := s.ApplyInitialWindowDelta(-2); err != nil {
		t.Fatalf("delta -2: %v", err)
	}
	b1, _ = s.StreamBalance(1)
	b3, _ = s.StreamBalance(3)
	assertEq(t, "stream1 window after delta", b1.Window, 0-2)
	assertEq(t, "stream3 window after delta", b3.Window, 0-2)
	if err := s.OpenStream(9); err != nil {
		t.Fatal(err)
	}
	b9, _ := s.StreamBalance(9)
	assertEq(t, "new stream uses updated initial window", b9.Window, 2)
	conn, balances := s.Balances()
	assertEq(t, "active stream count", len(balances), 3)
	assertEq(t, "connection window untouched by stream delta", conn, 0)
}

// 场景六：构造期非法配置必须直接拒绝，避免组件带着坏设置运行。
func TestConfigValidation(t *testing.T) {
	if _, err := NewSender(Config{MaxFrameSize: 16383}); !errors.Is(err, ErrInvalidMaxFrameSize) {
		t.Fatalf("small max frame: got %v, want ErrInvalidMaxFrameSize", err)
	}
	if _, err := NewSender(Config{MaxFrameSize: MaxMaxFrameSize + 1}); !errors.Is(err, ErrInvalidMaxFrameSize) {
		t.Fatalf("large max frame: got %v, want ErrInvalidMaxFrameSize", err)
	}
	if _, err := NewSender(Config{InitialStreamWindow: -1}); err == nil {
		t.Fatal("negative initial window should be rejected")
	}
	if _, err := NewSender(Config{InitialStreamWindow: MaxWindowSize + 1}); err == nil {
		t.Fatal("oversized initial window should be rejected")
	}
	if _, err := NewSender(Config{ConnectionWindow: -1}); err == nil {
		t.Fatal("negative connection window should be rejected")
	}

	// 默认配置回落与题面常量一致。
	s, err := NewSender(Config{})
	if err != nil {
		t.Fatalf("default config: %v", err)
	}
	assertEq(t, "default initial window", s.InitialStreamWindow(), DefaultInitialWindowSize)
	assertEq(t, "default connection window", s.ConnectionWindow(), DefaultInitialWindowSize)
	assertEq(t, "default max frame", s.MaxFrameSize(), DefaultMaxFrameSize)
}
