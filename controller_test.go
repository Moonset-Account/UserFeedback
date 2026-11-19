package mantrap

import (
	"errors"
	"sync"
	"testing"
)

// ---- 小工具 ----

func mustRegister(t *testing.T, c *Controller, card string, z Zone) {
	t.Helper()
	if err := c.RegisterCard(card, z); err != nil {
		t.Fatalf("RegisterCard(%q,%q): %v", card, z, err)
	}
}

func assertZone(t *testing.T, c *Controller, card string, want Zone) {
	t.Helper()
	got, err := c.CardZone(card)
	if err != nil {
		t.Fatalf("CardZone(%q): %v", card, err)
	}
	if got != want {
		t.Fatalf("card %q zone = %q, want %q", card, got, want)
	}
}

func assertUsed(t *testing.T, c *Controller, z Zone, want int) {
	t.Helper()
	got, err := c.ZoneUsed(z)
	if err != nil {
		t.Fatalf("ZoneUsed(%q): %v", z, err)
	}
	if got != want {
		t.Fatalf("zone %q used = %d, want %d (位置与名额不一致)", z, got, want)
	}
}

func assertLocked(t *testing.T, c *Controller, d Door, want bool) {
	t.Helper()
	got, err := c.DoorLocked(d)
	if err != nil {
		t.Fatalf("DoorLocked(%q): %v", d, err)
	}
	if got != want {
		t.Fatalf("door %q locked = %v, want %v", d, got, want)
	}
}

func assertErrIs(t *testing.T, err error, target error) {
	t.Helper()
	if !errors.Is(err, target) {
		t.Fatalf("err = %v, want errors.Is(..., %v)", err, target)
	}
}

func inwardReceipt(s Snapshot) Receipt {
	return Receipt{Card: s.Card, Door: s.Door, Direction: s.Direction}
}

// 场景 1（可接受结果）：单卡完整走通 外区 -> 中间区 -> 内区，
// 每一步的位置、名额、互锁状态都要一致。
func TestFullJourneyOuterToInner(t *testing.T) {
	c := New()
	mustRegister(t, c, "A", Outer)

	// 外区 -> 中间区
	s1, err := c.Request("A", Inward)
	if err != nil {
		t.Fatalf("Request#1: %v", err)
	}
	if s1.From != Outer || s1.To != Middle || s1.Door != OuterDoor {
		t.Fatalf("snapshot#1 = %+v", s1)
	}
	if s1.State != StateReleased {
		t.Fatalf("state = %v, want released", s1.State)
	}
	// 放行即暂占中间区名额；凭证位置尚未改变。
	assertZone(t, c, "A", Outer)
	assertUsed(t, c, Middle, 1)
	assertLocked(t, c, OuterDoor, false)
	assertLocked(t, c, InnerDoor, true)

	if err := c.NotifyDoorOpened(OuterDoor); err != nil {
		t.Fatalf("NotifyDoorOpened: %v", err)
	}
	s1, err = c.RoundSnapshot(s1.ID)
	if err != nil {
		t.Fatalf("RoundSnapshot: %v", err)
	}
	if s1.State != StatePendingVerify {
		t.Fatalf("state = %v, want pending", s1.State)
	}
	// 门已开但传感器未确认：凭证与名额保持待核对，位置不变。
	assertZone(t, c, "A", Outer)
	assertUsed(t, c, Middle, 1)

	// 有方向的过门回执到达：位置此刻才改变。
	s1, err = c.SubmitReceipt(s1.ID, inwardReceipt(s1))
	if err != nil {
		t.Fatalf("SubmitReceipt#1: %v", err)
	}
	if s1.State != StateCompleted {
		t.Fatalf("state = %v, want completed", s1.State)
	}
	assertZone(t, c, "A", Middle)
	assertUsed(t, c, Middle, 1) // 暂占名额转为实际占用，总量不变
	assertLocked(t, c, OuterDoor, true)
	assertLocked(t, c, InnerDoor, true)

	// 中间区 -> 内区
	s2, err := c.Request("A", Inward)
	if err != nil {
		t.Fatalf("Request#2: %v", err)
	}
	if s2.Door != InnerDoor || s2.From != Middle || s2.To != Inner {
		t.Fatalf("snapshot#2 = %+v", s2)
	}
	// 中间区实际占用仍为 A 本人（请求不改变位置，内区不限容量）。
	assertUsed(t, c, Middle, 1)
	if err := c.NotifyDoorOpened(InnerDoor); err != nil {
		t.Fatalf("NotifyDoorOpened#2: %v", err)
	}
	if _, err := c.SubmitReceipt(s2.ID, inwardReceipt(s2)); err != nil {
		t.Fatalf("SubmitReceipt#2: %v", err)
	}
	assertZone(t, c, "A", Inner)
	assertUsed(t, c, Middle, 0) // A 离开后中间区彻底空出
}

