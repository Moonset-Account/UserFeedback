package permit

import (
	"errors"
	"testing"
	"time"
)

// ---- 测试夹具 ---------------------------------------------------------------

type fakeClock struct {
	now time.Time
}

func (f *fakeClock) Now() time.Time { return f.now }
func (f *fakeClock) set(t time.Time) {
	f.now = t
}

var baseTime = time.Date(2026, 11, 19, 8, 0, 0, 0, time.UTC)

func at(offset time.Duration) time.Time { return baseTime.Add(offset) }

const (
	personA PersonID = "alice"
	personB PersonID = "bob"
	areaX   AreaID   = "zone-x"
	areaY   AreaID   = "zone-y"
)

func grant(p PersonID, a AreaID, start, end time.Time) Grant {
	return Grant{Person: p, Area: a, NotBefore: start, ExpiresAt: end}
}

func mustIngestSnapshot(t *testing.T, e *Engine, s Snapshot) {
	t.Helper()
	if err := e.IngestSnapshot(s); err != nil {
		t.Fatalf("IngestSnapshot(seq=%d) unexpected error: %v", s.Seq, err)
	}
}

func assertDenied(t *testing.T, d Decision, wantReason string) {
	t.Helper()
	if d.Allowed {
		t.Fatalf("expected denial with reason %q, got allowed decision", wantReason)
	}
	if d.Reason != wantReason {
		t.Fatalf("expected denial reason %q, got %q", wantReason, d.Reason)
	}
}

func assertAllowed(t *testing.T, d Decision) {
	t.Helper()
	if !d.Allowed {
		t.Fatalf("expected allowed decision, got denied with reason %q", d.Reason)
	}
}

// ---- 可接受结果 -------------------------------------------------------------

func TestValidGrantAllowsLogin(t *testing.T) {
	clk := &fakeClock{now: at(time.Hour)}
	e := NewEngine(clk)
	mustIngestSnapshot(t, e, Snapshot{
		Seq: 1,
		At:  at(0),
		Grants: []Grant{
			grant(personA, areaX, at(0), at(10*time.Hour)),
		},
	})

	assertAllowed(t, e.Login(personA, areaX))
}

func TestDisconnectedTerminalWorksUntilDeadline(t *testing.T) {
	clk := &fakeClock{now: at(time.Hour)}
	e := NewEngine(clk)
	mustIngestSnapshot(t, e, Snapshot{
		Seq:    1,
		At:     at(0),
		Grants: []Grant{grant(personA, areaX, at(0), at(10*time.Hour))},
	})

	// 断线期间不再接收任何上游数据，只推进本地时钟：期限内持续可用。
	clk.set(at(5 * time.Hour))
	assertAllowed(t, e.Login(personA, areaX))
	clk.set(at(10*time.Hour - time.Second))
	assertAllowed(t, e.Login(personA, areaX))

	// 到达期限（终点为排他边界）即拒绝。
	clk.set(at(10 * time.Hour))
	assertDenied(t, e.Login(personA, areaX), ReasonExpired)

	// 断线期间排队到达的撤销记录，无需新快照也能立即阻断后续登录。
	clk.set(at(6 * time.Hour))
	e2 := NewEngine(clk)
	mustIngestSnapshot(t, e2, Snapshot{
		Seq:    1,
		Grants: []Grant{grant(personA, areaX, at(0), at(10*time.Hour))},
	})
	if err := e2.IngestRevocation(Revocation{Seq: 2, Person: personA, Area: areaX}); err != nil {
		t.Fatalf("IngestRevocation unexpected error: %v", err)
	}
	assertDenied(t, e2.Login(personA, areaX), ReasonRevoked)
}

// ---- 应被阻止/拒绝的结果 -----------------------------------------------------

func TestNoSnapshotAndNoGrant(t *testing.T) {
	clk := &fakeClock{now: at(time.Hour)}
	e := NewEngine(clk)

	// 从未收到可信快照：拒绝。
	assertDenied(t, e.Login(personA, areaX), ReasonNoSnapshot)

	mustIngestSnapshot(t, e, Snapshot{
		Seq:    1,
		Grants: []Grant{grant(personA, areaX, at(0), at(10*time.Hour))},
	})

	// 快照未覆盖的人员/工区组合：必须明确授权，拒绝。
	assertDenied(t, e.Login(personA, areaY), ReasonNoGrant)
	assertDenied(t, e.Login(personB, areaX), ReasonNoGrant)
}

