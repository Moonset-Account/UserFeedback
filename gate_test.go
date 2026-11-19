package sendgate

import (
	"testing"
)

// fakeTransport 是测试用传输层：提供连接、返回路径（对端地址）、连接换
// 地址，以及“实际收到的唯一数据报编号和字节数”的独立统计，作为门禁
// 账本之外的对照事实来源。
type fakeTransport struct {
	g       *Gate
	current map[string]string // 连接 -> 当前对端返回地址

	// 每条路径自身维护的去重集合与统计，用于和门禁结果交叉核对。
	seen  map[pathKey]map[int64]struct{}
	bytes map[pathKey]int64
	count map[pathKey]int
}

func newFakeTransport(headerSize int64) *fakeTransport {
	return &fakeTransport{
		g:       New(headerSize),
		current: map[string]string{},
		seen:    map[pathKey]map[int64]struct{}{},
		bytes:   map[pathKey]int64{},
		count:   map[pathKey]int{},
	}
}

func (f *fakeTransport) key(conn, addr string) pathKey {
	return pathKey{conn: conn, addr: addr}
}

// Connect 建立连接并登记初始返回路径。
func (f *fakeTransport) Connect(conn, addr string) {
	f.current[conn] = addr
}

// Migrate 模拟连接更换对端地址；旧路径状态在传输层与门禁中都保留。
func (f *fakeTransport) Migrate(conn, newAddr string) {
	f.current[conn] = newAddr
}

func (f *fakeTransport) Addr(conn string) string {
	return f.current[conn]
}

// Deliver 向指定路径投递一个数据报解析事件，并断言传输层去重判断与门禁
// 去重判断完全一致。
func (f *fakeTransport) Deliver(t *testing.T, conn, addr string, number, size int64) bool {
	t.Helper()
	k := f.key(conn, addr)
	if f.seen[k] == nil {
		f.seen[k] = map[int64]struct{}{}
	}
	_, localFirst := f.seen[k][number]
	if !localFirst {
		f.seen[k][number] = struct{}{}
		f.bytes[k] += size
		f.count[k]++
	}
	counted := f.g.OnReceive(conn, addr, number, size)
	if counted != !localFirst {
		t.Fatalf("路径 (%s,%s) 数据报 %d：传输层首次=%v 与门禁入账=%v 不一致",
			conn, addr, number, !localFirst, counted)
	}
	return counted
}

// UniqueStats 返回传输层独立统计的唯一数据报条数与字节数。
func (f *fakeTransport) UniqueStats(conn, addr string) (int, int64) {
	k := f.key(conn, addr)
	return f.count[k], f.bytes[k]
}

func (f *fakeTransport) Verify(conn, addr string) error {
	return f.g.OnVerified(conn, addr)
}

func (f *fakeTransport) Send(conn, addr string, number int64, responseSizes []int64) (bool, error) {
	return f.g.TrySend(conn, addr, number, responseSizes)
}

func (f *fakeTransport) Succeed(conn string, number int64) error {
	return f.g.OnSendResult(conn, number, true)
}

func (f *fakeTransport) Fail(conn string, number int64) error {
	return f.g.OnSendResult(conn, number, false)
}

func mustReport(t *testing.T, g *Gate, conn, addr string) PathReport {
	t.Helper()
	r, ok := g.ReportPath(conn, addr)
	if !ok {
		t.Fatalf("路径 (%s,%s) 应存在于 Report 中，实际不存在", conn, addr)
	}
	return r
}

func wantRemaining(t *testing.T, r PathReport, want int64) {
	t.Helper()
	if r.Remaining != want {
		t.Fatalf("路径 (%s,%s) 剩余额度 = %d, 期望 %d", r.Conn, r.Addr, r.Remaining, want)
	}
}

func wantCharged(t *testing.T, r PathReport, want int64) {
	t.Helper()
	if r.Charged != want {
		t.Fatalf("路径 (%s,%s) 已预留字节 = %d, 期望 %d", r.Conn, r.Addr, r.Charged, want)
	}
}

func wantApproved(t *testing.T, r PathReport, want ...int64) {
	t.Helper()
	if len(r.ApprovedPackets) != len(want) {
		t.Fatalf("路径 (%s,%s) 获准包号 = %v, 期望 %v", r.Conn, r.Addr, r.ApprovedPackets, want)
	}
	for i, n := range want {
		if r.ApprovedPackets[i] != n {
			t.Fatalf("路径 (%s,%s) 获准包号 = %v, 期望 %v", r.Conn, r.Addr, r.ApprovedPackets, want)
		}
	}
}

