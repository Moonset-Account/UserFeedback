package faultcoordinator

import (
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"
)

// sharedDownstreamTopology 构造两组互不重叠的实例，共享同一下游依赖 shared。
// 每个实例在 shared 上消耗 load，便于精确控制容量边界。
func sharedDownstreamTopology(sharedCap, sharedFloor, load int) Topology {
	return Topology{
		Dependencies: []Dependency{
			{ID: "shared", Capacity: sharedCap},
			{ID: "alt", Capacity: 100},
		},
		Instances: []Instance{
			{ID: "a1", Routes: []Route{{DependencyID: "shared", Load: load}}},
			{ID: "a2", Routes: []Route{{DependencyID: "shared", Load: load}}},
			{ID: "b1", Routes: []Route{{DependencyID: "shared", Load: load}}},
			{ID: "b2", Routes: []Route{{DependencyID: "shared", Load: load}}},
			{ID: "z1", Routes: []Route{{DependencyID: "alt", Load: 1}}},
		},
		RequiredPaths: []RequiredPath{
			{ID: "p-shared", Floor: sharedFloor, DependencyIDs: []string{"shared"}},
		},
	}
}

func mustNew(t *testing.T, topo Topology) *Coordinator {
	t.Helper()
	c, err := NewCoordinator(topo)
	if err != nil {
		t.Fatalf("NewCoordinator returned unexpected error: %v", err)
	}
	return c
}

func assertStatus(t *testing.T, c *Coordinator, id string, want DrillStatus) {
	t.Helper()
	got, ok := c.Drill(id)
	if !ok {
		t.Fatalf("drill %q missing from coordinator", id)
	}
	if got.Status != want {
		t.Fatalf("drill %q status = %s, want %s", id, got.Status, want)
	}
}