func TestRevocationBlocksLogin(t *testing.T) {
	clk := &fakeClock{now: at(time.Hour)}
	e := NewEngine(clk)
	mustIngestSnapshot(t, e, Snapshot{
		Seq:    1,
		Grants: []Grant{grant(personA, areaX, at(0), at(10*time.Hour))},
	})
	if err := e.IngestRevocation(Revocation{Seq: 2, Person: personA, Area: areaX}); err != nil {
		t.Fatalf("IngestRevocation unexpected error: %v", err)
	}

	assertDenied(t, e.Login(personA, areaX), ReasonRevoked)
	if got := e.RevocationSeq(personA, areaX); got != 2 {
		t.Fatalf("expected stored revocation seq 2, got %d", got)
	}
}

func TestPersonWideRevocationBlocksAllAreas(t *testing.T) {
	clk := &fakeClock{now: at(time.Hour)}
	e := NewEngine(clk)
	mustIngestSnapshot(t, e, Snapshot{
		Seq: 1,
		Grants: []Grant{
			grant(personA, areaX, at(0), at(10*time.Hour)),
			grant(personA, areaY, at(0), at(10*time.Hour)),
		},
	})
	// 空工区 = 人员级撤销。
	if err := e.IngestRevocation(Revocation{Seq: 5, Person: personA}); err != nil {
		t.Fatalf("IngestRevocation unexpected error: %v", err)
	}

	assertDenied(t, e.Login(personA, areaX), ReasonRevoked)
	assertDenied(t, e.Login(personA, areaY), ReasonRevoked)
	// 其他人员不受影响。
	mustIngestSnapshot(t, e, Snapshot{
		Seq: 6,
		Grants: []Grant{
			grant(personB, areaX, at(0), at(10*time.Hour)),
		},
	})
	assertAllowed(t, e.Login(personB, areaX))
}

func TestSameSequenceTieGoesToRevocation(t *testing.T) {
	clk := &fakeClock{now: at(time.Hour)}
	e := NewEngine(clk)
	mustIngestSnapshot(t, e, Snapshot{
		Seq:    5,
		Grants: []Grant{grant(personA, areaX, at(0), at(10*time.Hour))},
	})
	// 撤销编号与快照编号相同：保守按撤销优先，拒绝。
	if err := e.IngestRevocation(Revocation{Seq: 5, Person: personA, Area: areaX}); err != nil {
		t.Fatalf("IngestRevocation unexpected error: %v", err)
	}
	assertDenied(t, e.Login(personA, areaX), ReasonRevoked)
}

// ---- 重新授权与旧快照/旧撤销的交错约束 ---------------------------------------

func TestOnlyExplicitRegrantInNewerSnapshotRestores(t *testing.T) {
	clk := &fakeClock{now: at(time.Hour)}
	e := NewEngine(clk)
	mustIngestSnapshot(t, e, Snapshot{
		Seq:    1,
		Grants: []Grant{grant(personA, areaX, at(0), at(10*time.Hour))},
	})
	if err := e.IngestRevocation(Revocation{Seq: 3, Person: personA, Area: areaX}); err != nil {
		t.Fatalf("IngestRevocation unexpected error: %v", err)
	}
	assertDenied(t, e.Login(personA, areaX), ReasonRevoked)

	// 新快照没有重新授权 alice/zone-x：即使编号更新，仍拒绝。
	mustIngestSnapshot(t, e, Snapshot{
		Seq: 4,
		Grants: []Grant{
			grant(personB, areaX, at(0), at(10*time.Hour)),
		},
	})
	assertDenied(t, e.Login(personA, areaX), ReasonNoGrant)

	// 编号严格大于撤销编号的新快照中明确重新授权：恢复登录。
	mustIngestSnapshot(t, e, Snapshot{
		Seq: 5,
		Grants: []Grant{
			grant(personA, areaX, at(0), at(10*time.Hour)),
			grant(personB, areaX, at(0), at(10*time.Hour)),
		},
	})
	assertAllowed(t, e.Login(personA, areaX))
}