// 场景 2（被阻止）：凭证只能从登记所在区发起；未登记凭证、
// 与所在区不匹配的方向都必须被拒绝，且不产生任何名额占用。
func TestCredentialRequestRestrictions(t *testing.T) {
	c := New()
	mustRegister(t, c, "A", Outer)

	if _, err := c.Request("ghost", Inward); !errors.Is(err, ErrUnknownCard) {
		t.Fatalf("unknown card err = %v, want ErrUnknownCard", err)
	}
	// 外区凭证只能向内；向外没有合法过门。
	if _, err := c.Request("A", Outward); !errors.Is(err, ErrBadDirection) {
		t.Fatalf("bad direction err = %v, want ErrBadDirection", err)
	}
	assertUsed(t, c, Middle, 0)
	assertLocked(t, c, OuterDoor, true)
}

// 场景 3（被阻止，题面指定：容量为一的中间区，两张卡同时请求）。
// 第一张卡放行并暂占唯一名额后，第二张卡的请求必须被拒绝；
// 即使第一张已完成进入中间区，名额仍被其实际占用，直到它离开；
// 纯互锁（名额够但门在用）的独立验证见场景 9。
func TestTwoCardsCapacityOneInterlock(t *testing.T) {
	c := New()
	mustRegister(t, c, "A", Outer)
	mustRegister(t, c, "B", Outer)

	sA, err := c.Request("A", Inward) // 外->中，暂占唯一名额
	if err != nil {
		t.Fatalf("A request: %v", err)
	}
	assertUsed(t, c, Middle, 1)

	// B 同时请求：容量约束优先判定 -> ErrZoneFull（互锁此时也成立）。
	if _, err := c.Request("B", Inward); !errors.Is(err, ErrZoneFull) {
		t.Fatalf("B request err = %v, want ErrZoneFull", err)
	}
	// 门打开进入待核对后，B 仍被同一容量约束挡住，拒绝原因保持确定。
	if err := c.NotifyDoorOpened(OuterDoor); err != nil {
		t.Fatalf("open: %v", err)
	}
	if _, err := c.Request("B", Inward); !errors.Is(err, ErrZoneFull) {
		t.Fatalf("B request while pending err = %v, want ErrZoneFull", err)
	}
	// 被拒绝的请求没有留下任何痕迹。
	assertUsed(t, c, Middle, 1)
	assertZone(t, c, "B", Outer)

	// A 完成进入中间区：暂占名额转为实际占用，B 依然进不来。
	if _, err := c.SubmitReceipt(sA.ID, inwardReceipt(sA)); err != nil {
		t.Fatalf("A receipt: %v", err)
	}
	assertZone(t, c, "A", Middle)
	assertUsed(t, c, Middle, 1)
	if _, err := c.Request("B", Inward); !errors.Is(err, ErrZoneFull) {
		t.Fatalf("B request with A in middle err = %v, want ErrZoneFull", err)
	}

	// A 继续进入内区，中间区彻底空出，B 才能成功请求。
	sA2, err := c.Request("A", Inward)
	if err != nil {
		t.Fatalf("A request#2: %v", err)
	}
	if err := c.NotifyDoorOpened(InnerDoor); err != nil {
		t.Fatalf("A open#2: %v", err)
	}
	if _, err := c.SubmitReceipt(sA2.ID, inwardReceipt(sA2)); err != nil {
		t.Fatalf("A receipt#2: %v", err)
	}
	assertZone(t, c, "A", Inner)
	assertUsed(t, c, Middle, 0)

	sB, err := c.Request("B", Inward)
	if err != nil {
		t.Fatalf("B retry: %v", err)
	}
	if sB.From != Outer || sB.To != Middle || sB.Door != OuterDoor {
		t.Fatalf("B snapshot = %+v", sB)
	}
}