// TestUnverifiedQuotaAcceptBlockAndBoundary 覆盖可接受结果、应被阻止结果
// 与二者之间的边界：未验证路径额度 = 唯一已收字节的 3 倍。
func TestUnverifiedQuotaAcceptBlockAndBoundary(t *testing.T) {
	const header = 10
	tr := newFakeTransport(header)
	tr.Connect("c1", "A")

	if !tr.Deliver(t, "c1", "A", 1, 100) {
		t.Fatal("首个数据报应入账")
	}
	if !tr.Deliver(t, "c1", "A", 2, 50) {
		t.Fatal("新编号数据报应入账")
	}
	// 唯一字节 150 -> 额度 450。
	r := mustReport(t, tr.g, "c1", "A")
	if r.UniqueReceived != 150 {
		t.Fatalf("唯一已收字节 = %d, 期望 150", r.UniqueReceived)
	}
	wantRemaining(t, r, 450)

	// 可接受：整包 = 10(包头) + 200 + 140 = 350 <= 450。
	ok, err := tr.Send("c1", "A", 10, []int64{200, 140})
	if err != nil || !ok {
		t.Fatalf("包 10 应获准，ok=%v err=%v", ok, err)
	}
	wantRemaining(t, mustReport(t, tr.g, "c1", "A"), 100)

	// 应被阻止：整包 110 > 剩余 100，整包一字节都不预留。
	ok, err = tr.Send("c1", "A", 11, []int64{100})
	if err != nil || ok {
		t.Fatalf("包 11 应被拒绝，ok=%v err=%v", ok, err)
	}
	wantRemaining(t, mustReport(t, tr.g, "c1", "A"), 100)
	wantCharged(t, mustReport(t, tr.g, "c1", "A"), 350)

	// 边界：整包恰好等于剩余额度 100，必须获准。
	ok, _ = tr.Send("c1", "A", 12, []int64{90})
	if !ok {
		t.Fatal("边界包 12（恰好占满剩余额度）应获准")
	}
	wantRemaining(t, mustReport(t, tr.g, "c1", "A"), 0)

	// 额度为零后，连只有包头的包也发不下。
	ok, _ = tr.Send("c1", "A", 13, nil)
	if ok {
		t.Fatal("额度耗尽后包头包也应被拒绝")
	}

	// 明确失败归还预留，额度重新可用。
	if err := tr.Fail("c1", 10); err != nil {
		t.Fatalf("失败确认出错: %v", err)
	}
	wantRemaining(t, mustReport(t, tr.g, "c1", "A"), 350)

	// 复用此前被拒的包号重试，现在可以获准。
	ok, _ = tr.Send("c1", "A", 13, []int64{90})
	if !ok {
		t.Fatal("归还额度后包 13 应获准")
	}
	// 成功确认不归还额度：剩余仍为 250（450 - 100 - 100）。
	if err := tr.Succeed("c1", 12); err != nil {
		t.Fatalf("成功确认出错: %v", err)
	}
	r = mustReport(t, tr.g, "c1", "A")
	wantRemaining(t, r, 250)
	wantCharged(t, r, 200)
	wantApproved(t, r, 10, 12, 13)
}

// TestDuplicateDatagramParsedOnce 验证同一数据报的多个解析事件只能入账
// 一次，并与传输层独立统计交叉核对。
func TestDuplicateDatagramParsedOnce(t *testing.T) {
	tr := newFakeTransport(10)
	tr.Connect("c2", "B")

	tr.Deliver(t, "c2", "B", 7, 100)
	for i := 0; i < 3; i++ {
		if tr.Deliver(t, "c2", "B", 7, 100) {
			t.Fatal("重复解析事件不得再次入账")
		}
	}
	tr.Deliver(t, "c2", "B", 8, 100)
	// 再次重复旧编号，仍不得入账。
	if tr.Deliver(t, "c2", "B", 7, 100) {
		t.Fatal("旧编号的迟到重复事件不得入账")
	}

	gotCount, gotBytes := tr.UniqueStats("c2", "B")
	if gotCount != 2 || gotBytes != 200 {
		t.Fatalf("传输层唯一统计 = (%d 条, %d 字节)，期望 (2, 200)", gotCount, gotBytes)
	}
	r := mustReport(t, tr.g, "c2", "B")
	if r.UniqueReceived != gotBytes {
		t.Fatalf("门禁唯一字节 %d 与传输层统计 %d 不一致", r.UniqueReceived, gotBytes)
	}
	wantRemaining(t, r, 600) // 200 * 3
}