func TestStaleSnapshotDoesNotChangeKnownState(t *testing.T) {
	clk := &fakeClock{now: at(time.Hour)}
	e := NewEngine(clk)
	mustIngestSnapshot(t, e, Snapshot{
		Seq:    2,
		Grants: []Grant{grant(personA, areaX, at(0), at(10*time.Hour))},
	})
	if err := e.IngestRevocation(Revocation{Seq: 4, Person: personA, Area: areaX}); err != nil {
		t.Fatalf("IngestRevocation unexpected error: %v", err)
	}
	assertDenied(t, e.Login(personA, areaX), ReasonRevoked)

	// 更旧的快照（seq=1）试图带回授权：必须被拒绝，且撤销结果不变。
	err := e.IngestSnapshot(Snapshot{
		Seq:    1,
		Grants: []Grant{grant(personA, areaX, at(0), at(10*time.Hour))},
	})
	if !errors.Is(err, ErrStaleSnapshot) {
		t.Fatalf("expected ErrStaleSnapshot, got %v", err)
	}
	if e.LatestSnapshotSeq() != 2 {
		t.Fatalf("latest snapshot seq changed to %d, want 2", e.LatestSnapshotSeq())
	}
	assertDenied(t, e.Login(personA, areaX), ReasonRevoked)

	// 相同编号同样拒绝。
	err = e.IngestSnapshot(Snapshot{Seq: 2})
	if !errors.Is(err, ErrStaleSnapshot) {
		t.Fatalf("expected ErrStaleSnapshot for equal seq, got %v", err)
	}
}

func TestOlderRevocationRecordsAreIgnored(t *testing.T) {
	clk := &fakeClock{now: at(time.Hour)}
	e := NewEngine(clk)
	mustIngestSnapshot(t, e, Snapshot{
		Seq:    1,
		Grants: []Grant{grant(personA, areaX, at(0), at(10*time.Hour))},
	})

	// 乱序到达：先收到新撤销（seq 8），后收到旧撤销（seq 5）。
	if err := e.IngestRevocation(Revocation{Seq: 8, Person: personA, Area: areaX}); err != nil {
		t.Fatalf("IngestRevocation unexpected error: %v", err)
	}
	if err := e.IngestRevocation(Revocation{Seq: 5, Person: personA, Area: areaX}); err != nil {
		t.Fatalf("old revocation should be ignored without error, got %v", err)
	}
	if got := e.RevocationSeq(personA, areaX); got != 8 {
		t.Fatalf("old revocation overwrote state: seq=%d, want 8", got)
	}

	// 新快照 seq=9 明确重新授权（严格大于 8）：恢复；旧撤销 5 不能再阻断它。
	mustIngestSnapshot(t, e, Snapshot{
		Seq:    9,
		Grants: []Grant{grant(personA, areaX, at(0), at(10*time.Hour))},
	})
	assertAllowed(t, e.Login(personA, areaX))
}

// ---- 时钟回拨：不得延长已知有效期 --------------------------------------------

func TestClockRollbackCannotReviveExpiredGrant(t *testing.T) {
	clk := &fakeClock{now: at(2 * time.Hour)}
	e := NewEngine(clk)
	mustIngestSnapshot(t, e, Snapshot{
		Seq:    1,
		Grants: []Grant{grant(personA, areaX, at(0), at(10*time.Hour))},
	})

	// 正常推进到过期。
	clk.set(at(11 * time.Hour))
	assertDenied(t, e.Login(personA, areaX), ReasonExpired)
	if !e.Watermark().Equal(at(11 * time.Hour)) {
		t.Fatalf("watermark = %v, want %v", e.Watermark(), at(11*time.Hour))
	}

	// 时钟被回拨到窗口内：水位不变，许可不能复活。
	clk.set(at(3 * time.Hour))
	d := e.Login(personA, areaX)
	assertDenied(t, d, ReasonExpired)
	if !e.Watermark().Equal(at(11 * time.Hour)) {
		t.Fatalf("rollback lowered watermark to %v", e.Watermark())
	}
}