// 场景 4（交错 + 并发）：两张外区卡同时请求进入容量为一的中间区，
// 并发执行多轮，每一轮恰好一个成功、一个被拒，且名额守恒。
func TestConcurrentRequestsExactlyOneWins(t *testing.T) {
	const iterations = 200
	for i := 0; i < iterations; i++ {
		c := New()
		mustRegister(t, c, "A", Outer)
		mustRegister(t, c, "B", Outer)

		var wg sync.WaitGroup
		errs := make([]error, 2)
		snaps := make([]Snapshot, 2)
		start := make(chan struct{})
		wg.Add(2)
		go func() {
			defer wg.Done()
			<-start
			snaps[0], errs[0] = c.Request("A", Inward)
		}()
		go func() {
			defer wg.Done()
			<-start
			snaps[1], errs[1] = c.Request("B", Inward)
		}()
		close(start)
		wg.Wait()

		ok, rejected := 0, 0
		for _, err := range errs {
			switch {
			case err == nil:
				ok++
			case errors.Is(err, ErrZoneFull) || errors.Is(err, ErrDoorInterlocked):
				rejected++
			default:
				t.Fatalf("iteration %d: unexpected err %v", i, err)
			}
		}
		if ok != 1 || rejected != 1 {
			t.Fatalf("iteration %d: ok=%d rejected=%d, want exactly 1 each", i, ok, rejected)
		}
		// 无论谁赢，中间区只暂占一个名额，两卡位置都还在外区。
		assertUsed(t, c, Middle, 1)
		assertZone(t, c, "A", Outer)
		assertZone(t, c, "B", Outer)
		_ = snaps
	}
}

// 场景 5（边界）：放行后超时未开门 -> 归还名额、解除互锁，
// 另一张卡可以立即使用中间区；超时轮次的迟来回执必须被拒绝。
func TestTimeoutReleasesSlotAndStaleReceiptRejected(t *testing.T) {
	c := New()
	mustRegister(t, c, "A", Outer)
	mustRegister(t, c, "B", Outer)

	sA, err := c.Request("A", Inward)
	if err != nil {
		t.Fatalf("A request: %v", err)
	}
	assertUsed(t, c, Middle, 1)

	if err := c.NotifyDoorTimeout(OuterDoor); err != nil {
		t.Fatalf("timeout: %v", err)
	}
	sA, _ = c.RoundSnapshot(sA.ID)
	if sA.State != StateTimedOut {
		t.Fatalf("state = %v, want timed_out", sA.State)
	}
	assertUsed(t, c, Middle, 0) // 名额归还
	assertZone(t, c, "A", Outer)
	assertLocked(t, c, OuterDoor, true)

	// 超时事件重送必须被拒绝：该门已无活动轮次。
	if err := c.NotifyDoorTimeout(OuterDoor); !errors.Is(err, ErrUnknownDoor) {
		t.Fatalf("duplicate timeout err = %v, want ErrUnknownDoor", err)
	}
	// 上一轮（A 的超时轮）回执不能替新一轮过门，也不能复活旧轮。
	if _, err := c.SubmitReceipt(sA.ID, inwardReceipt(sA)); !errors.Is(err, ErrStaleReceipt) {
		t.Fatalf("stale receipt after timeout err = %v, want ErrStaleReceipt", err)
	}

	// B 立刻可以请求并成功。
	sB, err := c.Request("B", Inward)
	if err != nil {
		t.Fatalf("B request after timeout: %v", err)
	}
	assertUsed(t, c, Middle, 1)
	// B 的门打开之后再报超时属于非法状态迁移。
	if err := c.NotifyDoorOpened(OuterDoor); err != nil {
		t.Fatalf("B open: %v", err)
	}
	if err := c.NotifyDoorTimeout(OuterDoor); !errors.Is(err, ErrRoundState) {
		t.Fatalf("timeout after open err = %v, want ErrRoundState", err)
	}
	_ = sB
}

