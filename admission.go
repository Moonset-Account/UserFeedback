// Package admission 实现监控采集准入控制：在团队与单指标两个维度上
// 限制活跃时间序列的数量，对样本批次给出逐条接收结果。
//
// 核心语义：
//   - 序列身份 = (团队, 规范化指标名, 清洗后的标签组)。两条样本清洗后
//     标签组相同（且团队、规范化指标名相同）就共用一条序列。
//   - 指标旧名与通过 RegisterAlias 登记的新名解析为同一规范化名，
//     因此也共用序列；只有真正的新身份才占用预算。
//   - 序列在 StaleAfter 时间内未见到新鲜样本即退出，释放预算。
//   - 明显迟到的样本（确认时刻与样本时刻之差超过 LateAfter）记为
//     历史写入，不创建/刷新序列，不占用预算，当前活跃数保持不变。
package admission

import (
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"
)

// Config 是准入模块的静态配置。
type Config struct {
	// KeepLabels 是需要保留的标签名；其余标签在清洗时被丢弃。
	KeepLabels []string
	// TeamBudget 给出各团队允许的最大活跃序列数；未配置的团队不受限。
	TeamBudget map[string]int
	// MetricBudget 给出各规范化指标名允许的最大活跃序列数；未配置的指标不受限。
	MetricBudget map[string]int
	// StaleAfter 是序列的保活时长：确认时刻减去最近一次新鲜样本的
	// 确认时刻，严格大于该值时序列退出。
	StaleAfter time.Duration
	// LateAfter 是迟到阈值：确认时刻减去样本时刻，严格大于该值时
	// 样本被判定为明显迟到，记为历史写入。
	LateAfter time.Duration
}

// Decision 是单条样本的接收结果。
type Decision int

const (
	// Accepted 表示样本被接收并计入当前活跃序列。
	Accepted Decision = iota
	// Historical 表示样本明显迟到，被记为历史写入，不改变活跃状态。
	Historical
	// Rejected 表示样本因预算不足被拒绝。
	Rejected
)

func (d Decision) String() string {
	switch d {
	case Accepted:
		return "accepted"
	case Historical:
		return "historical"
	case Rejected:
		return "rejected"
	default:
		return "unknown"
	}
}

// Sample 是一条待准入的样本。
type Sample struct {
	Team   string            // 所属团队
	Metric string            // 指标名（可以是已登记的别名）
	Labels map[string]string // 原始标签，准入前按 KeepLabels 清洗
	Time   time.Time         // 样本时刻
}

// BudgetRef 描述一个被卡住的预算项。
type BudgetRef struct {
	Kind  string // "team" 或 "metric"
	Key   string // 团队名或规范化指标名
	Limit int    // 预算上限
	Used  int    // 做出决定时已占用的活跃数
}

func (b BudgetRef) String() string {
	return fmt.Sprintf("%s %q budget %d/%d", b.Kind, b.Key, b.Used, b.Limit)
}

// SampleResult 是单条样本的准入结果。
type SampleResult struct {
	Sample      Sample
	Decision    Decision
	Identity    string      // 解析后的序列身份键
	NewIdentity bool        // 该样本是否实际新增了身份
	BlockedBy   []BudgetRef // 拒绝时卡住它的预算项
}

// BatchResult 是一个批次的整体结果。
type BatchResult struct {
	Results       []SampleResult // 与输入样本一一对应
	NewIdentities []string       // 本批次实际新增的身份
	Blocked       []BudgetRef    // 本批次被卡住的预算项（去重）
	Historical    int            // 本批次记为历史写入的样本数
}

type seriesState struct {
	team     string
	metric   string // 规范化指标名
	lastSeen time.Time
}

// Controller 是采集准入模块。
type Controller struct {
	keep         map[string]bool
	teamBudget   map[string]int
	metricBudget map[string]int
	staleAfter   time.Duration
	lateAfter    time.Duration

	alias        map[string]string // 别名 -> 规范化名
	series       map[string]*seriesState
	teamActive   map[string]int
	metricActive map[string]int
	historical   int
}

// New 创建一个准入模块。
func New(cfg Config) *Controller {
	keep := make(map[string]bool, len(cfg.KeepLabels))
	for _, k := range cfg.KeepLabels {
		keep[k] = true
	}
	c := &Controller{
		keep:         keep,
		teamBudget:   map[string]int{},
		metricBudget: map[string]int{},
		staleAfter:   cfg.StaleAfter,
		lateAfter:    cfg.LateAfter,
		alias:        map[string]string{},
		series:       map[string]*seriesState{},
		teamActive:   map[string]int{},
		metricActive: map[string]int{},
	}
	for k, v := range cfg.TeamBudget {
		c.teamBudget[k] = v
	}
	for k, v := range cfg.MetricBudget {
		c.metricBudget[k] = v
	}
	return c
}