// TestSharedDownstreamOrdering 覆盖题面核心场景：
// 两个互不重叠的实例集合共享同一下游；核对“等待 → 外部故障撤回 → 后来放行”的次序。
//
// 参数（容量 20、底线 6、每实例负载 5）：
//   - D-A 占两个实例（持 10），剩余 10 >= 6，准入；
//   - D-B 占一个互不重叠的实例（再持 5），叠加后剩余 4 < 6，进入等待；
//   - 外部故障削减 5 后，D-A 持有下剩余 5 < 6 而跌破；
//   - 撤回 D-A 并放行 D-B（持 5）后剩余 = 20-5-5 = 10 >= 6，恢复可行。
func TestSharedDownstreamOrdering(t *testing.T) {
	c := mustNew(t, sharedDownstreamTopology(20, 6, 5))

	da, err := c.Submit(Drill{ID: "D-A", Instances: []string{"a1", "a2"}, Duration: time.Hour, Priority: 10})
	if err != nil {
		t.Fatalf("submit D-A: %v", err)
	}
	if da.Status != StatusActive {
		t.Fatalf("D-A should be admitted immediately, got %s", da.Status)
	}
	if da.Generation != 1 {
		t.Fatalf("D-A generation = %d, want 1", da.Generation)
	}

	// D-B 只占用一个实例（与 A 的实例集合互不重叠），叠加后剩余 4 < 底线 6，等待。
	db, err := c.Submit(Drill{ID: "D-B", Instances: []string{"b1"}, Duration: time.Hour, Priority: 10})
	if err != nil {
		t.Fatalf("submit D-B: %v", err)
	}
	if db.Status != StatusWaiting {
		t.Fatalf("D-B should wait because shared floor would be breached, got %s", db.Status)
	}
	// D-A 持有 10，shared 剩余 = 20 - 10 = 10。
	if r := c.Snapshot().Residual["shared"]; r != 10 {
		t.Fatalf("residual shared = %d, want 10 (D-A holds 10 of 20)", r)
	}

	// 演练之间插入外部模拟故障：shared 削减 5，剩余 5 < 底线 6，跌破。
	res, err := c.InjectExternalFault(ExternalFault{ID: "F1", DependencyID: "shared", Reduction: 5})
	if err != nil {
		t.Fatalf("inject fault: %v", err)
	}
	if !res.Recovered {
		t.Fatalf("fault should be recoverable by withdrawing D-A, gaps=%+v", res.Infeasibility)
	}
	if len(res.Revoked) != 1 || res.Revoked[0] != "D-A" {
		t.Fatalf("revoked set = %v, want [D-A]", res.Revoked)
	}

	// D-A 被撤回（代次关闭、预算与实例立即释放），D-B 随后被 FIFO 放行。
	assertStatus(t, c, "D-A", StatusRecovering)
	assertStatus(t, c, "D-B", StatusActive)
	dbSnap, _ := c.Drill("D-B")
	if dbSnap.Generation != 2 {
		t.Fatalf("D-B generation = %d, want 2 (admitted after D-A withdrawal)", dbSnap.Generation)
	}
	// 撤回 D-A、放行 D-B（持 5）后：剩余 = 20 - 5(故障) - 5 = 10 >= 底线 6。
	if r := c.Snapshot().Residual["shared"]; r != 10 {
		t.Fatalf("residual after withdrawal and admission = %d, want 10", r)
	}

	// D-A 的代次已关闭：匹配旧代次的健康回执按过期登记，不归还任何预算。
	expired, err := c.ReceiveReceipt(Receipt{DrillID: "D-A", Generation: 1, Kind: ReceiptHealthy})
	if err != nil {
		t.Fatalf("receipt for revoked drill: %v", err)
	}
	if expired.Outcome != OutcomeExpired {
		t.Fatalf("revoked-drill receipt = %s, want EXPIRED", expired.Outcome)
	}
	assertStatus(t, c, "D-A", StatusRecovering)

	// D-B 退出后预算仍持有；收到本次代次（gen=2）健康证据才归还。
	if _, err := c.Exit("D-B"); err != nil {
		t.Fatalf("exit D-B: %v", err)
	}
	assertStatus(t, c, "D-B", StatusRecovering)
	wrongGen, err := c.ReceiveReceipt(Receipt{DrillID: "D-B", Generation: 99, Kind: ReceiptHealthy})
	if err != nil {
		t.Fatalf("stale receipt: %v", err)
	}
	if wrongGen.Outcome != OutcomeExpired {
		t.Fatalf("stale receipt outcome = %s, want EXPIRED", wrongGen.Outcome)
	}
	assertStatus(t, c, "D-B", StatusRecovering)

	ok2, err := c.ReceiveReceipt(Receipt{DrillID: "D-B", Generation: 2, Kind: ReceiptHealthy})
	if err != nil {
		t.Fatalf("healthy receipt: %v", err)
	}
	if ok2.Outcome != OutcomeAccepted {
		t.Fatalf("healthy receipt outcome = %s, want ACCEPTED", ok2.Outcome)
	}
	assertStatus(t, c, "D-B", StatusReturned)
	// 归还后仅剩外部故障削减：20 - 5 = 15。
	if r := c.Snapshot().Residual["shared"]; r != 15 {
		t.Fatalf("residual after return = %d, want 15 (20 cap - 5 fault)", r)
	}

	// 审计次序必须能核对：准入、排队、故障撤回、后来放行、过期、归还全部有记录。
	log := c.AuditLog()
	wantSeq := []string{
		"DRILL_ADMITTED", "DRILL_QUEUED",
		"FAULT_REVOKED", "FAULT_APPLIED", "DRILL_ADMITTED",
		"RECEIPT_EXPIRED", "DRILL_EXITED", "RECEIPT_EXPIRED", "DRILL_RETURNED",
	}
	var gotSeq []string
	for _, e := range log {
		gotSeq = append(gotSeq, e.Kind)
	}
	if fmt.Sprint(gotSeq) != fmt.Sprint(wantSeq) {
		t.Fatalf("audit sequence = %v, want %v", gotSeq, wantSeq)
	}
	for i := 1; i < len(log); i++ {
		if log[i].Seq <= log[i-1].Seq {
			t.Fatalf("audit seq not monotonic at %d", i)
		}
	}
}