func TestClockRollbackDoesNotChangeWindowSemantics(t *testing.T) {
	clk := &fakeClock{now: at(6 * time.Hour)}
	e := NewEngine(clk)
	mustIngestSnapshot(t, e, Snapshot{
		Seq: 1,
		Grants: []Grant{
			grant(personA, areaX, at(4*time.Hour), at(10*time.Hour)),
		},
	})
	assertAllowed(t, e.Login(personA, areaX))

	// 回拨到 NotBefore 之前：水位仍为 6h，授权依旧有效（不能倒带成“未生效”）。
	clk.set(at(2 * time.Hour))
	assertAllowed(t, e.Login(personA, areaX))
	if !e.Watermark().Equal(at(6 * time.Hour)) {
		t.Fatalf("watermark changed after rollback: %v", e.Watermark())
	}
}

func TestNotYetValidAndExpirationBoundary(t *testing.T) {
	clk := &fakeClock{now: at(2 * time.Hour)}
	e := NewEngine(clk)
	mustIngestSnapshot(t, e, Snapshot{
		Seq: 1,
		Grants: []Grant{
			grant(personA, areaX, at(4*time.Hour), at(10*time.Hour)),
		},
	})

	// 窗口尚未开始。
	assertDenied(t, e.Login(personA, areaX), ReasonNotYetValid)

	// 起点为包含边界。
	clk.set(at(4 * time.Hour))
	assertAllowed(t, e.Login(personA, areaX))

	// 终点前一秒有效，终点时刻失效。
	clk.set(at(10*time.Hour - time.Second))
	assertAllowed(t, e.Login(personA, areaX))
	clk.set(at(10 * time.Hour))
	assertDenied(t, e.Login(personA, areaX), ReasonExpired)
}

// ---- 输入校验与防御性拷贝 ----------------------------------------------------

func TestInvalidInputs(t *testing.T) {
	e := NewEngine(&fakeClock{now: at(time.Hour)})

	if !errors.Is(e.IngestSnapshot(Snapshot{Seq: 0}), ErrInvalidSnapshot) {
		t.Fatal("seq=0 snapshot should be invalid")
	}
	if !errors.Is(e.IngestSnapshot(Snapshot{Seq: -1}), ErrInvalidSnapshot) {
		t.Fatal("negative seq snapshot should be invalid")
	}
	if !errors.Is(e.IngestSnapshot(Snapshot{
		Seq:    1,
		Grants: []Grant{{Person: "", Area: areaX, NotBefore: at(0), ExpiresAt: at(time.Hour)}},
	}), ErrInvalidSnapshot) {
		t.Fatal("grant with empty person should be invalid")
	}
	if !errors.Is(e.IngestSnapshot(Snapshot{
		Seq:    1,
		Grants: []Grant{grant(personA, areaX, at(2*time.Hour), at(time.Hour))},
	}), ErrInvalidSnapshot) {
		t.Fatal("grant with ExpiresAt <= NotBefore should be invalid")
	}

	if !errors.Is(e.IngestRevocation(Revocation{Seq: 0, Person: personA}), ErrInvalidRevocation) {
		t.Fatal("revocation seq=0 should be invalid")
	}
	if !errors.Is(e.IngestRevocation(Revocation{Seq: 1, Person: ""}), ErrInvalidRevocation) {
		t.Fatal("revocation with empty person should be invalid")
	}

	// 非法快照不得建立“已知最新快照”。
	if e.LatestSnapshotSeq() != 0 {
		t.Fatalf("invalid snapshot mutated state, latest seq=%d", e.LatestSnapshotSeq())
	}
	assertDenied(t, e.Login("", areaX), ReasonEmptyPerson)
}

func TestDefensiveCopyOfSnapshot(t *testing.T) {
	clk := &fakeClock{now: at(time.Hour)}
	e := NewEngine(clk)
	in := Snapshot{
		Seq:    1,
		Grants: []Grant{grant(personA, areaX, at(0), at(10*time.Hour))},
	}
	mustIngestSnapshot(t, e, in)

	// 调用方在摄入后篡改原始切片，引擎内部状态不受影响。
	in.Grants[0].Person = personB
	in.Grants[0].ExpiresAt = at(2 * time.Hour)
	assertAllowed(t, e.Login(personA, areaX))

	clk.set(at(9 * time.Hour))
	assertAllowed(t, e.Login(personA, areaX))
}

func TestNilClockFallsBackToWallClock(t *testing.T) {
	e := NewEngine(nil)
	// 仅做冒烟：无快照时应稳定拒绝而不是 panic。
	assertDenied(t, e.Login(personA, areaX), ReasonNoSnapshot)
}
