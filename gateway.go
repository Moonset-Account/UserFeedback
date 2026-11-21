// Package gateway 实现按逻辑时间驱动的网关放行队列。
//
// 每个请求同时消耗一个租户桶和一个路由桶；每个桶有容量和每毫秒补充量。
// 放行时一次性扣齐两个桶的额度；等待与取消均不预扣。
// 较早未放行的请求会挡住与它共享任一桶的后来者，完全独立桶的请求可先走。
// 同一时刻按入队号（提交顺序）裁决；单笔成本超过任一相关桶容量时立即退回。
package gateway

import (
	"errors"
	"fmt"
)

var (
	// ErrCostExceedsCapacity 表示单笔成本超过任一相关桶容量，请求被立即退回。
	ErrCostExceedsCapacity = errors.New("gateway: request cost exceeds bucket capacity")
	// ErrInvalidCost 表示成本不是正整数。
	ErrInvalidCost = errors.New("gateway: cost must be a positive integer")
	// ErrUnknownTenant 表示租户桶未注册。
	ErrUnknownTenant = errors.New("gateway: unknown tenant bucket")
	// ErrUnknownRoute 表示路由桶未注册。
	ErrUnknownRoute = errors.New("gateway: unknown route bucket")
	// ErrTimeRegression 表示逻辑时间倒退。
	ErrTimeRegression = errors.New("gateway: logical time cannot move backwards")
)

// BucketSpec 描述一个令牌桶：容量与每毫秒补充量。
type BucketSpec struct {
	Capacity    int64
	RefillPerMs int64
}

// Status 是请求的生命周期状态。
type Status int

const (
	// Pending 表示请求仍在队列中等待放行。
	Pending Status = iota
	// Admitted 表示请求已被放行（额度已在放行时刻一次性扣齐）。
	Admitted
	// Cancelled 表示请求在等待期间被取消（从未扣减额度）。
	Cancelled
)

func (s Status) String() string {
	switch s {
	case Pending:
		return "Pending"
	case Admitted:
		return "Admitted"
	case Cancelled:
		return "Cancelled"
	default:
		return "Unknown"
	}
}

// Request 是进入放行队列的一次申请。
type Request struct {
	id         int64
	tenant     string
	route      string
	cost       int64
	enqueuedAt int64
	status     Status
	admittedAt int64
}

// ID 返回入队号，按提交顺序从 1 单调递增。
func (r *Request) ID() int64 { return r.id }

// Tenant 返回请求消耗的租户桶名。
func (r *Request) Tenant() string { return r.tenant }

// Route 返回请求消耗的路由桶名。
func (r *Request) Route() string { return r.route }

// Cost 返回请求的整数成本。
func (r *Request) Cost() int64 { return r.cost }

// EnqueuedAt 返回请求入队的逻辑时刻。
func (r *Request) EnqueuedAt() int64 { return r.enqueuedAt }

// Status 返回请求当前状态。
func (r *Request) Status() Status { return r.status }

// AdmittedAt 返回放行时刻；未放行时 ok 为 false。
func (r *Request) AdmittedAt() (at int64, ok bool) {
	if r.status == Admitted {
		return r.admittedAt, true
	}
	return 0, false
}

// bucket 是令牌桶的运行时状态，补充量按逻辑时间惰性结算。
type bucket struct {
	spec       BucketSpec
	tokens     int64
	lastRefill int64
}

// Gateway 是按逻辑时间驱动的放行队列。
type Gateway struct {
	now     int64
	tenants map[string]*bucket
	routes  map[string]*bucket
	queue   []*Request
	nextID  int64
}

// NewGateway 用给定的租户桶与路由桶规格创建网关。
// 所有桶在逻辑时刻 0 以满容量启动。
func NewGateway(tenants, routes map[string]BucketSpec) (*Gateway, error) {
	g := &Gateway{
		tenants: make(map[string]*bucket, len(tenants)),
		routes:  make(map[string]*bucket, len(routes)),
		nextID:  1,
	}
	for name, spec := range tenants {
		if err := validateSpec(spec); err != nil {
			return nil, fmt.Errorf("tenant bucket %q: %w", name, err)
		}
		g.tenants[name] = &bucket{spec: spec, tokens: spec.Capacity}
	}
	for name, spec := range routes {
		if err := validateSpec(spec); err != nil {
			return nil, fmt.Errorf("route bucket %q: %w", name, err)
		}
		g.routes[name] = &bucket{spec: spec, tokens: spec.Capacity}
	}
	return g, nil
}

func validateSpec(spec BucketSpec) error {
	if spec.Capacity < 1 {
		return errors.New("capacity must be at least 1")
	}
	if spec.RefillPerMs < 0 {
		return errors.New("refill per millisecond cannot be negative")
	}
	return nil
}

