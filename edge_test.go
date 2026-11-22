package distinctagg

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

func baseConfig() Config {
	return Config{
		GroupingSets: []GroupingSet{
			{Name: "all", KeyColumns: nil},
			{Name: "by_k", KeyColumns: []int{0}},
		},
		Distincts: []DistinctSpec{
			{Name: "d_v", ArgColumns: []int{1}},
			{Name: "d_pair", ArgColumns: []int{1, 2}},
		},
		MemoryBudgetBytes: 1 << 20,
	}
}

// TestEmptyInputZeroRow 无数据时仍对空组集产生计数为零的一行。
func TestEmptyInputZeroRow(t *testing.T) {
	cfg := baseConfig()
	engine, err := New(cfg)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	defer engine.Close()

	result, err := engine.Run(context.Background(), nil)
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if len(result.Rows) != 1 {
		t.Fatalf("rows = %d, want exactly one zero-count row for the empty grouping set", len(result.Rows))
	}
	row := result.Rows[0]
	if row.SetName != "all" {
		t.Errorf("SetName = %q, want all", row.SetName)
	}
	if len(row.GroupValues) != 0 || len(row.MemberRowIDs) != 0 {
		t.Errorf("empty row should have no keys/members, got %+v", row)
	}
	for _, c := range row.DistinctCounts {
		if c != 0 {
			t.Errorf("DistinctCounts = %v, want all zero", row.DistinctCounts)
		}
	}
	for _, b := range row.SelectedBitmap {
		if b {
			t.Error("empty grouping set bitmap must be all false")
		}
	}
	if result.Stats.AttemptsTotal != 0 {
		t.Errorf("AttemptsTotal = %d, want 0 with no partitions", result.Stats.AttemptsTotal)
	}

	// 显式传入空分区切片时行为一致。
	result2, err := engine.Run(context.Background(), [][]Row{{}})
	if err != nil {
		t.Fatalf("Run empty partition: %v", err)
	}
	if len(result2.Rows) != 1 || result2.Rows[0].Count("d_v") != 0 {
		t.Errorf("empty partition result wrong: %+v", result2.Rows)
	}
}

// TestNullArgExcluded 任一 DISTINCT 参数为 NULL 时该行不计入该组合，
// 但仍计入组的成员与其他组合。
func TestNullArgExcluded(t *testing.T) {
	cfg := baseConfig()
	engine, err := New(cfg)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	defer engine.Close()

	rows := []Row{
		{ID: 1, Values: []Value{StringValue("k"), StringValue("x"), NullValue()}},
		{ID: 2, Values: []Value{StringValue("k"), StringValue("x"), StringValue("z")}},
		{ID: 3, Values: []Value{StringValue("k"), NullValue(), StringValue("z")}},
	}
	result, err := engine.Run(context.Background(), [][]Row{rows})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	g := result.FindRow("by_k", StringValue("k"))
	if g == nil {
		t.Fatal("missing group k")
	}
	if len(g.MemberRowIDs) != 3 {
		t.Errorf("members = %v, want all 3 rows", g.MemberRowIDs)
	}
	// d_v：行 3 参数 NULL 被排除；x 重复 -> 1
	if got := g.Count("d_v"); got != 1 {
		t.Errorf("d_v = %d, want 1", got)
	}
	// d_pair：行 1、行 3 各含 NULL 参数被排除，只剩 ("x","z") -> 1
	if got := g.Count("d_pair"); got != 1 {
		t.Errorf("d_pair = %d, want 1", got)
	}
}

