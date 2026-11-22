package faultcoordinator

import (
	"fmt"
	"testing"
	"time"
)

// TestReceiptReturnUnblocksFIFO 核对健康回执归还预算后等待队列按 FIFO 放行，
// 以及队头阻塞：队头仍不可准入时，其后的等待者即使自身可行也不得插队。
func TestReceiptReturnUnblocksFIFO(t *testing.T) {
	// 容量 12、底线 2，每实例负载 3，共 5 个实例。
	topo := Topology{
		Dependencies: []Dependency{{ID: "shared", Capacity: 12}},
		Instances: []Instance{
			{ID: "i1", Routes: []Route{{DependencyID: "shared", Load: 3}}},
			{ID: "i2", Routes: []Route{{DependencyID: "shared", Load: 3}}},
			{ID: "i3", Routes: []Route{{DependencyID: "shared", Load: 3}}},
			{ID: "i4", Routes: []Route{{DependencyID: "shared", Load: 3}}},
			{ID: "i5", Routes: []Route{{DependencyID: "shared", Load: 3}}},
		},
		RequiredPaths: []RequiredPath{{ID: "p", Floor: 2, DependencyIDs: []string{"shared"}}},
	}
	c := mustNew(t, topo)

	// D-A 持 9（i1-i3），剩余 3 >= 2 准入。
	if _, err := c.Submit(Drill{ID: "D-A", Instances: []string{"i1", "i2", "i3"}, Duration: time.Hour}); err != nil {
		t.Fatalf("submit D-A: %v", err)
	}
	// 队头 D-BIG 需要三个空闲实例（i3-i5，持 9）：D-A 持有下叠加剩余 -6，等待。
	if s, err := c.Submit(Drill{ID: "D-BIG", Instances: []string{"i3", "i4", "i5"}, Duration: time.Hour}); err != nil {
		t.Fatalf("submit D-BIG: %v", err)
	} else if s.Status != StatusWaiting {
		t.Fatalf("D-BIG should wait, got %s", s.Status)
	}
	// D-SMALL 只需 i4（持 3）：D-A 下叠加剩余 0 < 2，等待，排在 D-BIG 之后。
	if s, err := c.Submit(Drill{ID: "D-SMALL", Instances: []string{"i4"}, Duration: time.Hour}); err != nil {
		t.Fatalf("submit D-SMALL: %v", err)
	} else if s.Status != StatusWaiting {
		t.Fatalf("D-SMALL should wait, got %s", s.Status)
	}
	// D-TINY 只需 i5。注意：D-A 一旦归还，D-TINY 单独看完全可行（剩余 9 >= 2、
	// i5 空闲），但它必须被队头 D-BIG 阻塞。
	if s, err := c.Submit(Drill{ID: "D-TINY", Instances: []string{"i5"}, Duration: time.Hour}); err != nil {
		t.Fatalf("submit D-TINY: %v", err)
	} else if s.Status != StatusWaiting {
		t.Fatalf("D-TINY should wait behind the blocked head, got %s", s.Status)
	}

	// D-A 退出 + 健康回执：预算归还，剩余回到 12、i1-i3 全部释放。
	if _, err := c.Exit("D-A"); err != nil {
		t.Fatalf("exit D-A: %v", err)
	}
	r, err := c.ReceiveReceipt(Receipt{DrillID: "D-A", Generation: 1, Kind: ReceiptHealthy})
	if err != nil || r.Outcome != OutcomeAccepted {
		t.Fatalf("receipt: %+v err=%v", r, err)
	}
	// 队头 D-BIG 持 9 后剩余 3 >= 2、i3-i5 全空闲，准入；
	// 其后 D-SMALL 与 D-TINY 需要的 i4/i5 已被 D-BIG 占用，继续等待——
	// 即使按容量它们本可被接受，FIFO 次序与实例互斥使它们无法插队。
	assertStatus(t, c, "D-BIG", StatusActive)
	assertStatus(t, c, "D-SMALL", StatusWaiting)
	assertStatus(t, c, "D-TINY", StatusWaiting)
	if gen, _ := c.Drill("D-BIG"); gen.Generation != 2 {
		t.Fatalf("D-BIG generation = %d, want 2", gen.Generation)
	}
	if r := c.Snapshot().Residual["shared"]; r != 3 {
		t.Fatalf("residual = %d, want 3 (D-BIG holds 9 of 12)", r)
	}

	// D-BIG 归还后，FIFO 同轮依次放行 D-SMALL、D-TINY（各持 3，剩余 6 >= 2）。
	if _, err := c.Exit("D-BIG"); err != nil {
		t.Fatalf("exit D-BIG: %v", err)
	}
	r2, err := c.ReceiveReceipt(Receipt{DrillID: "D-BIG", Generation: 2, Kind: ReceiptHealthy})
	if err != nil || r2.Outcome != OutcomeAccepted {
		t.Fatalf("receipt: %+v err=%v", r2, err)
	}
	assertStatus(t, c, "D-SMALL", StatusActive)
	assertStatus(t, c, "D-TINY", StatusActive)
	if r := c.Snapshot().Residual["shared"]; r != 6 {
		t.Fatalf("residual = %d, want 6 (two small drills hold 6 of 12)", r)
	}
	if len(c.Snapshot().Waiting) != 0 {
		t.Fatalf("waiting queue should be drained: %+v", c.Snapshot().Waiting)
	}
}

