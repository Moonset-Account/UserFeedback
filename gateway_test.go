package gateway

import (
	"errors"
	"testing"
)

func mustGateway(t *testing.T, tenants, routes map[string]BucketSpec) *Gateway {
	t.Helper()
	g, err := NewGateway(tenants, routes)
	if err != nil {
		t.Fatalf("NewGateway: %v", err)
	}
	return g
}

func mustSubmit(t *testing.T, g *Gateway, at int64, tenant, route string, cost int64) *Request {
	t.Helper()
	r, err := g.Submit(at, tenant, route, cost)
	if err != nil {
		t.Fatalf("Submit(t=%d, %s, %s, %d): %v", at, tenant, route, cost, err)
	}
	return r
}

func mustAdmittedAt(t *testing.T, r *Request, want int64) {
	t.Helper()
	at, ok := r.AdmittedAt()
	if !ok {
		t.Fatalf("request %d: expected admitted at %d, still %s", r.ID(), want, r.Status())
	}
	if at != want {
		t.Fatalf("request %d: admitted at %d, want %d", r.ID(), at, want)
	}
}

func mustPending(t *testing.T, r *Request) {
	t.Helper()
	if r.Status() != Pending {
		t.Fatalf("request %d: status %s, want Pending", r.ID(), r.Status())
	}
	if _, ok := r.AdmittedAt(); ok {
		t.Fatalf("request %d: pending request must not report an admission time", r.ID())
	}
}

// 可接受结果：两个桶额度都足够时立即放行，一次扣齐，放行时刻可查。
func TestImmediateAdmissionDeductsBothBuckets(t *testing.T) {
	g := mustGateway(t,
		map[string]BucketSpec{"acme": {Capacity: 10, RefillPerMs: 1}},
		map[string]BucketSpec{"read": {Capacity: 5, RefillPerMs: 1}},
	)

	r := mustSubmit(t, g, 0, "acme", "read", 4)
	mustAdmittedAt(t, r, 0)

	if got, _ := g.TenantAvailable("acme"); got != 6 {
		t.Fatalf("tenant tokens = %d, want 6", got)
	}
	if got, _ := g.RouteAvailable("read"); got != 1 {
		t.Fatalf("route tokens = %d, want 1", got)
	}
	if g.Pending() != 0 {
		t.Fatalf("pending = %d, want 0", g.Pending())
	}
}

// 应被拒绝的结果：单笔成本超过任一相关桶容量，立即退回且不入队。
func TestOversizedCostRejectedImmediately(t *testing.T) {
	g := mustGateway(t,
		map[string]BucketSpec{"acme": {Capacity: 10, RefillPerMs: 1}},
		map[string]BucketSpec{"read": {Capacity: 5, RefillPerMs: 1}},
	)

	// 超过路由桶容量（5），即使租户桶容量（10）足够也必须退回。
	r, err := g.Submit(0, "acme", "read", 6)
	if !errors.Is(err, ErrCostExceedsCapacity) {
		t.Fatalf("err = %v, want ErrCostExceedsCapacity", err)
	}
	if r != nil {
		t.Fatalf("rejected request must not be enqueued, got id=%d", r.ID())
	}
	if g.Pending() != 0 {
		t.Fatalf("pending = %d, want 0 after immediate rejection", g.Pending())
	}
	// 立即退回不得扣减任何额度。
	if got, _ := g.TenantAvailable("acme"); got != 10 {
		t.Fatalf("tenant tokens = %d, want 10 (untouched)", got)
	}
	if got, _ := g.RouteAvailable("read"); got != 5 {
		t.Fatalf("route tokens = %d, want 5 (untouched)", got)
	}
}

// 边界：成本恰好等于两个桶容量时可放行；超过任一桶容量 1 即被退回。
func TestCostExactlyAtCapacityBoundary(t *testing.T) {
	g := mustGateway(t,
		map[string]BucketSpec{"acme": {Capacity: 10, RefillPerMs: 0}},
		map[string]BucketSpec{"read": {Capacity: 7, RefillPerMs: 0}},
	)

	exact := mustSubmit(t, g, 0, "acme", "read", 7)
	mustAdmittedAt(t, exact, 0)
	if got, _ := g.RouteAvailable("read"); got != 0 {
		t.Fatalf("route tokens = %d, want 0 after exact-fit admission", got)
	}

	if _, err := g.Submit(0, "acme", "read", 8); !errors.Is(err, ErrCostExceedsCapacity) {
		t.Fatalf("cost 8 vs route capacity 7: err = %v, want ErrCostExceedsCapacity", err)
	}
	if _, err := g.Submit(0, "acme", "read", 11); !errors.Is(err, ErrCostExceedsCapacity) {
		t.Fatalf("cost 11 vs tenant capacity 10: err = %v, want ErrCostExceedsCapacity", err)
	}
}