// TestWaitThenReceiptRelease 是“等待→后来放行”的另一交错：
// 不依赖外部故障，D-B 在 D-A 的健康回执归还预算后按 FIFO 放行。
func TestWaitThenReceiptRelease(t *testing.T) {
	// 容量 20、底线 4、每实例负载 4：单演练负载 8，双演练负载 16 时剩余 4 >= 4 可共存，
	// 这里把负载提到 5：单演练 10（剩余 10），双演练 20（剩余 0 < 4），必须等待。
	c := mustNew(t, sharedDownstreamTopology(20, 4, 5))

	if _, err := c.Submit(Drill{ID: "D-A", Instances: []string{"a1", "a2"}, Duration: time.Hour}); err != nil {
		t.Fatalf("submit D-A: %v", err)
	}
	if _, err := c.Submit(Drill{ID: "D-B", Instances: []string{"b1", "b2"}, Duration: time.Hour}); err != nil {
		t.Fatalf("submit D-B: %v", err)
	}
	assertStatus(t, c, "D-A", StatusActive)
	assertStatus(t, c, "D-B", StatusWaiting)

	// 仅退出不归还：D-B 必须继续等待。
	if _, err := c.Exit("D-A"); err != nil {
		t.Fatalf("exit D-A: %v", err)
	}
	assertStatus(t, c, "D-B", StatusWaiting)

	// 取消回执不归还预算，D-B 仍等待。
	cancelRes, err := c.ReceiveReceipt(Receipt{DrillID: "D-A", Generation: 1, Kind: ReceiptCanceled})
	if err != nil {
		t.Fatalf("cancel receipt: %v", err)
	}
	if cancelRes.Outcome != OutcomeCanceled {
		t.Fatalf("cancel outcome = %s, want CANCELED", cancelRes.Outcome)
	}
	assertStatus(t, c, "D-B", StatusWaiting)
	assertStatus(t, c, "D-A", StatusRecovering)

	// 同一代次随后送达的健康回执属于重复（首份回执已处理），仍不归还。
	dup, err := c.ReceiveReceipt(Receipt{DrillID: "D-A", Generation: 1, Kind: ReceiptHealthy})
	if err != nil {
		t.Fatalf("duplicate receipt: %v", err)
	}
	if dup.Outcome != OutcomeDuplicate {
		t.Fatalf("outcome = %s, want DUPLICATE", dup.Outcome)
	}
	assertStatus(t, c, "D-B", StatusWaiting)

	// 未知演练回执：单独可核记录，不影响队列。
	unknown, err := c.ReceiveReceipt(Receipt{DrillID: "GHOST", Generation: 1, Kind: ReceiptHealthy})
	if err != nil {
		t.Fatalf("unknown receipt: %v", err)
	}
	if unknown.Outcome != OutcomeUnknown {
		t.Fatalf("outcome = %s, want UNKNOWN", unknown.Outcome)
	}
}