// 场景 6（题面指定：管理员纠正有疑义的通行）。
// 门已开、关门已确认但传感器没确认是否通过：凭证与名额保持待核对；
// 管理员撤销后，位置与空余名额必须一起恢复一致，互锁解除。
func TestAdminCancelRestoresPositionAndSlot(t *testing.T) {
	t.Run("capacity_one_cancel_restores_consistency", func(t *testing.T) {
		c := New()
		mustRegister(t, c, "A", Outer)
		mustRegister(t, c, "B", Outer)

		sA, err := c.Request("A", Inward)
		if err != nil {
			t.Fatalf("A request: %v", err)
		}
		if err := c.NotifyDoorOpened(OuterDoor); err != nil {
			t.Fatalf("open: %v", err)
		}
		// 关门确认，但没有过门回执。
		if err := c.NotifyDoorClosed(OuterDoor); err != nil {
			t.Fatalf("closed: %v", err)
		}
		sA, _ = c.RoundSnapshot(sA.ID)
		if sA.State != StatePendingVerify {
			t.Fatalf("state = %v, want pending", sA.State)
		}
		// 待核对期间 B 不能请求：容量为一，容量约束优先判定。
		if _, err := c.Request("B", Inward); !errors.Is(err, ErrZoneFull) {
			t.Fatalf("B during pending err = %v, want ErrZoneFull", err)
		}
		assertZone(t, c, "A", Outer)
		assertUsed(t, c, Middle, 1)

		// 管理员判定有疑义，撤销本轮。
		snap, err := c.AdminResolve(sA.ID, false)
		if err != nil {
			t.Fatalf("admin cancel: %v", err)
		}
		if snap.State != StateCancelled {
			t.Fatalf("state = %v, want cancelled", snap.State)
		}
		assertZone(t, c, "A", Outer) // 位置恢复：A 仍在外区
		assertUsed(t, c, Middle, 0)  // 名额恢复：中间区全空
		assertLocked(t, c, OuterDoor, true)
		assertLocked(t, c, InnerDoor, true)

		// 撤销后旧回执失效，B 可以正常使用唯一名额。
		if _, err := c.SubmitReceipt(sA.ID, inwardReceipt(sA)); !errors.Is(err, ErrStaleReceipt) {
			t.Fatalf("receipt after cancel err = %v, want ErrStaleReceipt", err)
		}
		sB, err := c.Request("B", Inward)
		if err != nil {
			t.Fatalf("B after admin cancel: %v", err)
		}
		if err := c.NotifyDoorOpened(OuterDoor); err != nil {
			t.Fatalf("B open: %v", err)
		}
		if _, err := c.AdminResolve(sB.ID, true); err != nil {
			t.Fatalf("admin confirm: %v", err)
		}
		assertZone(t, c, "B", Middle)
		assertUsed(t, c, Middle, 1)
	})

	t.Run("interlock_holds_after_door_closed_pending", func(t *testing.T) {
		// 容量放宽到 2：名额充足，单独检验互锁在关门待核对期间不提前解除。
		c := NewWithCapacity(map[Zone]int{Middle: 2})
		mustRegister(t, c, "A", Outer)
		mustRegister(t, c, "B", Inner)

		sA, err := c.Request("A", Inward)
		if err != nil {
			t.Fatalf("A request: %v", err)
		}
		if err := c.NotifyDoorOpened(OuterDoor); err != nil {
			t.Fatalf("open: %v", err)
		}
		if err := c.NotifyDoorClosed(OuterDoor); err != nil {
			t.Fatalf("closed: %v", err)
		}
		// 物理门已锁，但轮次仍待核对：中间区还有空位，B 仍必须被互锁挡住。
		assertLocked(t, c, OuterDoor, true)
		if _, err := c.Request("B", Outward); !errors.Is(err, ErrDoorInterlocked) {
			t.Fatalf("B after closed-pending err = %v, want ErrDoorInterlocked", err)
		}
		// 管理员撤销后互锁才真正解除，B 可放行（容量 2 足够）。
		if _, err := c.AdminResolve(sA.ID, false); err != nil {
			t.Fatalf("admin cancel: %v", err)
		}
		if _, err := c.Request("B", Outward); err != nil {
			t.Fatalf("B after cancel: %v", err)
		}
	})
}