// TestCrossSegmentDedup 极小预算强制单分区多次溢写，
// 同一去重值跨段出现时最终只能计一次。
func TestCrossSegmentDedup(t *testing.T) {
	cfg := baseConfig()
	cfg.MemoryBudgetBytes = 120 // 必然产生多个落盘段
	cfg.WorkDir = filepath.Join(t.TempDir(), "spill")
	engine, err := New(cfg)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	defer engine.Close()

	var rows []Row
	for i := int64(1); i <= 20; i++ {
		rows = append(rows, Row{
			ID:     i,
			Values: []Value{StringValue("k"), StringValue("dup"), StringValue("same")},
		})
	}
	result, err := engine.Run(context.Background(), [][]Row{rows})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if result.Stats.SpillSegments < 2 {
		t.Fatalf("SpillSegments = %d, want >= 2 under tight budget", result.Stats.SpillSegments)
	}
	g := result.FindRow("by_k", StringValue("k"))
	if g == nil {
		t.Fatal("missing group")
	}
	if g.Count("d_v") != 1 || g.Count("d_pair") != 1 {
		t.Errorf("cross-segment dedup failed: counts = %+v", g.DistinctCounts)
	}
	if len(g.MemberRowIDs) != 20 {
		t.Errorf("members = %d, want 20", len(g.MemberRowIDs))
	}
}

// TestCrossPartitionDedup 同一去重值跨分区重复只计一次。
func TestCrossPartitionDedup(t *testing.T) {
	cfg := baseConfig()
	engine, err := New(cfg)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	defer engine.Close()

	mk := func(id int64) Row {
		return Row{ID: id, Values: []Value{StringValue("k"), StringValue("v"), StringValue("p")}}
	}
	partitions := [][]Row{
		{mk(1), mk(2)},
		{mk(3), mk(4)},
		{mk(5)},
	}
	result, err := engine.Run(context.Background(), partitions)
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	g := result.FindRow("by_k", StringValue("k"))
	if g == nil {
		t.Fatal("missing group")
	}
	if g.Count("d_v") != 1 || g.Count("d_pair") != 1 {
		t.Errorf("cross-partition dedup failed: counts = %+v", g.DistinctCounts)
	}
	if len(g.MemberRowIDs) != 5 {
		t.Errorf("members = %v, want 5 distinct row IDs", g.MemberRowIDs)
	}
	if result.Stats.Partitions != 3 || result.Stats.Retries != 0 {
		t.Errorf("stats wrong: %+v", result.Stats)
	}
}

// TestRetryDiscardsUncommittedSegments 已写完但未确认的旧片段不能污染结果。
func TestRetryDiscardsUncommittedSegments(t *testing.T) {
	cfg := baseConfig()
	cfg.MemoryBudgetBytes = 120
	cfg.WorkDir = filepath.Join(t.TempDir(), "spill")

	var failed bool
	var failMu sync.Mutex
	cfg.FaultHook = func(partition, attempt int, phase SpillPhase, spillSeq int) error {
		if partition == 0 && attempt == 1 && phase == PhaseAfterWrite {
			failMu.Lock()
			defer failMu.Unlock()
			if !failed {
				failed = true
				return errInjected
			}
		}
		return nil
	}
	engine, err := New(cfg)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	defer engine.Close()

	rows := []Row{
		{ID: 1, Values: []Value{StringValue("k"), StringValue("a"), StringValue("x")}},
		{ID: 2, Values: []Value{StringValue("k"), StringValue("b"), StringValue("y")}},
		{ID: 3, Values: []Value{StringValue("k"), StringValue("a"), StringValue("x")}},
	}
	result, err := engine.Run(context.Background(), [][]Row{rows})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if result.Stats.Retries != 1 || engine.winners[0] != 2 {
		t.Fatalf("retry stats wrong: %+v winners=%v", result.Stats, engine.winners)
	}
	g := result.FindRow("by_k", StringValue("k"))
	if g == nil || g.Count("d_v") != 2 || g.Count("d_pair") != 2 {
		t.Fatalf("polluted or wrong counts after retry: %+v", g)
	}
	if len(g.MemberRowIDs) != 3 {
		t.Errorf("members = %v, want 3", g.MemberRowIDs)
	}
	// 暂存目录必须不存在任何残留，committed 下只有 attempt-2。
	if dirty, err := engine.store.hasStaging(0); err != nil || dirty {
		t.Errorf("staging leftover: dirty=%v err=%v", dirty, err)
	}
	committed := filepath.Join(engine.StoreRoot(), "p0", "committed")
	entries, err := readDirNames(committed)
	if err != nil {
		t.Fatalf("read committed dir: %v", err)
	}
	if len(entries) != 1 || entries[0] != "attempt-2" {
		t.Errorf("committed attempts = %v, want only [attempt-2]", entries)
	}
}