// TestBoundaryAdmission 精确核对容量底线边界：剩余恰好等于底线时准入，差一个单位则等待。
func TestBoundaryAdmission(t *testing.T) {
	// 容量 10、底线 2、每实例负载 4。
	c := mustNew(t, sharedDownstreamTopology(10, 2, 4))

	// D-A 占用两个实例 = 8，剩余 2，恰好等于底线，准入。
	a, err := c.Submit(Drill{ID: "D-A", Instances: []string{"a1", "a2"}, Duration: time.Hour})
	if err != nil {
		t.Fatalf("submit D-A: %v", err)
	}
	if a.Status != StatusActive {
		t.Fatalf("D-A at exact floor should be admitted, got %s", a.Status)
	}
	// D-B 再占 4 会使剩余 -2，等待。
	b, err := c.Submit(Drill{ID: "D-B", Instances: []string{"b1"}, Duration: time.Hour})
	if err != nil {
		t.Fatalf("submit D-B: %v", err)
	}
	if b.Status != StatusWaiting {
		t.Fatalf("D-B beyond floor should wait, got %s", b.Status)
	}

	// 外部故障削减 1 即跌破（剩余 2 -> 1 < 2）；撤回 D-A 后无演练持有，
	// 剩余 = 10 - 1 = 9 >= 2，恢复可行。
	res, err := c.InjectExternalFault(ExternalFault{ID: "F-edge", DependencyID: "shared", Reduction: 1})
	if err != nil {
		t.Fatalf("inject fault: %v", err)
	}
	if !res.Recovered || len(res.Revoked) != 1 || res.Revoked[0] != "D-A" {
		t.Fatalf("unexpected fault result: %+v", res)
	}
	// 撤回 D-A 后 D-B 尝试放行：剩余 = 10 - 1 - 4 = 5 >= 2，应准入。
	assertStatus(t, c, "D-B", StatusActive)
}

// TestMinimumRevocationAndTieBreak 核对最少撤回集合，以及同数量时
// “低优先级优先、再按演练 ID 定序”的规则。
func TestMinimumRevocationAndTieBreak(t *testing.T) {
	// 容量 30、底线 2，每实例负载 2；四个 ACTIVE 演练各占一个互不重叠的实例（各持 2）。
	c := mustNew(t, sharedDownstreamTopology(30, 2, 2))

	drills := []Drill{
		{ID: "D-HI", Instances: []string{"a1"}, Duration: time.Hour, Priority: 100},
		{ID: "D-LO", Instances: []string{"b1"}, Duration: time.Hour, Priority: 1},
		{ID: "D-MID", Instances: []string{"a2"}, Duration: time.Hour, Priority: 50},
		{ID: "D-MID2", Instances: []string{"b2"}, Duration: time.Hour, Priority: 50},
	}
	for _, d := range drills {
		s, err := c.Submit(d)
		if err != nil {
			t.Fatalf("submit %s: %v", d.ID, err)
		}
		if s.Status != StatusActive {
			t.Fatalf("drill %s expected active, got %s", d.ID, s.Status)
		}
	}
	// 当前持有 4*2=8，剩余 22。第一次故障削减 21 -> 剩余 1 < 2，跌破。
	// 撤回任意一个演练释放 2：剩余 3 >= 2，最少集合大小为 1。
	// 大小 1 的候选按 (优先级升序, ID 升序)：D-LO(pri=1) 胜出。
	res, err := c.InjectExternalFault(ExternalFault{ID: "F-tie", DependencyID: "shared", Reduction: 21})
	if err != nil {
		t.Fatalf("inject fault: %v", err)
	}
	if !res.Recovered {
		t.Fatalf("should be recoverable: %+v", res.Infeasibility)
	}
	if len(res.Revoked) != 1 || res.Revoked[0] != "D-LO" {
		t.Fatalf("revoked = %v, want [D-LO] (lowest priority among size-1 sets)", res.Revoked)
	}

	// 第二次故障前 ACTIVE 为 D-HI、D-MID、D-MID2，共持有 6，当前剩余 3。
	// 再削减 5：基线剩余 = 30-21-5-6 = -2 < 2；
	// 撤回 1 个释放 2 -> 0 < 2 仍不可行；撤回 2 个释放 4 -> 2 >= 2 可行，故最少大小为 2。
	res2, err := c.InjectExternalFault(ExternalFault{ID: "F-tie2", DependencyID: "shared", Reduction: 5})
	if err != nil {
		t.Fatalf("inject second fault: %v", err)
	}
	if !res2.Recovered {
		t.Fatalf("second fault should be recoverable")
	}
	// 大小 2 的集合按 (pri, id) 字典序：D-MID(50) < D-MID2(50) < D-HI(100)，
	// 故首选 {D-MID, D-MID2}。
	want := []string{"D-MID", "D-MID2"}
	if fmt.Sprint(res2.Revoked) != fmt.Sprint(want) {
		t.Fatalf("revoked = %v, want %v", res2.Revoked, want)
	}
}

