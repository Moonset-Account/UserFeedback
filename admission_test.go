package admission

import (
	"testing"
	"time"
)

var t0 = time.Date(2026, 11, 27, 12, 0, 0, 0, time.UTC)

func newTestController() *Controller {
	return New(Config{
		KeepLabels:   []string{"job", "route"},
		TeamBudget:   map[string]int{"alpha": 2},
		MetricBudget: map[string]int{"http_requests": 2},
		StaleAfter:   10 * time.Minute,
		LateAfter:    5 * time.Minute,
	})
}

// 正常场景：清洗后标签组相同的两条样本共用一条序列，
// 不在保留名单里的标签不影响身份。
func TestCleanedLabelsShareSeries(t *testing.T) {
	c := newTestController()
	res := c.Admit([]Sample{
		{Team: "alpha", Metric: "http_requests", Labels: map[string]string{"job": "api", "route": "/a", "instance": "i-1"}, Time: t0},
		{Team: "alpha", Metric: "http_requests", Labels: map[string]string{"job": "api", "route": "/a", "instance": "i-2"}, Time: t0},
	}, t0)

	if len(res.Results) != 2 {
		t.Fatalf("want 2 results, got %d", len(res.Results))
	}
	for i, r := range res.Results {
		if r.Decision != Accepted {
			t.Fatalf("result %d: want accepted, got %v", i, r.Decision)
		}
	}
	if res.Results[0].Identity != res.Results[1].Identity {
		t.Fatalf("cleaned samples should share identity: %q vs %q", res.Results[0].Identity, res.Results[1].Identity)
	}
	if len(res.NewIdentities) != 1 {
		t.Fatalf("want 1 new identity, got %d", len(res.NewIdentities))
	}
	if !res.Results[0].NewIdentity || res.Results[1].NewIdentity {
		t.Fatalf("only the first sample should create the identity")
	}
	if got := c.ActiveCount(); got != 1 {
		t.Fatalf("want 1 active series, got %d", got)
	}
}

// 旧名与已登记的新名共用序列；未登记的新名属于新身份，受预算限制。
func TestAliasSharesWithOldName(t *testing.T) {
	c := newTestController()
	if err := c.RegisterAlias("http_requests", "http_reqs"); err != nil {
		t.Fatalf("register alias: %v", err)
	}
	labels := map[string]string{"job": "api", "route": "/a"}

	first := c.Admit([]Sample{{Team: "alpha", Metric: "http_requests", Labels: labels, Time: t0}}, t0)
	second := c.Admit([]Sample{{Team: "alpha", Metric: "http_reqs", Labels: labels, Time: t0}}, t0.Add(time.Minute))

	if first.Results[0].Identity != second.Results[0].Identity {
		t.Fatalf("alias should share identity with old name")
	}
	if len(second.NewIdentities) != 0 {
		t.Fatalf("alias sample must not create a new identity")
	}
	if got := c.MetricActive("http_reqs"); got != 1 {
		t.Fatalf("metric active via alias: want 1, got %d", got)
	}

	// 未登记的新名是全新身份。
	third := c.Admit([]Sample{{Team: "alpha", Metric: "http_requests_v2", Labels: labels, Time: t0}}, t0.Add(2*time.Minute))
	if len(third.NewIdentities) != 1 {
		t.Fatalf("unregistered new name should create a new identity")
	}
}

// 别名冲突：同一新名不能登记到不同的规范化名。
func TestRegisterAliasConflict(t *testing.T) {
	c := newTestController()
	if err := c.RegisterAlias("http_requests", "reqs"); err != nil {
		t.Fatalf("first register: %v", err)
	}
	if err := c.RegisterAlias("other_metric", "reqs"); err == nil {
		t.Fatalf("conflicting alias registration should fail")
	}
	if err := c.RegisterAlias("http_requests", "reqs"); err != nil {
		t.Fatalf("idempotent re-register should succeed: %v", err)
	}
}