// 交错场景：共享桶的队首阻塞、独立桶先行、等待不预扣、补满后重判。
func TestSharedBucketBlockingAndIndependentBypass(t *testing.T) {
	g := mustGateway(t,
		map[string]BucketSpec{
			"t1": {Capacity: 10, RefillPerMs: 1},
			"t2": {Capacity: 10, RefillPerMs: 1},
		},
		map[string]BucketSpec{
			"r1": {Capacity: 10, RefillPerMs: 1},
			"r2": {Capacity: 10, RefillPerMs: 1},
			"r3": {Capacity: 10, RefillPerMs: 1},
		},
	)

	a := mustSubmit(t, g, 0, "t1", "r1", 8)
	mustAdmittedAt(t, a, 0) // t1: 10-8=2

	b := mustSubmit(t, g, 0, "t1", "r2", 5)
	mustPending(t, b) // t1 只剩 2，不足 5

	// 等待不预扣：B 挂起期间 t1 仍是 2。
	if got, _ := g.TenantAvailable("t1"); got != 2 {
		t.Fatalf("tenant t1 tokens = %d while B waits, want 2 (no pre-deduction)", got)
	}

	// 完全独立桶的 C 不受 B 阻塞，同一时刻先走。
	c := mustSubmit(t, g, 0, "t2", "r3", 5)
	mustAdmittedAt(t, c, 0)

	// D 与 B 共享租户桶 t1：即使 2 的余额够 D 的成本 2，也被队首挡住。
	d := mustSubmit(t, g, 0, "t1", "r3", 2)
	mustPending(t, d)

	// 推进 3ms：t1 补到 5，B 恰好放行；D 仍因 t1 归零而等待。
	if err := g.AdvanceTo(3); err != nil {
		t.Fatalf("AdvanceTo(3): %v", err)
	}
	mustAdmittedAt(t, b, 3)
	mustPending(t, d)

	// 再推进 2ms：t1 补到 2，D 放行，放行时刻为当前逻辑时刻。
	if err := g.AdvanceTo(5); err != nil {
		t.Fatalf("AdvanceTo(5): %v", err)
	}
	mustAdmittedAt(t, d, 5)
	if g.Pending() != 0 {
		t.Fatalf("pending = %d, want 0", g.Pending())
	}
}

// 队首取消后重新判断：后来者立即获得放行机会，取消不预扣。
func TestCancelHeadReevaluatesQueue(t *testing.T) {
	g := mustGateway(t,
		map[string]BucketSpec{"t1": {Capacity: 10, RefillPerMs: 1}},
		map[string]BucketSpec{
			"r1": {Capacity: 10, RefillPerMs: 1},
			"r2": {Capacity: 10, RefillPerMs: 1},
		},
	)

	a := mustSubmit(t, g, 0, "t1", "r1", 9)
	mustAdmittedAt(t, a, 0) // t1: 10-9=1

	b := mustSubmit(t, g, 0, "t1", "r2", 5)
	mustPending(t, b)

	c := mustSubmit(t, g, 0, "t1", "r1", 3)
	mustPending(t, c) // 被共享 t1 的 B 挡住

	// t=2 时取消 B：t1 补到 3，C 立即放行于时刻 2。
	cancelled, err := g.Cancel(2, b.ID())
	if err != nil {
		t.Fatalf("Cancel: %v", err)
	}
	if !cancelled {
		t.Fatalf("Cancel(%d) = false, want true", b.ID())
	}
	if b.Status() != Cancelled {
		t.Fatalf("B status = %s, want Cancelled", b.Status())
	}
	if _, ok := b.AdmittedAt(); ok {
		t.Fatalf("cancelled request must not report an admission time")
	}
	mustAdmittedAt(t, c, 2)

	// 取消不预扣：t1 = 1 + 2ms*1 - 3(C) = 0。
	if got, _ := g.TenantAvailable("t1"); got != 0 {
		t.Fatalf("tenant t1 tokens = %d, want 0", got)
	}

	// 重复取消已不在队列中的请求返回 false。
	again, err := g.Cancel(2, b.ID())
	if err != nil {
		t.Fatalf("Cancel again: %v", err)
	}
	if again {
		t.Fatalf("second Cancel(%d) = true, want false", b.ID())
	}
}