// TestInfeasibleGapReport 核对任何撤回组合都无法恢复时的剩余缺口报告。
func TestInfeasibleGapReport(t *testing.T) {
	topo := Topology{
		Dependencies: []Dependency{{ID: "shared", Capacity: 10}},
		Instances: []Instance{
			{ID: "i1", Routes: []Route{{DependencyID: "shared", Load: 6}}},
		},
		RequiredPaths: []RequiredPath{
			{ID: "p1", Floor: 3, DependencyIDs: []string{"shared"}},
		},
	}
	c := mustNew(t, topo)
	if _, err := c.Submit(Drill{ID: "D-A", Instances: []string{"i1"}, Duration: time.Hour}); err != nil {
		t.Fatalf("submit: %v", err)
	}
	// 剩余 4 >= 3。故障削减 9：即使撤回 D-A，剩余 = 10-9 = 1 < 3，无法恢复。
	res, err := c.InjectExternalFault(ExternalFault{ID: "F-bad", DependencyID: "shared", Reduction: 9})
	if err != nil {
		t.Fatalf("inject fault: %v", err)
	}
	if res.Recovered {
		t.Fatalf("fault should be infeasible")
	}
	if res.Infeasibility == nil || len(res.Infeasibility.Gaps) != 1 {
		t.Fatalf("expected one breached path, got %+v", res.Infeasibility)
	}
	pg := res.Infeasibility.Gaps[0]
	if pg.PathID != "p1" || len(pg.DependencyGaps) != 1 {
		t.Fatalf("unexpected gap structure: %+v", pg)
	}
	gap := pg.DependencyGaps[0]
	// 不做部分撤回，当前持有下剩余 = 10 - 9 - 6 = -5；缺口 = 3 - (-5) = 8。
	if gap.DependencyID != "shared" || gap.Remaining != -5 || gap.Floor != 3 || gap.Shortfall != 8 {
		t.Fatalf("unexpected gap values: %+v", gap)
	}
	// 不可行时不得撤回任何演练。
	assertStatus(t, c, "D-A", StatusActive)
}

// TestAutoExitAndGeneration 核对模拟时钟推进后的自动退出、代次推进与重新提交。
func TestAutoExitAndGeneration(t *testing.T) {
	c := mustNew(t, sharedDownstreamTopology(50, 2, 3))

	s, err := c.Submit(Drill{ID: "D-A", Instances: []string{"a1"}, Duration: 2 * time.Minute})
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	if s.Generation != 1 {
		t.Fatalf("first generation = %d, want 1", s.Generation)
	}
	now, exited := c.Advance(2 * time.Minute)
	if len(exited) != 1 || exited[0] != "D-A" {
		t.Fatalf("auto-exit = %v, want [D-A]", exited)
	}
	if now.Sub(baseTime) != 2*time.Minute {
		t.Fatalf("clock = %s", now)
	}
	assertStatus(t, c, "D-A", StatusRecovering)
	// 自动退出不释放预算。
	if r := c.Snapshot().Residual["shared"]; r != 47 {
		t.Fatalf("residual after auto-exit = %d, want 47 (budget still held)", r)
	}

	r, err := c.ReceiveReceipt(Receipt{DrillID: "D-A", Generation: 1, Kind: ReceiptHealthy})
	if err != nil || r.Outcome != OutcomeAccepted {
		t.Fatalf("receipt: %+v err=%v", r, err)
	}
	assertStatus(t, c, "D-A", StatusReturned)

	// 同一 ID 重新提交获得新代次。
	s2, err := c.Submit(Drill{ID: "D-A", Instances: []string{"a1"}, Duration: time.Minute})
	if err != nil {
		t.Fatalf("resubmit: %v", err)
	}
	if s2.Status != StatusActive || s2.Generation != 2 {
		t.Fatalf("resubmitted drill = %+v", s2)
	}
	// 旧代次回执现在属于过期。
	old, err := c.ReceiveReceipt(Receipt{DrillID: "D-A", Generation: 1, Kind: ReceiptHealthy})
	if err != nil {
		t.Fatalf("stale receipt: %v", err)
	}
	if old.Outcome != OutcomeExpired {
		t.Fatalf("outcome = %s, want EXPIRED", old.Outcome)
	}
}