// 团队预算边界：恰好用满可以，再新增就被该预算项卡住。
func TestTeamBudgetBoundary(t *testing.T) {
	c := newTestController() // alpha 上限 2
	mk := func(route string) Sample {
		return Sample{Team: "alpha", Metric: "other_metric", Labels: map[string]string{"job": "api", "route": route}, Time: t0}
	}
	res := c.Admit([]Sample{mk("/a"), mk("/b"), mk("/c")}, t0)

	if res.Results[0].Decision != Accepted || res.Results[1].Decision != Accepted {
		t.Fatalf("first two identities should be accepted: %v %v", res.Results[0].Decision, res.Results[1].Decision)
	}
	third := res.Results[2]
	if third.Decision != Rejected {
		t.Fatalf("third identity should be rejected, got %v", third.Decision)
	}
	if len(third.BlockedBy) != 1 || third.BlockedBy[0].Kind != "team" || third.BlockedBy[0].Key != "alpha" {
		t.Fatalf("blocked by wrong budget item: %+v", third.BlockedBy)
	}
	if third.BlockedBy[0].Limit != 2 || third.BlockedBy[0].Used != 2 {
		t.Fatalf("budget ref should report 2/2, got %+v", third.BlockedBy[0])
	}
	if len(res.Blocked) != 1 {
		t.Fatalf("batch should report 1 blocked budget item, got %d", len(res.Blocked))
	}
	if got := c.TeamActive("alpha"); got != 2 {
		t.Fatalf("alpha active: want 2, got %d", got)
	}
}

// 指标预算边界：按规范化名计数，超限的新身份被卡住。
func TestMetricBudgetBoundary(t *testing.T) {
	c := New(Config{
		KeepLabels:   []string{"job"},
		MetricBudget: map[string]int{"cpu_usage": 1},
		StaleAfter:   10 * time.Minute,
		LateAfter:    5 * time.Minute,
	})
	res := c.Admit([]Sample{
		{Team: "t1", Metric: "cpu_usage", Labels: map[string]string{"job": "a"}, Time: t0},
		{Team: "t2", Metric: "cpu_usage", Labels: map[string]string{"job": "b"}, Time: t0},
	}, t0)

	if res.Results[0].Decision != Accepted {
		t.Fatalf("first identity should be accepted")
	}
	if res.Results[1].Decision != Rejected {
		t.Fatalf("second identity exceeds metric budget, want rejected, got %v", res.Results[1].Decision)
	}
	b := res.Results[1].BlockedBy
	if len(b) != 1 || b[0].Kind != "metric" || b[0].Key != "cpu_usage" || b[0].Limit != 1 {
		t.Fatalf("blocked by wrong budget item: %+v", b)
	}
}

// 序列退出边界：恰好 StaleAfter 未见新鲜样本仍活跃；严格超过才退出。
func TestStalenessBoundary(t *testing.T) {
	c := newTestController()
	labels := map[string]string{"job": "api", "route": "/a"}
	c.Admit([]Sample{{Team: "alpha", Metric: "other_metric", Labels: labels, Time: t0}}, t0)

	// 恰好 10 分钟：仍活跃，共用序列，不是新身份。
	atBoundary := c.Admit([]Sample{{Team: "alpha", Metric: "other_metric", Labels: labels, Time: t0}}, t0.Add(10*time.Minute))
	if len(atBoundary.NewIdentities) != 0 || c.ActiveCount() != 1 {
		t.Fatalf("series should still be active exactly at StaleAfter")
	}

	// 超过 10 分钟（相对上次确认时刻 t0+10m）：序列退出，同一身份重新计为新增。
	fresh := t0.Add(10*time.Minute + 10*time.Minute + time.Second)
	past := c.Admit([]Sample{{Team: "alpha", Metric: "other_metric", Labels: labels, Time: fresh}}, fresh)
	if len(past.NewIdentities) != 1 {
		t.Fatalf("expired series should be re-created as a new identity")
	}
	if got := c.ActiveCount(); got != 1 {
		t.Fatalf("active count should stay 1 after re-creation, got %d", got)
	}
}