// Now 返回网关当前逻辑时刻。
func (g *Gateway) Now() int64 { return g.now }

// Submit 在逻辑时刻 at 提交一次申请。
//
// 成本超过任一相关桶容量时立即退回（ErrCostExceedsCapacity），请求不入队。
// 否则请求入队并立即触发一轮放行判断；返回的 *Request 可查询状态与放行时刻。
func (g *Gateway) Submit(at int64, tenant, route string, cost int64) (*Request, error) {
	if err := g.advance(at); err != nil {
		return nil, err
	}
	if cost <= 0 {
		return nil, ErrInvalidCost
	}
	tb, ok := g.tenants[tenant]
	if !ok {
		return nil, fmt.Errorf("%w: %s", ErrUnknownTenant, tenant)
	}
	rb, ok := g.routes[route]
	if !ok {
		return nil, fmt.Errorf("%w: %s", ErrUnknownRoute, route)
	}
	if cost > tb.spec.Capacity || cost > rb.spec.Capacity {
		return nil, ErrCostExceedsCapacity
	}
	r := &Request{
		id:         g.nextID,
		tenant:     tenant,
		route:      route,
		cost:       cost,
		enqueuedAt: at,
		status:     Pending,
	}
	g.nextID++
	g.queue = append(g.queue, r)
	g.process()
	return r, nil
}

// Cancel 在逻辑时刻 at 取消一个仍在等待的请求。
// 取消不预扣额度；队首（或任意等待者）取消后会重新判断谁可开始。
// 返回是否找到并取消了等待中的请求。
func (g *Gateway) Cancel(at int64, id int64) (bool, error) {
	if err := g.advance(at); err != nil {
		return false, err
	}
	for i, r := range g.queue {
		if r.id == id {
			r.status = Cancelled
			g.queue = append(g.queue[:i], g.queue[i+1:]...)
			g.process()
			return true, nil
		}
	}
	return false, nil
}

// AdvanceTo 把逻辑时间推进到 at，并重新判断队列中谁可开始
// （例如等待独立桶补满的阻塞请求）。
func (g *Gateway) AdvanceTo(at int64) error {
	if err := g.advance(at); err != nil {
		return err
	}
	g.process()
	return nil
}

// Pending 返回当前仍在等待的请求数。
func (g *Gateway) Pending() int { return len(g.queue) }

// TenantAvailable 返回租户桶在当前逻辑时刻的可用额度。
func (g *Gateway) TenantAvailable(name string) (int64, bool) {
	b, ok := g.tenants[name]
	if !ok {
		return 0, false
	}
	g.refill(b)
	return b.tokens, true
}

// RouteAvailable 返回路由桶在当前逻辑时刻的可用额度。
func (g *Gateway) RouteAvailable(name string) (int64, bool) {
	b, ok := g.routes[name]
	if !ok {
		return 0, false
	}
	g.refill(b)
	return b.tokens, true
}

func (g *Gateway) advance(at int64) error {
	if at < g.now {
		return fmt.Errorf("%w: now=%d, got=%d", ErrTimeRegression, g.now, at)
	}
	g.now = at
	return nil
}

// refill 把桶的额度惰性结算到当前逻辑时刻，封顶于容量。
func (g *Gateway) refill(b *bucket) {
	if g.now <= b.lastRefill {
		return
	}
	b.tokens += (g.now - b.lastRefill) * b.spec.RefillPerMs
	if b.tokens > b.spec.Capacity {
		b.tokens = b.spec.Capacity
	}
	b.lastRefill = g.now
}

// process 按入队号顺序扫描等待队列：
// 被更早未放行请求共享任一桶的后来者跳过；
// 其余请求在两个桶额度都足够时一次性扣齐并放行，
// 否则它自己也成为新的阻塞源。
func (g *Gateway) process() {
	blocked := make(map[*bucket]bool)
	kept := g.queue[:0]
	for _, r := range g.queue {
		tb := g.tenants[r.tenant]
		rb := g.routes[r.route]
		if blocked[tb] || blocked[rb] {
			kept = append(kept, r)
			continue
		}
		g.refill(tb)
		g.refill(rb)
		if tb.tokens >= r.cost && rb.tokens >= r.cost {
			tb.tokens -= r.cost
			rb.tokens -= r.cost
			r.status = Admitted
			r.admittedAt = g.now
			continue
		}
		blocked[tb] = true
		blocked[rb] = true
		kept = append(kept, r)
	}
	// 清空尾部，避免已放行请求被底层数组滞留。
	for i := len(kept); i < len(g.queue); i++ {
		g.queue[i] = nil
	}
	g.queue = kept
}