// TestConcurrentSubmissions 核对并发交错下的互斥与底线约束：
// 大量 goroutine 同时提交后，ACTIVE 集合必须始终满足实例互斥与容量底线。
func TestConcurrentSubmissions(t *testing.T) {
	// 容量 20、底线 2，8 个实例各负载 2。任意时刻最多 (20-2)/2 = 9，
	// 受实例数限制最多 8 个 ACTIVE；用更小容量 10 使容量成为约束：最多 4 个。
	topo := Topology{
		Dependencies: []Dependency{{ID: "shared", Capacity: 10}},
		Instances: []Instance{
			{ID: "i1", Routes: []Route{{DependencyID: "shared", Load: 2}}},
			{ID: "i2", Routes: []Route{{DependencyID: "shared", Load: 2}}},
			{ID: "i3", Routes: []Route{{DependencyID: "shared", Load: 2}}},
			{ID: "i4", Routes: []Route{{DependencyID: "shared", Load: 2}}},
			{ID: "i5", Routes: []Route{{DependencyID: "shared", Load: 2}}},
			{ID: "i6", Routes: []Route{{DependencyID: "shared", Load: 2}}},
		},
		RequiredPaths: []RequiredPath{{ID: "p", Floor: 2, DependencyIDs: []string{"shared"}}},
	}
	c := mustNew(t, topo)

	const n = 24
	var wg sync.WaitGroup
	errs := make(chan error, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			inst := fmt.Sprintf("i%d", (i%6)+1)
			_, err := c.Submit(Drill{
				ID:        fmt.Sprintf("D-%02d", i),
				Instances: []string{inst},
				Duration:  time.Hour,
			})
			errs <- err
		}(i)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatalf("concurrent submit error: %v", err)
		}
	}

	snap := c.Snapshot()
	// 容量约束：每个 ACTIVE 持 2，剩余必须 >= 2 -> 至多 4 个 ACTIVE。
	if len(snap.Active) != 4 {
		t.Fatalf("active count = %d, want 4", len(snap.Active))
	}
	if len(snap.Waiting) != n-4 {
		t.Fatalf("waiting count = %d, want %d", len(snap.Waiting), n-4)
	}
	if r := snap.Residual["shared"]; r != 2 {
		t.Fatalf("residual = %d, want exactly floor 2", r)
	}
	// 实例互斥：无两个 ACTIVE 演练共享实例。
	owners := map[string]string{}
	for _, d := range snap.Active {
		for _, inst := range d.Instances {
			if prev, dup := owners[inst]; dup {
				t.Fatalf("instance %s held by both %s and %s", inst, prev, d.ID)
			}
			owners[inst] = d.ID
		}
	}
}

