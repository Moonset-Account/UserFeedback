package cookiejar

import "time"

// Clock 抽象时间来源，生产环境使用 SystemClock，测试使用 FakeClock
// 以确定性地验证有效期、访问时间与逐出顺序。
type Clock interface {
	Now() time.Time
}

// SystemClock 直接返回系统当前时间（UTC）。
type SystemClock struct{}

// Now 实现 Clock。
func (SystemClock) Now() time.Time { return time.Now().UTC() }

// FakeClock 是单调推进的可控时钟，时间统一使用 UTC，避免时区与夏令时干扰。
type FakeClock struct {
	now time.Time
}

// NewFakeClock 以 t 为起点构造可控时钟（内部转成 UTC）。
func NewFakeClock(t time.Time) *FakeClock {
	return &FakeClock{now: t.UTC()}
}

// Now 实现 Clock。
func (c *FakeClock) Now() time.Time { return c.now }

// Advance 将时钟向后推进 d。
func (c *FakeClock) Advance(d time.Duration) { c.now = c.now.Add(d) }

// Set 直接设置时钟时刻（内部转成 UTC）。
func (c *FakeClock) Set(t time.Time) { c.now = t.UTC() }