// TestBundledReservationAtomic 验证多个响应拼成一包时连同包头整体预留，
// 发不下就整包拒绝；同时覆盖零响应边界与零包头配置。
func TestBundledReservationAtomic(t *testing.T) {
	const header = 10
	tr := newFakeTransport(header)
	tr.Connect("c3", "C")
	tr.Deliver(t, "c3", "C", 1, 100) // 额度 300

	// 整包 = 10 + 150 + 140 = 300，恰好占满。
	ok, _ := tr.Send("c3", "C", 20, []int64{150, 140})
	if !ok {
		t.Fatal("拼包恰好等于额度时应获准")
	}
	wantRemaining(t, mustReport(t, tr.g, "c3", "C"), 0)

	// 只差 1 字节：整包 11 > 0，拒绝且额度不变。
	ok, _ = tr.Send("c3", "C", 21, []int64{1})
	if ok {
		t.Fatal("额度不足时拼包必须整体拒绝")
	}
	wantRemaining(t, mustReport(t, tr.g, "c3", "C"), 0)
	wantApproved(t, mustReport(t, tr.g, "c3", "C"), 20)

	// 失败归还后，同样的拼包可再次整体预留。
	if err := tr.Fail("c3", 20); err != nil {
		t.Fatalf("失败确认出错: %v", err)
	}
	ok, _ = tr.Send("c3", "C", 22, []int64{150, 140})
	if !ok {
		t.Fatal("归还后拼包应能再次获准")
	}

	// 边界：零响应包仍占用固定包头字节。
	tr2 := newFakeTransport(header)
	tr2.Connect("c4", "D")
	tr2.Deliver(t, "c4", "D", 1, 10) // 额度 30
	ok, _ = tr2.Send("c4", "D", 30, nil)
	if !ok {
		t.Fatal("零响应包在额度足够时应获准并预留包头")
	}
	r := mustReport(t, tr2.g, "c4", "D")
	wantCharged(t, r, header)
	wantRemaining(t, r, 20)

	// 零包头配置下，零响应包预留 0 字节，额度不受影响。
	g0 := New(0)
	g0.OnReceive("c5", "E", 1, 30) // 额度 90
	ok, _ = g0.TrySend("c5", "E", 40, nil)
	if !ok {
		t.Fatal("零包头零响应包应获准")
	}
	r0, _ := g0.ReportPath("c5", "E")
	wantCharged(t, r0, 0)
	wantRemaining(t, r0, 90)
}

// TestOnlyExplicitFailureRefunds 验证只有明确失败才归还预留；成功确认
// 不归还，未知/重复确认被拒绝，在途包号不可复用。
func TestOnlyExplicitFailureRefunds(t *testing.T) {
	tr := newFakeTransport(10)
	tr.Connect("c6", "F")
	tr.Deliver(t, "c6", "F", 1, 100) // 额度 300

	ok, _ := tr.Send("c6", "F", 1, []int64{90}) // 整包 100
	if !ok {
		t.Fatal("包 1 应获准")
	}
	// 在途期间同一包号不得再次获准。
	if _, err := tr.Send("c6", "F", 1, []int64{1}); err != ErrPacketInflight {
		t.Fatalf("在途包号复用应返回 ErrPacketInflight，实际 %v", err)
	}

	// 成功不归还额度。
	if err := tr.Succeed("c6", 1); err != nil {
		t.Fatalf("成功确认出错: %v", err)
	}
	wantRemaining(t, mustReport(t, tr.g, "c6", "F"), 200)
	// 结束在途后包号可以复用。
	ok, _ = tr.Send("c6", "F", 1, []int64{190}) // 整包 200
	if !ok {
		t.Fatal("在途结束后包号应可复用")
	}

	// 明确失败才归还。
	if err := tr.Fail("c6", 1); err != nil {
		t.Fatalf("失败确认出错: %v", err)
	}
	wantRemaining(t, mustReport(t, tr.g, "c6", "F"), 200)

	// 未知包号、重复确认都不得改动账本。
	if err := tr.Succeed("c6", 999); err != ErrPacketUnknown {
		t.Fatalf("未知包确认应返回 ErrPacketUnknown，实际 %v", err)
	}
	if err := tr.Fail("c6", 1); err != ErrPacketUnknown {
		t.Fatalf("重复失败确认应返回 ErrPacketUnknown，实际 %v", err)
	}
	wantRemaining(t, mustReport(t, tr.g, "c6", "F"), 200)
}