// TestFaultWithWaitersAndRepeatedRevocation 核对故障发生时存在等待者的交错：
// 撤回后 FIFO 放行等待者；连续两次故障都能各自选出最少撤回集并留下完整审计。
func TestFaultWithWaitersAndRepeatedRevocation(t *testing.T) {
	c := mustNew(t, sharedDownstreamTopology(24, 2, 2))

	// 四个 ACTIVE，各持 2（共 8），第五个进入等待。
	ids := []string{"D1", "D2", "D3", "D4"}
	insts := []string{"a1", "a2", "b1", "b2"}
	for i, id := range ids {
		s, err := c.Submit(Drill{ID: id, Instances: []string{insts[i]}, Duration: time.Hour, Priority: i + 1})
		if err != nil {
			t.Fatalf("submit %s: %v", id, err)
		}
		if s.Status != StatusActive {
			t.Fatalf("%s should be active, got %s", id, s.Status)
		}
	}
	// D5 与 D1 共享实例 a1：实例互斥导致等待（容量本身也接近边界）。
	if s, err := c.Submit(Drill{ID: "D5", Instances: []string{"a1"}, Duration: time.Hour, Priority: 9}); err != nil {
		t.Fatalf("submit D5: %v", err)
	} else if s.Status != StatusWaiting {
		t.Fatalf("D5 should wait on instance conflict, got %s", s.Status)
	}

	// 故障削减 21：剩余 24-21-8 = -5 < 2。撤回一个释放 2 仍 -3 < 2；
	// 撤回两个释放 4 -> -1 < 2；撤回三个释放 6 -> 1 < 2；撤回四个 -> 3 >= 2。
	// 即必须撤回全部四个 ACTIVE；定序按 (pri,id)：D1(p1),D2(p2),D3(p3),D4(p4)。
	res, err := c.InjectExternalFault(ExternalFault{ID: "F1", DependencyID: "shared", Reduction: 21})
	if err != nil {
		t.Fatalf("fault F1: %v", err)
	}
	if !res.Recovered {
		t.Fatalf("F1 infeasible: %+v", res.Infeasibility)
	}
	if fmt.Sprint(res.Revoked) != "[D1 D2 D3 D4]" {
		t.Fatalf("revoked = %v, want [D1 D2 D3 D4]", res.Revoked)
	}
	// 全部撤回后剩余 = 24-21 = 3 >= 2；D5 尝试准入：持 2 后剩余 1 < 2，仍须等待。
	assertStatus(t, c, "D5", StatusWaiting)

	// 第二次故障再削减 1：无 ACTIVE 可撤回，剩余 = 24-22 = 2 >= 2，恰好不跌破。
	res2, err := c.InjectExternalFault(ExternalFault{ID: "F2", DependencyID: "shared", Reduction: 1})
	if err != nil {
		t.Fatalf("fault F2: %v", err)
	}
	if !res2.Recovered || len(res2.Revoked) != 0 {
		t.Fatalf("F2 should hold without revocation: %+v", res2)
	}

	// 审计记录必须覆盖每次撤回与两次故障。
	kinds := map[string]int{}
	for _, e := range c.AuditLog() {
		kinds[e.Kind]++
	}
	if kinds["FAULT_REVOKED"] != 4 || kinds["FAULT_APPLIED"] != 2 {
		t.Fatalf("audit counts wrong: %+v", kinds)
	}
}