// TestAllAttemptsFail 持续故障时重做次数有上限，错误向上传递且无结果产出。
func TestAllAttemptsFail(t *testing.T) {
	cfg := baseConfig()
	cfg.MemoryBudgetBytes = 120
	cfg.MaxAttempts = 2
	cfg.FaultHook = func(partition, attempt int, phase SpillPhase, spillSeq int) error {
		if phase == PhaseBeforeCommit {
			return errInjected
		}
		return nil
	}
	engine, err := New(cfg)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	defer engine.Close()

	rows := []Row{{ID: 1, Values: []Value{StringValue("k"), StringValue("a"), StringValue("x")}}}
	_, err = engine.Run(context.Background(), [][]Row{rows})
	if err == nil || !strings.Contains(err.Error(), "exhausted") {
		t.Fatalf("err = %v, want exhausted attempts error", err)
	}
	if dirty, _ := engine.store.hasStaging(0); dirty {
		t.Error("failed attempts must leave no staging leftovers")
	}
}

// TestSegmentStoreCommitAtomicity 存储层两阶段提交的直接单元测试：
// 暂存片段在提交前对合并不可见，提交后整体可见。
func TestSegmentStoreCommitAtomicity(t *testing.T) {
	store := newSegmentStore(filepath.Join(t.TempDir(), "store"))
	seg := &segment{
		Partition: 0,
		Groups: []*groupState{
			newGroupState(0, "k", []Value{StringValue("k")}),
		},
	}

	attempt, _, err := store.startAttempt(0)
	if err != nil {
		t.Fatalf("startAttempt: %v", err)
	}
	if attempt != 1 {
		t.Fatalf("attempt = %d, want 1", attempt)
	}
	if _, _, err := store.writeSegment(0, attempt, 0, seg); err != nil {
		t.Fatalf("writeSegment: %v", err)
	}
	// 提交前：committedSegments 找不到任何片段。
	if paths, err := store.committedSegments(0, attempt); err == nil {
		t.Fatalf("uncommitted attempt visible to merge: %v", paths)
	}
	// 第二次尝试编号严格递增。
	attempt2, _, err := store.startAttempt(0)
	if err != nil || attempt2 != 2 {
		t.Fatalf("second attempt = %d err=%v, want 2", attempt2, err)
	}
	if _, _, err := store.writeSegment(0, attempt2, 0, seg); err != nil {
		t.Fatalf("writeSegment 2: %v", err)
	}
	// 此时第一次尝试仍在暂存：合并看不到任何已确认片段。
	if dirty, _ := store.hasStaging(0); !dirty {
		t.Error("expected staging attempts to be visible before abort/commit")
	}
	if err := store.abortAttempt(0, attempt2); err != nil {
		t.Fatalf("abort: %v", err)
	}
	// abort 只删除 attempt-2，attempt-1 仍在暂存，提交前对合并依然不可见。
	if paths, err := store.committedSegments(0, attempt); err == nil {
		t.Fatalf("uncommitted attempt visible to merge: %v", paths)
	}
	if err := store.abortAttempt(0, attempt); err != nil {
		t.Fatalf("abort attempt 1: %v", err)
	}
	if dirty, _ := store.hasStaging(0); dirty {
		t.Error("abort did not remove all staging attempts")
	}
	// 重新做一次完整尝试并提交：合并只能看到它的整组片段。
	attempt3, _, err := store.startAttempt(0)
	if err != nil || attempt3 != 3 {
		t.Fatalf("third attempt = %d err=%v, want 3", attempt3, err)
	}
	if _, _, err := store.writeSegment(0, attempt3, 0, seg); err != nil {
		t.Fatalf("writeSegment 3: %v", err)
	}
	if _, err := store.commitAttempt(0, attempt3); err != nil {
		t.Fatalf("commit: %v", err)
	}
	paths, err := store.committedSegments(0, attempt3)
	if err != nil {
		t.Fatalf("committedSegments: %v", err)
	}
	if len(paths) != 1 {
		t.Fatalf("committed paths = %v, want 1", paths)
	}
	got, err := readSegment(paths[0])
	if err != nil {
		t.Fatalf("readSegment: %v", err)
	}
	if len(got.Groups) != 1 || got.Groups[0].Key != "k" {
		t.Errorf("round-trip segment wrong: %+v", got.Groups)
	}
}