// TestMigrationRouting 验证连接换地址后的交错场景：新路径从零开始，旧
// 路径的验证回执与迟到发送确认只归原路径。
func TestMigrationRouting(t *testing.T) {
	tr := newFakeTransport(10)
	tr.Connect("c7", "A")
	tr.Deliver(t, "c7", "A", 1, 100) // 旧路径 A 额度 300

	ok, _ := tr.Send("c7", "A", 1, []int64{90}) // 整包 100，在途
	if !ok {
		t.Fatal("旧路径包 1 应获准")
	}

	// 连接换到新地址 B：新路径从零开始。
	tr.Migrate("c7", "B")
	if tr.Addr("c7") != "B" {
		t.Fatal("传输层当前地址应为 B")
	}
	ok, _ = tr.Send("c7", "B", 2, []int64{90})
	if ok {
		t.Fatal("新路径在收到任何数据报前额度为零，必须拒绝")
	}
	rb := mustReport(t, tr.g, "c7", "B")
	if rb.UniqueReceived != 0 || rb.Remaining != 0 {
		t.Fatalf("新路径应从零开始，实际 unique=%d remaining=%d", rb.UniqueReceived, rb.Remaining)
	}

	// 同一数据报编号在新路径上是首次入账。
	if !tr.Deliver(t, "c7", "B", 1, 100) {
		t.Fatal("同一编号在新路径上应允许入账一次")
	}
	ok, _ = tr.Send("c7", "B", 2, []int64{90}) // 整包 100
	if !ok {
		t.Fatal("新路径收包后包 2 应获准")
	}

	// 迟到的发送失败确认：只归原路径 A，B 不受影响。
	if err := tr.Fail("c7", 1); err != nil {
		t.Fatalf("迟到失败确认出错: %v", err)
	}
	ra := mustReport(t, tr.g, "c7", "A")
	wantCharged(t, ra, 0)
	wantRemaining(t, ra, 300)
	rb = mustReport(t, tr.g, "c7", "B")
	wantCharged(t, rb, 100)
	wantRemaining(t, rb, 200)

	// 旧路径的迟到验证回执只验证 A，不提升 B。
	if err := tr.Verify("c7", "A"); err != nil {
		t.Fatalf("旧路径验证出错: %v", err)
	}
	ra = mustReport(t, tr.g, "c7", "A")
	if !ra.Verified || ra.Remaining != UnlimitedRemaining {
		t.Fatalf("旧路径应已解除限制，实际 verified=%v remaining=%d", ra.Verified, ra.Remaining)
	}
	rb = mustReport(t, tr.g, "c7", "B")
	if rb.Verified {
		t.Fatal("旧路径验证不得作用于新路径")
	}
	wantRemaining(t, rb, 200)

	// 未知地址的验证回执必须被拒绝。
	if err := tr.Verify("c7", "Z"); err != ErrUnknownPath {
		t.Fatalf("未知路径验证应返回 ErrUnknownPath，实际 %v", err)
	}

	// 迟到的成功确认仍按原路径 B 归属，且不归还额度。
	if err := tr.Succeed("c7", 2); err != nil {
		t.Fatalf("迟到成功确认出错: %v", err)
	}
	wantRemaining(t, mustReport(t, tr.g, "c7", "B"), 200)

	// 新路径自己的验证回执才能解除其限制。
	if err := tr.Verify("c7", "B"); err != nil {
		t.Fatalf("新路径验证出错: %v", err)
	}
	wantRemaining(t, mustReport(t, tr.g, "c7", "B"), UnlimitedRemaining)

	wantApproved(t, mustReport(t, tr.g, "c7", "A"), 1)
	wantApproved(t, mustReport(t, tr.g, "c7", "B"), 2)

	// Report 必须同时保留新旧两条路径。
	reports := tr.g.Report()
	seen := map[string]bool{}
	for _, rep := range reports {
		seen[rep.Addr] = true
	}
	if !seen["A"] || !seen["B"] {
		t.Fatalf("Report 应同时包含旧路径 A 与新路径 B，实际 %v", seen)
	}
}