// 场景 7：相同回执可重送（幂等）；错门/错方向/错卡的回执被拒绝；
// 新一轮的请求不会被上一轮的回执顶替。
func TestReceiptIdempotencyAndMismatch(t *testing.T) {
	c := New()
	mustRegister(t, c, "A", Outer)

	s1, err := c.Request("A", Inward)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	if err := c.NotifyDoorOpened(OuterDoor); err != nil {
		t.Fatalf("open: %v", err)
	}
	good := inwardReceipt(s1)

	// 要素不匹配的回执一律拒绝，状态不得改变。
	bad := good
	bad.Door = InnerDoor
	if _, err := c.SubmitReceipt(s1.ID, bad); !errors.Is(err, ErrReceiptMismatch) {
		t.Fatalf("wrong door receipt err = %v, want ErrReceiptMismatch", err)
	}
	bad = good
	bad.Direction = Outward
	if _, err := c.SubmitReceipt(s1.ID, bad); !errors.Is(err, ErrReceiptMismatch) {
		t.Fatalf("wrong direction receipt err = %v, want ErrReceiptMismatch", err)
	}
	bad = good
	bad.Card = "X"
	if _, err := c.SubmitReceipt(s1.ID, bad); !errors.Is(err, ErrReceiptMismatch) {
		t.Fatalf("wrong card receipt err = %v, want ErrReceiptMismatch", err)
	}
	assertZone(t, c, "A", Outer)
	assertUsed(t, c, Middle, 1)

	// 正确回执完成通行。
	if _, err := c.SubmitReceipt(s1.ID, good); err != nil {
		t.Fatalf("first receipt: %v", err)
	}
	assertZone(t, c, "A", Middle)
	// 相同回执重送：幂等成功，位置/名额不发生第二次变化。
	snap, err := c.SubmitReceipt(s1.ID, good)
	if err != nil {
		t.Fatalf("duplicate receipt: %v", err)
	}
	if snap.State != StateCompleted {
		t.Fatalf("state = %v, want completed", snap.State)
	}
	assertZone(t, c, "A", Middle)
	assertUsed(t, c, Middle, 1)

	// A 进入中间区后发起新一轮（中->内）；上一轮的回执不能替新一轮过门。
	s2, err := c.Request("A", Inward)
	if err != nil {
		t.Fatalf("request#2: %v", err)
	}
	// 旧回执（外门/外->中）投递给新一轮（内门/中->内）：要素不匹配，拒绝。
	if _, err := c.SubmitReceipt(s2.ID, good); !errors.Is(err, ErrReceiptMismatch) {
		t.Fatalf("old receipt for new round err = %v, want ErrReceiptMismatch", err)
	}
	// 向已关闭的旧轮次重投其自身回执：幂等返回，不改变任何位置/名额。
	snap1, err := c.SubmitReceipt(s1.ID, good)
	if err != nil {
		t.Fatalf("idempotent replay on old round: %v", err)
	}
	if snap1.State != StateCompleted {
		t.Fatalf("old round state = %v, want completed", snap1.State)
	}
	assertZone(t, c, "A", Middle)
	assertUsed(t, c, Middle, 1)
	// 新轮次必须用与本轮匹配的回执。
	want2 := inwardReceipt(s2)
	if _, err := c.SubmitReceipt(s2.ID, want2); err != nil {
		t.Fatalf("new round receipt: %v", err)
	}
	assertZone(t, c, "A", Inner)
}