// TestContextCancellation 上下文取消后运行快速失败。
func TestContextCancellation(t *testing.T) {
	cfg := baseConfig()
	cfg.MemoryBudgetBytes = 120
	engine, err := New(cfg)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	defer engine.Close()

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	rows := []Row{{ID: 1, Values: []Value{StringValue("k"), StringValue("a"), StringValue("x")}}}
	if _, err := engine.Run(ctx, [][]Row{rows}); err == nil {
		t.Fatal("expected context cancellation error")
	}
}

// TestConcurrentPartitionsRace 多分区并发处理，配合 -race 检查数据竞争。
func TestConcurrentPartitionsRace(t *testing.T) {
	cfg := baseConfig()
	cfg.MemoryBudgetBytes = 200
	engine, err := New(cfg)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	defer engine.Close()

	const partitions = 4
	var parts [][]Row
	var id int64
	for p := 0; p < partitions; p++ {
		var rows []Row
		for i := 0; i < 25; i++ {
			id++
			rows = append(rows, Row{
				ID:     id,
				Values: []Value{StringValue("k"), StringValue("v"), StringValue("p")},
			})
		}
		parts = append(parts, rows)
	}
	result, err := engine.Run(context.Background(), parts)
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	g := result.FindRow("all")
	if g == nil {
		t.Fatal("missing empty-grouping-set row")
	}
	if g.Count("d_v") != 1 || g.Count("d_pair") != 1 {
		t.Errorf("concurrent dedup failed: %+v", g.DistinctCounts)
	}
	if len(g.MemberRowIDs) != 100 {
		t.Errorf("members = %d, want 100", len(g.MemberRowIDs))
	}
	if result.Stats.Partitions != partitions || result.Stats.Retries != 0 {
		t.Errorf("stats wrong: %+v", result.Stats)
	}
}

// TestInvalidConfig 配置校验的边界用例。
func TestInvalidConfig(t *testing.T) {
	cases := []struct {
		name   string
		modify func(*Config)
	}{
		{"no grouping sets", func(c *Config) { c.GroupingSets = nil }},
		{"no distincts", func(c *Config) { c.Distincts = nil }},
		{"zero budget", func(c *Config) { c.MemoryBudgetBytes = 0 }},
		{"dup group name", func(c *Config) { c.GroupingSets[1].Name = "all" }},
		{"negative column", func(c *Config) { c.GroupingSets[0].KeyColumns = []int{-1} }},
		{"empty distinct args", func(c *Config) { c.Distincts[0].ArgColumns = nil }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg := baseConfig()
			tc.modify(&cfg)
			if _, err := New(cfg); err == nil {
				t.Fatal("expected validation error")
			}
		})
	}
}

// TestRowWidthMismatch 行宽与配置不符时报错而不是 panic。
func TestRowWidthMismatch(t *testing.T) {
	cfg := baseConfig()
	engine, err := New(cfg)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	defer engine.Close()
	bad := []Row{{ID: 1, Values: []Value{StringValue("k")}}}
	if _, err := engine.Run(context.Background(), [][]Row{bad}); err == nil {
		t.Fatal("expected row width error")
	}
}

func readDirNames(dir string) ([]string, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	names := make([]string, 0, len(entries))
	for _, e := range entries {
		names = append(names, e.Name())
	}
	return names, nil
}