// TestVerificationLiftsLimit 验证路径验证后额度限制解除，但已预留字节
// 仍如实记账，后续收包照常累计。
func TestVerificationLiftsLimit(t *testing.T) {
	tr := newFakeTransport(10)
	tr.Connect("c8", "V")
	tr.Deliver(t, "c8", "V", 1, 10) // 额度 30

	ok, _ := tr.Send("c8", "V", 1, []int64{21}) // 整包 31，超限
	if ok {
		t.Fatal("未验证路径超额包应被拒绝")
	}
	if err := tr.Verify("c8", "V"); err != nil {
		t.Fatalf("验证出错: %v", err)
	}
	ok, _ = tr.Send("c8", "V", 1, []int64{21}) // 同一包验证后获准
	if !ok {
		t.Fatal("验证后超额包应获准")
	}
	r := mustReport(t, tr.g, "c8", "V")
	wantRemaining(t, r, UnlimitedRemaining)
	wantCharged(t, r, 31)

	// 验证后继续收包，唯一字节仍累计，失败归还仍作用于已预留字节。
	tr.Deliver(t, "c8", "V", 2, 10)
	if err := tr.Fail("c8", 1); err != nil {
		t.Fatalf("失败确认出错: %v", err)
	}
	r = mustReport(t, tr.g, "c8", "V")
	if r.UniqueReceived != 20 {
		t.Fatalf("验证后收包应继续累计，unique=%d", r.UniqueReceived)
	}
	wantCharged(t, r, 0)
	wantRemaining(t, r, UnlimitedRemaining)

	// 从未出现过的路径，验证回执直接拒绝。
	if err := tr.Verify("c8", "Nobody"); err != ErrUnknownPath {
		t.Fatalf("未知路径验证应被拒绝，实际 %v", err)
	}
}

// TestInterleavedReservationAndReceive 覆盖预留与收包交错的边界场景：
// 新收数据报抬升额度，重复事件不抬升，失败逐包归还。
func TestInterleavedReservationAndReceive(t *testing.T) {
	tr := newFakeTransport(10)
	tr.Connect("c9", "I")
	tr.Deliver(t, "c9", "I", 1, 10) // 额度 30

	ok, _ := tr.Send("c9", "I", 1, []int64{10}) // 整包 20
	if !ok {
		t.Fatal("包 1 应获准")
	}
	wantRemaining(t, mustReport(t, tr.g, "c9", "I"), 10)

	// 新数据报使额度升至 60，剩余同步升至 40。
	tr.Deliver(t, "c9", "I", 2, 10)
	wantRemaining(t, mustReport(t, tr.g, "c9", "I"), 40)
	// 重复解析不改变任何额度。
	tr.Deliver(t, "c9", "I", 2, 10)
	wantRemaining(t, mustReport(t, tr.g, "c9", "I"), 40)

	// 边界：整包恰好 40，获准后剩余为零。
	ok, _ = tr.Send("c9", "I", 2, []int64{30})
	if !ok {
		t.Fatal("边界包 2 应获准")
	}
	wantRemaining(t, mustReport(t, tr.g, "c9", "I"), 0)

	// 逐包失败归还，最终额度全部释放。
	if err := tr.Fail("c9", 2); err != nil {
		t.Fatalf("失败确认出错: %v", err)
	}
	wantRemaining(t, mustReport(t, tr.g, "c9", "I"), 40)
	if err := tr.Fail("c9", 1); err != nil {
		t.Fatalf("失败确认出错: %v", err)
	}
	r := mustReport(t, tr.g, "c9", "I")
	wantRemaining(t, r, 60)
	wantCharged(t, r, 0)

	gotCount, gotBytes := tr.UniqueStats("c9", "I")
	if gotCount != 2 || gotBytes != 20 || r.UniqueReceived != 20 {
		t.Fatalf("交叉核对失败：传输层(%d,%d) 门禁 unique=%d", gotCount, gotBytes, r.UniqueReceived)
	}
}

// TestSendWithoutReceiveRejected 验证没有任何入站数据报的路径额度为零。
func TestSendWithoutReceiveRejected(t *testing.T) {
	g := New(10)
	ok, err := g.TrySend("c10", "G", 1, []int64{1})
	if err != nil || ok {
		t.Fatalf("无入站数据报的路径发送必须被拒绝，ok=%v err=%v", ok, err)
	}
	r, exists := g.ReportPath("c10", "G")
	if !exists {
		t.Fatal("被拒路径仍应出现在 Report 中便于观测")
	}
	if r.UniqueReceived != 0 || r.Remaining != 0 || len(r.ApprovedPackets) != 0 {
		t.Fatalf("零额度路径快照异常: %+v", r)
	}
	if err := g.OnSendResult("c10", 1, true); err != ErrPacketUnknown {
		t.Fatalf("从未获准的包确认应被拒绝，实际 %v", err)
	}
}