// 场景 8（边界）：关门确认后、管理员介入前，迟来的有效回执仍可完成通行；
// 此时再由管理员纠正必须被拒绝（轮次已完结）。
func TestLateReceiptAfterDoorClosed(t *testing.T) {
	c := New()
	mustRegister(t, c, "A", Outer)

	s, err := c.Request("A", Inward)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	if err := c.NotifyDoorOpened(OuterDoor); err != nil {
		t.Fatalf("open: %v", err)
	}
	if err := c.NotifyDoorClosed(OuterDoor); err != nil {
		t.Fatalf("closed: %v", err)
	}
	// 关门后仍处待核对。
	if s, _ := c.RoundSnapshot(s.ID); s.State != StatePendingVerify {
		t.Fatalf("state = %v, want pending", s.State)
	}
	// 迟来但要素匹配的回执：接受，位置与名额一次性对齐。
	if _, err := c.SubmitReceipt(s.ID, inwardReceipt(s)); err != nil {
		t.Fatalf("late receipt: %v", err)
	}
	assertZone(t, c, "A", Middle)
	assertUsed(t, c, Middle, 1)
	// 已完结轮次不再接受管理员纠正。
	if _, err := c.AdminResolve(s.ID, false); !errors.Is(err, ErrRoundState) {
		t.Fatalf("admin after completion err = %v, want ErrRoundState", err)
	}
}

// 场景 9（互锁独立于容量）：把中间区容量放宽到 2，制造一个
// “名额够、但另一扇门正在放行”的局面，验证互锁本身仍然生效。
func TestInterlockIndependentOfCapacity(t *testing.T) {
	c := NewWithCapacity(map[Zone]int{Middle: 2})
	mustRegister(t, c, "A", Outer)
	mustRegister(t, c, "B", Inner)

	// A 请求外->中：中间区容量 2，名额充足，外门放行。
	sA, err := c.Request("A", Inward)
	if err != nil {
		t.Fatalf("A request: %v", err)
	}
	// 此时 B 内->中 目标区仍有空位（used=1 < cap=2），但内门必须互锁。
	if _, err := c.Request("B", Outward); !errors.Is(err, ErrDoorInterlocked) {
		t.Fatalf("B err = %v, want ErrDoorInterlocked", err)
	}
	if err := c.NotifyDoorOpened(OuterDoor); err != nil {
		t.Fatalf("open: %v", err)
	}
	// 待核对期间互锁依旧。
	if _, err := c.Request("B", Outward); !errors.Is(err, ErrDoorInterlocked) {
		t.Fatalf("B pending err = %v, want ErrDoorInterlocked", err)
	}
	if _, err := c.SubmitReceipt(sA.ID, inwardReceipt(sA)); err != nil {
		t.Fatalf("A receipt: %v", err)
	}
	// A 完成、两扇门都回到锁定态后，B 才被放行；此时中间区有 A 一人，
	// 容量 2 仍可容纳 B。
	sB, err := c.Request("B", Outward)
	if err != nil {
		t.Fatalf("B request after A done: %v", err)
	}
	if sB.To != Middle {
		t.Fatalf("B target = %v, want middle", sB.To)
	}
	assertUsed(t, c, Middle, 2) // A 实际占用 + B 暂占
}

// 场景 10：同一凭证在上一轮未核对完成前不得发起新一轮（ErrCardBusy），
// 且整个过程区域名额守恒。
func TestSameCardCannotStartNewRoundBeforeResolved(t *testing.T) {
	c := New()
	mustRegister(t, c, "A", Outer)

	s, err := c.Request("A", Inward)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	// 门还没开，重复请求：卡忙（互锁也同时成立，卡忙优先判定）。
	if _, err := c.Request("A", Inward); !errors.Is(err, ErrCardBusy) {
		t.Fatalf("re-request released err = %v, want ErrCardBusy", err)
	}
	if err := c.NotifyDoorOpened(OuterDoor); err != nil {
		t.Fatalf("open: %v", err)
	}
	if _, err := c.Request("A", Inward); !errors.Is(err, ErrCardBusy) {
		t.Fatalf("re-request pending err = %v, want ErrCardBusy", err)
	}
	if _, err := c.SubmitReceipt(s.ID, inwardReceipt(s)); err != nil {
		t.Fatalf("receipt: %v", err)
	}
	// 完结后同卡可立即发起下一轮。
	if _, err := c.Request("A", Inward); err != nil {
		t.Fatalf("request after completion: %v", err)
	}
}