// 序列退出后预算被释放，新身份可以再次进入。
func TestBudgetFreedAfterExpiry(t *testing.T) {
	c := New(Config{
		KeepLabels: []string{"job"},
		TeamBudget: map[string]int{"alpha": 1},
		StaleAfter: 10 * time.Minute,
		LateAfter:  5 * time.Minute,
	})
	c.Admit([]Sample{{Team: "alpha", Metric: "m", Labels: map[string]string{"job": "a"}, Time: t0}}, t0)

	// 未退出前，第二个身份被卡住。
	blocked := c.Admit([]Sample{{Team: "alpha", Metric: "m", Labels: map[string]string{"job": "b"}, Time: t0}}, t0.Add(time.Minute))
	if blocked.Results[0].Decision != Rejected {
		t.Fatalf("want rejected while budget full, got %v", blocked.Results[0].Decision)
	}

	// 旧序列退出后，新身份获得预算。
	later := t0.Add(11 * time.Minute)
	free := c.Admit([]Sample{{Team: "alpha", Metric: "m", Labels: map[string]string{"job": "b"}, Time: later}}, later)
	if free.Results[0].Decision != Accepted || len(free.NewIdentities) != 1 {
		t.Fatalf("budget should be freed after expiry: %+v", free.Results[0])
	}
	if got := c.TeamActive("alpha"); got != 1 {
		t.Fatalf("alpha active: want 1, got %d", got)
	}
}

// 迟到边界：恰好 LateAfter 不算迟到；严格超过记为历史写入，
// 不创建身份、不占预算、当前活跃数保持不变。
func TestLateSampleBoundary(t *testing.T) {
	c := New(Config{
		KeepLabels: []string{"job"},
		TeamBudget: map[string]int{"alpha": 0}, // 任何新身份都会被卡
		StaleAfter: 10 * time.Minute,
		LateAfter:  5 * time.Minute,
	})
	confirmed := t0.Add(time.Hour)

	// 恰好迟到阈值：按新鲜样本处理（此处因预算为 0 被拒，但绝不是历史写入）。
	atBoundary := c.Admit([]Sample{{Team: "alpha", Metric: "m", Labels: map[string]string{"job": "a"}, Time: confirmed.Add(-5 * time.Minute)}}, confirmed)
	if atBoundary.Results[0].Decision != Rejected {
		t.Fatalf("sample exactly at LateAfter should be fresh (rejected by budget here), got %v", atBoundary.Results[0].Decision)
	}

	// 严格超过阈值：历史写入，不占用预算，活跃数不变。
	late := c.Admit([]Sample{{Team: "alpha", Metric: "m", Labels: map[string]string{"job": "a"}, Time: confirmed.Add(-5*time.Minute - time.Second)}}, confirmed)
	r := late.Results[0]
	if r.Decision != Historical {
		t.Fatalf("obviously late sample should be historical, got %v", r.Decision)
	}
	if len(late.NewIdentities) != 0 || len(late.Blocked) != 0 {
		t.Fatalf("historical write must not create identities or touch budgets")
	}
	if got := c.ActiveCount(); got != 0 {
		t.Fatalf("active count must stay 0, got %d", got)
	}
	if late.Historical != 1 || c.HistoricalWrites() != 1 {
		t.Fatalf("historical counters wrong: batch=%d total=%d", late.Historical, c.HistoricalWrites())
	}
}

// 迟到样本不刷新已有序列的保活时间，序列仍按原计划退出。
func TestLateSampleDoesNotRefreshSeries(t *testing.T) {
	c := newTestController()
	labels := map[string]string{"job": "api", "route": "/a"}
	c.Admit([]Sample{{Team: "alpha", Metric: "other_metric", Labels: labels, Time: t0}}, t0)

	// t0+9m 收到一条样本时刻为 t0-1m 的明显迟到样本：记历史，不续命。
	confirmed := t0.Add(9 * time.Minute)
	late := c.Admit([]Sample{{Team: "alpha", Metric: "other_metric", Labels: labels, Time: t0.Add(-time.Minute)}}, confirmed)
	if late.Results[0].Decision != Historical {
		t.Fatalf("want historical, got %v", late.Results[0].Decision)
	}

	// t0+10m+1s：序列照样退出（上次新鲜确认仍是 t0）。
	after := c.Admit(nil, t0.Add(10*time.Minute+time.Second))
	_ = after
	if got := c.ActiveCount(); got != 0 {
		t.Fatalf("series should have expired on schedule, active=%d", got)
	}
}