// TestValidationAndGuardPaths 覆盖拓扑校验与状态机守卫。
func TestValidationAndGuardPaths(t *testing.T) {
	bad := []Topology{
		{ // 未知依赖引用
			Dependencies:  []Dependency{{ID: "d1", Capacity: 1}},
			Instances:     []Instance{{ID: "i1", Routes: []Route{{DependencyID: "ghost", Load: 1}}}},
			RequiredPaths: []RequiredPath{{ID: "p", Floor: 0, DependencyIDs: []string{"d1"}}},
		},
		{ // 非正负载
			Dependencies:  []Dependency{{ID: "d1", Capacity: 1}},
			Instances:     []Instance{{ID: "i1", Routes: []Route{{DependencyID: "d1", Load: 0}}}},
			RequiredPaths: []RequiredPath{{ID: "p", Floor: 0, DependencyIDs: []string{"d1"}}},
		},
		{ // 重复实例 ID
			Dependencies: []Dependency{{ID: "d1", Capacity: 1}},
			Instances: []Instance{
				{ID: "i1", Routes: []Route{{DependencyID: "d1", Load: 1}}},
				{ID: "i1", Routes: []Route{{DependencyID: "d1", Load: 1}}},
			},
			RequiredPaths: []RequiredPath{{ID: "p", Floor: 0, DependencyIDs: []string{"d1"}}},
		},
		{ // 路径引用未知依赖
			Dependencies:  []Dependency{{ID: "d1", Capacity: 1}},
			Instances:     []Instance{{ID: "i1", Routes: []Route{{DependencyID: "d1", Load: 1}}}},
			RequiredPaths: []RequiredPath{{ID: "p", Floor: 0, DependencyIDs: []string{"ghost"}}},
		},
	}
	for i, topo := range bad {
		if _, err := NewCoordinator(topo); !errors.Is(err, ErrInvalidTopology) {
			t.Fatalf("case %d: expected ErrInvalidTopology, got %v", i, err)
		}
	}

	c := mustNew(t, sharedDownstreamTopology(20, 2, 2))
	if _, err := c.Submit(Drill{ID: "", Instances: []string{"a1"}, Duration: time.Hour}); !errors.Is(err, ErrInvalidArgument) {
		t.Fatalf("empty id: expected ErrInvalidArgument, got %v", err)
	}
	if _, err := c.Submit(Drill{ID: "D-X", Instances: []string{"ghost"}, Duration: time.Hour}); !errors.Is(err, ErrInvalidArgument) {
		t.Fatalf("unknown instance: expected ErrInvalidArgument, got %v", err)
	}
	if _, err := c.Submit(Drill{ID: "D-X", Instances: []string{"a1"}, Duration: 0}); !errors.Is(err, ErrInvalidArgument) {
		t.Fatalf("zero duration: expected ErrInvalidArgument, got %v", err)
	}
	if _, err := c.Submit(Drill{ID: "D-X", Instances: []string{"a1"}, Duration: time.Hour}); err != nil {
		t.Fatalf("first submit: %v", err)
	}
	if _, err := c.Submit(Drill{ID: "D-X", Instances: []string{"a2"}, Duration: time.Hour}); !errors.Is(err, ErrDuplicateDrill) {
		t.Fatalf("duplicate submit: expected ErrDuplicateDrill, got %v", err)
	}
	if _, err := c.Exit("GHOST"); !errors.Is(err, ErrUnknownDrill) {
		t.Fatalf("exit unknown: expected ErrUnknownDrill, got %v", err)
	}
	if _, err := c.InjectExternalFault(ExternalFault{ID: "F", DependencyID: "ghost", Reduction: 1}); !errors.Is(err, ErrUnknownDependency) {
		t.Fatalf("fault unknown dep: expected ErrUnknownDependency, got %v", err)
	}
	if _, err := c.InjectExternalFault(ExternalFault{ID: "F", DependencyID: "shared", Reduction: 1}); err != nil {
		t.Fatalf("first fault: %v", err)
	}
	if _, err := c.InjectExternalFault(ExternalFault{ID: "F", DependencyID: "shared", Reduction: 1}); !errors.Is(err, ErrDuplicateFault) {
		t.Fatalf("duplicate fault: expected ErrDuplicateFault, got %v", err)
	}
	// 对 WAITING/RECOVERING 演练调用 Exit 必须被守卫拒绝。
	// 容量 20、底线 2、负载 2：D-A 占两个实例持 4，D-W 占两个实例再持 4 后剩余 12，
	// 天然可准入；这里用单依赖小容量拓扑制造真正的等待者。
	guardTopo := Topology{
		Dependencies:  []Dependency{{ID: "shared", Capacity: 10}},
		Instances:     []Instance{{ID: "g1", Routes: []Route{{DependencyID: "shared", Load: 6}}}, {ID: "g2", Routes: []Route{{DependencyID: "shared", Load: 6}}}},
		RequiredPaths: []RequiredPath{{ID: "p", Floor: 2, DependencyIDs: []string{"shared"}}},
	}
	gc := mustNew(t, guardTopo)
	if _, err := gc.Submit(Drill{ID: "G-A", Instances: []string{"g1"}, Duration: time.Hour}); err != nil {
		t.Fatalf("submit G-A: %v", err)
	}
	gw, err := gc.Submit(Drill{ID: "G-W", Instances: []string{"g2"}, Duration: time.Hour})
	if err != nil {
		t.Fatalf("submit G-W: %v", err)
	}
	if gw.Status != StatusWaiting {
		t.Fatalf("G-W should be waiting, got %s", gw.Status)
	}
	if _, err := gc.Exit("G-W"); !errors.Is(err, ErrNotActive) {
		t.Fatalf("exit waiting: expected ErrNotActive, got %v", err)
	}
	if _, err := gc.Exit("G-A"); err != nil {
		t.Fatalf("exit G-A: %v", err)
	}
	if _, err := gc.Exit("G-A"); !errors.Is(err, ErrNotActive) {
		t.Fatalf("re-exit recovering: expected ErrNotActive, got %v", err)
	}
}