// 补充量边界：差 1 毫秒不够，恰好补到成本的那一毫秒才放行。
func TestRefillBoundaryExactFit(t *testing.T) {
	g := mustGateway(t,
		map[string]BucketSpec{"t1": {Capacity: 10, RefillPerMs: 2}},
		map[string]BucketSpec{"r1": {Capacity: 10, RefillPerMs: 2}},
	)

	a := mustSubmit(t, g, 0, "t1", "r1", 10)
	mustAdmittedAt(t, a, 0) // 两桶清零

	b := mustSubmit(t, g, 0, "t1", "r1", 4)
	mustPending(t, b)

	if err := g.AdvanceTo(1); err != nil { // 补到 2，不够 4
		t.Fatalf("AdvanceTo(1): %v", err)
	}
	mustPending(t, b)

	if err := g.AdvanceTo(2); err != nil { // 补到 4，恰好够
		t.Fatalf("AdvanceTo(2): %v", err)
	}
	mustAdmittedAt(t, b, 2)
	if got, _ := g.TenantAvailable("t1"); got != 0 {
		t.Fatalf("tenant tokens = %d, want 0 after exact-fit admission", got)
	}
}

// 同一时刻多请求按入队号裁决：先入队者先扣，后来者被阻塞。
func TestSameTimeOrderingByEnqueueSequence(t *testing.T) {
	g := mustGateway(t,
		map[string]BucketSpec{"t1": {Capacity: 10, RefillPerMs: 0}},
		map[string]BucketSpec{
			"r1": {Capacity: 10, RefillPerMs: 0},
			"r2": {Capacity: 10, RefillPerMs: 0},
		},
	)

	first := mustSubmit(t, g, 4, "t1", "r1", 7)
	mustAdmittedAt(t, first, 4) // t1: 10-7=3

	second := mustSubmit(t, g, 4, "t1", "r2", 4)
	mustPending(t, second) // 同一时刻入队，t1 只剩 3，不够 4

	// 取消先入队者不会退回已扣额度，second 依旧等待。
	if _, err := g.Cancel(4, first.ID()); err != nil {
		t.Fatalf("Cancel: %v", err) // first 已放行，不在队列中
	}
	mustPending(t, second)
}

// 防御性校验：非法成本、未知桶、时间倒退。
func TestDefensiveValidation(t *testing.T) {
	g := mustGateway(t,
		map[string]BucketSpec{"t1": {Capacity: 10, RefillPerMs: 1}},
		map[string]BucketSpec{"r1": {Capacity: 10, RefillPerMs: 1}},
	)

	if _, err := g.Submit(0, "t1", "r1", 0); !errors.Is(err, ErrInvalidCost) {
		t.Fatalf("zero cost: err = %v, want ErrInvalidCost", err)
	}
	if _, err := g.Submit(0, "t1", "r1", -3); !errors.Is(err, ErrInvalidCost) {
		t.Fatalf("negative cost: err = %v, want ErrInvalidCost", err)
	}
	if _, err := g.Submit(0, "ghost", "r1", 1); !errors.Is(err, ErrUnknownTenant) {
		t.Fatalf("unknown tenant: err = %v, want ErrUnknownTenant", err)
	}
	if _, err := g.Submit(0, "t1", "ghost", 1); !errors.Is(err, ErrUnknownRoute) {
		t.Fatalf("unknown route: err = %v, want ErrUnknownRoute", err)
	}

	mustSubmit(t, g, 5, "t1", "r1", 1)
	if err := g.AdvanceTo(4); !errors.Is(err, ErrTimeRegression) {
		t.Fatalf("AdvanceTo(4) after now=5: err = %v, want ErrTimeRegression", err)
	}
	if _, err := g.Submit(3, "t1", "r1", 1); !errors.Is(err, ErrTimeRegression) {
		t.Fatalf("Submit at t=3 after now=5: err = %v, want ErrTimeRegression", err)
	}
	if _, err := g.Cancel(2, 1); !errors.Is(err, ErrTimeRegression) {
		t.Fatalf("Cancel at t=2 after now=5: err = %v, want ErrTimeRegression", err)
	}
}

// 非法桶规格在构造期被拒绝。
func TestInvalidBucketSpec(t *testing.T) {
	if _, err := NewGateway(map[string]BucketSpec{"t": {Capacity: 0, RefillPerMs: 1}}, nil); err == nil {
		t.Fatalf("zero capacity tenant bucket must be rejected")
	}
	if _, err := NewGateway(nil, map[string]BucketSpec{"r": {Capacity: 5, RefillPerMs: -1}}); err == nil {
		t.Fatalf("negative refill route bucket must be rejected")
	}
}