// 交错批次：同一批内混合已有身份、新身份、超预算身份、别名样本与迟到样本，
// 且批内先准入的身份立即可被后续样本共用。
func TestInterleavedBatch(t *testing.T) {
	c := New(Config{
		KeepLabels:   []string{"job", "route"},
		TeamBudget:   map[string]int{"alpha": 2, "beta": 1},
		MetricBudget: map[string]int{"http_requests": 4},
		StaleAfter:   10 * time.Minute,
		LateAfter:    5 * time.Minute,
	})
	if err := c.RegisterAlias("http_requests", "http_reqs"); err != nil {
		t.Fatalf("register alias: %v", err)
	}

	// 预置一条 alpha 的活跃序列。
	seed := Sample{Team: "alpha", Metric: "http_requests", Labels: map[string]string{"job": "api", "route": "/a"}, Time: t0}
	c.Admit([]Sample{seed}, t0)

	confirmed := t0.Add(time.Minute)
	batch := []Sample{
		seed, // 0: 已有身份，接受
		{Team: "alpha", Metric: "http_reqs", Labels: map[string]string{"job": "api", "route": "/b"}, Time: t0},                    // 1: 别名新身份，接受（alpha 2/2）
		{Team: "alpha", Metric: "http_requests", Labels: map[string]string{"job": "api", "route": "/c"}, Time: t0},                // 2: alpha 团队预算已满，拒绝
		{Team: "beta", Metric: "http_requests", Labels: map[string]string{"job": "api", "route": "/a"}, Time: t0},                 // 3: beta 新身份，接受
		{Team: "beta", Metric: "http_requests", Labels: map[string]string{"job": "api", "route": "/b"}, Time: t0},                 // 4: beta 团队预算满，拒绝
		{Team: "beta", Metric: "http_requests", Labels: map[string]string{"job": "api", "route": "/c"}, Time: t0.Add(-time.Hour)}, // 5: 明显迟到，历史写入
		{Team: "alpha", Metric: "http_reqs", Labels: map[string]string{"job": "api", "route": "/b", "instance": "i-9"}, Time: t0}, // 6: 与 1 清洗后相同，共用
	}
	res := c.Admit(batch, confirmed)

	want := []Decision{Accepted, Accepted, Rejected, Accepted, Rejected, Historical, Accepted}
	for i, w := range want {
		if res.Results[i].Decision != w {
			t.Fatalf("sample %d: want %v, got %v", i, w, res.Results[i].Decision)
		}
	}

	// 新增身份只有 1 号与 3 号。
	if len(res.NewIdentities) != 2 {
		t.Fatalf("want 2 new identities, got %d", len(res.NewIdentities))
	}
	// 6 号与 1 号共用身份（多余标签 instance 被清洗，别名解析一致）。
	if res.Results[6].Identity != res.Results[1].Identity {
		t.Fatalf("sample 6 should share identity with sample 1")
	}
	// 被卡住的预算项：alpha 团队与 beta 团队各一项，去重后共 2 项。
	if len(res.Blocked) != 2 {
		t.Fatalf("want 2 blocked budget items, got %d: %v", len(res.Blocked), res.Blocked)
	}
	kinds := map[string]bool{}
	for _, b := range res.Blocked {
		kinds[b.Kind+":"+b.Key] = true
	}
	if !kinds["team:alpha"] || !kinds["team:beta"] {
		t.Fatalf("blocked items should cover team alpha and beta: %v", res.Blocked)
	}
	// 历史写入计数。
	if res.Historical != 1 {
		t.Fatalf("want 1 historical write, got %d", res.Historical)
	}
	// 活跃数：alpha 2 条 + beta 1 条；迟到的 5 号不占数。
	if got := c.ActiveCount(); got != 3 {
		t.Fatalf("want 3 active series, got %d", got)
	}
	if got := c.MetricActive("http_requests"); got != 3 {
		t.Fatalf("metric active: want 3, got %d", got)
	}
}

// 未配置预算的团队/指标不受限。
func TestUnconfiguredBudgetIsUnlimited(t *testing.T) {
	c := New(Config{
		KeepLabels: []string{"job"},
		StaleAfter: 10 * time.Minute,
		LateAfter:  5 * time.Minute,
	})
	var samples []Sample
	for i := 0; i < 50; i++ {
		samples = append(samples, Sample{
			Team:   "free",
			Metric: "any",
			Labels: map[string]string{"job": string(rune('a'+i%26)) + string(rune('a'+i/26))},
			Time:   t0,
		})
	}
	res := c.Admit(samples, t0)
	if len(res.NewIdentities) != 50 || len(res.Blocked) != 0 {
		t.Fatalf("unconfigured budgets must not block: new=%d blocked=%v", len(res.NewIdentities), res.Blocked)
	}
}