// TestMultiHopPathShortBoard 核对多依赖路径的短板语义：
// 路径覆盖两个依赖，任一依赖跌破底线即视为整条路径不满足。
func TestMultiHopPathShortBoard(t *testing.T) {
	topo := Topology{
		Dependencies: []Dependency{
			{ID: "d1", Capacity: 10},
			{ID: "d2", Capacity: 10},
		},
		Instances: []Instance{
			{ID: "i1", Routes: []Route{
				{DependencyID: "d1", Load: 6},
				{DependencyID: "d2", Load: 6},
			}},
			{ID: "i2", Routes: []Route{
				{DependencyID: "d1", Load: 3},
				{DependencyID: "d2", Load: 3},
			}},
		},
		RequiredPaths: []RequiredPath{
			{ID: "p", Floor: 2, DependencyIDs: []string{"d1", "d2"}},
		},
	}
	c := mustNew(t, topo)
	// i1 后两依赖各剩 4 >= 2，准入。
	if _, err := c.Submit(Drill{ID: "D-A", Instances: []string{"i1"}, Duration: time.Hour}); err != nil {
		t.Fatalf("submit D-A: %v", err)
	}
	// i2 再各加 3：剩余 1 < 2，两依赖同时跌破，必须等待。
	s, err := c.Submit(Drill{ID: "D-B", Instances: []string{"i2"}, Duration: time.Hour})
	if err != nil {
		t.Fatalf("submit D-B: %v", err)
	}
	if s.Status != StatusWaiting {
		t.Fatalf("D-B should wait on short-board breach, got %s", s.Status)
	}
	// 外部故障打 d1 削减 3：D-A 持有下 d1 剩余 = 10-3-6 = 1 < 2，跌破；
	// 撤回 D-A 后 d1 剩余 = 10-3 = 7 >= 2，d2 剩余 10 >= 2，恢复可行。
	res, err := c.InjectExternalFault(ExternalFault{ID: "F", DependencyID: "d1", Reduction: 3})
	if err != nil {
		t.Fatalf("fault: %v", err)
	}
	if !res.Recovered || fmt.Sprint(res.Revoked) != "[D-A]" {
		t.Fatalf("unexpected fault result: %+v", res)
	}
	// D-B 放行后：d1 = 10-3-3=4, d2 = 10-3=7，均 >= 2。
	assertStatus(t, c, "D-B", StatusActive)
	snap := c.Snapshot()
	if snap.Residual["d1"] != 4 || snap.Residual["d2"] != 7 {
		t.Fatalf("residual map = %+v", snap.Residual)
	}
}