// RegisterAlias 把新名 alias 登记为已有指标名 canonical 的别名。
// 之后使用任一名字的样本都解析到同一规范化名，从而共用序列。
// canonical 本身也可以是已登记的别名，会被解析到最终的规范化名。
func (c *Controller) RegisterAlias(canonical, alias string) error {
	if canonical == "" || alias == "" {
		return errors.New("admission: canonical and alias must be non-empty")
	}
	canonical = c.resolve(canonical)
	if existing, ok := c.alias[alias]; ok && existing != canonical {
		return fmt.Errorf("admission: alias %q already registered to %q", alias, existing)
	}
	if alias == canonical {
		return nil
	}
	c.alias[alias] = canonical
	return nil
}

func (c *Controller) resolve(metric string) string {
	seen := map[string]bool{}
	for {
		next, ok := c.alias[metric]
		if !ok || next == metric {
			return metric
		}
		if seen[metric] { // 防御：别名成环时停止解析
			return metric
		}
		seen[metric] = true
		metric = next
	}
}

// clean 按 KeepLabels 清洗标签，返回排序后的 "k=v" 片段。
func (c *Controller) clean(labels map[string]string) []string {
	parts := make([]string, 0, len(labels))
	for k, v := range labels {
		if c.keep[k] {
			parts = append(parts, k+"\x01"+v)
		}
	}
	sort.Strings(parts)
	return parts
}

// identityKey 计算序列身份键：(团队, 规范化指标名, 清洗后标签组)。
func (c *Controller) identityKey(team, canonicalMetric string, labels map[string]string) string {
	var b strings.Builder
	b.WriteString(team)
	b.WriteByte(0)
	b.WriteString(canonicalMetric)
	for _, p := range c.clean(labels) {
		b.WriteByte(0)
		b.WriteString(p)
	}
	return b.String()
}

// expire 退出在 confirmedAt 时刻已经超期的序列。
// 严格大于 StaleAfter 才退出；恰好等于时仍然活跃。
func (c *Controller) expire(confirmedAt time.Time) {
	for id, st := range c.series {
		if confirmedAt.Sub(st.lastSeen) > c.staleAfter {
			delete(c.series, id)
			c.teamActive[st.team]--
			c.metricActive[st.metric]--
		}
	}
}

// Admit 对一个样本批次做准入判定。confirmedAt 是采集确认时刻，
// 用于迟到判定与序列退出判定。结果与输入样本一一对应。
func (c *Controller) Admit(samples []Sample, confirmedAt time.Time) BatchResult {
	c.expire(confirmedAt)

	res := BatchResult{Results: make([]SampleResult, 0, len(samples))}
	blockedSeen := map[BudgetRef]bool{}

	for _, s := range samples {
		canonical := c.resolve(s.Metric)
		id := c.identityKey(s.Team, canonical, s.Labels)
		sr := SampleResult{Sample: s, Identity: id}

		// 明显迟到：记为历史写入，不触碰序列状态与预算。
		// 严格大于 LateAfter 才算迟到；恰好等于仍按新鲜样本处理。
		if confirmedAt.Sub(s.Time) > c.lateAfter {
			sr.Decision = Historical
			c.historical++
			res.Historical++
			res.Results = append(res.Results, sr)
			continue
		}

		// 已有身份：直接共用序列并刷新保活时间。
		if st, ok := c.series[id]; ok {
			st.lastSeen = confirmedAt
			sr.Decision = Accepted
			res.Results = append(res.Results, sr)
			continue
		}

		// 新增身份：检查团队预算与指标预算。
		var blocked []BudgetRef
		if lim, ok := c.teamBudget[s.Team]; ok && c.teamActive[s.Team] >= lim {
			blocked = append(blocked, BudgetRef{Kind: "team", Key: s.Team, Limit: lim, Used: c.teamActive[s.Team]})
		}
		if lim, ok := c.metricBudget[canonical]; ok && c.metricActive[canonical] >= lim {
			blocked = append(blocked, BudgetRef{Kind: "metric", Key: canonical, Limit: lim, Used: c.metricActive[canonical]})
		}
		if len(blocked) > 0 {
			sr.Decision = Rejected
			sr.BlockedBy = blocked
			for _, b := range blocked {
				if !blockedSeen[b] {
					blockedSeen[b] = true
					res.Blocked = append(res.Blocked, b)
				}
			}
			res.Results = append(res.Results, sr)
			continue
		}

		c.series[id] = &seriesState{team: s.Team, metric: canonical, lastSeen: confirmedAt}
		c.teamActive[s.Team]++
		c.metricActive[canonical]++
		sr.Decision = Accepted
		sr.NewIdentity = true
		res.NewIdentities = append(res.NewIdentities, id)
		res.Results = append(res.Results, sr)
	}
	return res
}

// ActiveCount 返回当前活跃序列总数。
func (c *Controller) ActiveCount() int { return len(c.series) }

// TeamActive 返回某团队当前活跃序列数。
func (c *Controller) TeamActive(team string) int { return c.teamActive[team] }

// MetricActive 返回某指标（可为别名）当前活跃序列数。
func (c *Controller) MetricActive(metric string) int { return c.metricActive[c.resolve(metric)] }

// HistoricalWrites 返回累计记为历史写入的样本数。
func (c *Controller) HistoricalWrites() int { return c.historical }
