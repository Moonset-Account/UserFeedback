package distinctagg

import (
	"context"
	"errors"
	"path/filepath"
	"sync/atomic"
	"testing"
)

// errInjected 是故障注入测试使用的哨兵错误。
var errInjected = errors.New("injected mid-spill failure")

// maxRowGrowth 估算夹具中单行在最坏情况下（新组、新成员、所有非空
// DISTINCT 参数都是新值）给内存状态带来的最大增量，作为预算软上限的容差。
func maxRowGrowth(cfg Config, partitions [][]Row) int {
	worst := 0
	for _, part := range partitions {
		for _, row := range part {
			growth := 0
			for setID, gs := range cfg.GroupingSets {
				keyVals := make([]Value, 0, len(gs.KeyColumns))
				for _, c := range gs.KeyColumns {
					keyVals = append(keyVals, row.Values[c])
				}
				// 新 map 槽（组集前缀+组键）+ 新 groupState 基础开销 + 新成员
				growth += liveDelta(mapKey(setID, encodeKey(keyVals))) + bytesGroupBase
				growth += bytesMapEntry + bytesInt64Hdr
				for _, d := range cfg.Distincts {
					nullArg := false
					args := make([]Value, 0, len(d.ArgColumns))
					for _, c := range d.ArgColumns {
						if row.Values[c].IsNull {
							nullArg = true
							break
						}
						args = append(args, row.Values[c])
					}
					if nullArg {
						continue
					}
					growth += bytesDistinctState + liveDelta(encodeKey(args))
				}
			}
			if growth > worst {
				worst = growth
			}
		}
	}
	return worst
}

// buildFixture 构造题面要求的夹具：
// 同一批行同时进入三种组集，两项不同 DISTINCT，跨分区重复值，
// 以及一次“段已写完但尝试未确认”的半途溢写失败。
//
// 输入列：0=region, 1=product, 2=qty(int), 3=tag(string)
func buildFixture() (Config, [][]Row) {
	cfg := Config{
		GroupingSets: []GroupingSet{
			{Name: "G_empty", KeyColumns: nil},                  // 空组集
			{Name: "G_region", KeyColumns: []int{0}},            // 单键组集
			{Name: "G_region_product", KeyColumns: []int{0, 1}}, // 双键组集
		},
		Distincts: []DistinctSpec{
			{Name: "D_product", ArgColumns: []int{1}},
			{Name: "D_qty_tag", ArgColumns: []int{2, 3}},
		},
		// 预算刻意压小，保证每个分区在处理过程中都发生溢写（含段内多次溢写）。
		MemoryBudgetBytes: 300,
		MaxAttempts:       3,
	}

	part0 := []Row{
		{ID: 1, Values: []Value{StringValue("east"), StringValue("apple"), IntValue(10), StringValue("a")}},
		{ID: 2, Values: []Value{StringValue("east"), StringValue("pear"), IntValue(20), StringValue("b")}},
		// 行 3：tag 为 NULL -> D_qty_tag 跳过此行，D_product 仍计入。
		{ID: 3, Values: []Value{StringValue("east"), StringValue("plum"), IntValue(30), NullValue()}},
		{ID: 4, Values: []Value{StringValue("west"), StringValue("apple"), IntValue(10), StringValue("a")}},
		// 行 5：region 显式 NULL，与“组集未选择该列”的 NULL 语义不同。
		{ID: 5, Values: []Value{NullValue(), StringValue("apple"), IntValue(40), StringValue("c")}},
	}
	part1 := []Row{
		// 行 6、7 与分区 0 产生跨分区重复 DISTINCT 值。
		{ID: 6, Values: []Value{StringValue("east"), StringValue("apple"), IntValue(10), StringValue("a")}},
		{ID: 7, Values: []Value{StringValue("west"), StringValue("pear"), IntValue(20), StringValue("b")}},
		// 行 8：qty 为 NULL -> D_qty_tag 跳过。
		{ID: 8, Values: []Value{StringValue("west"), StringValue("fig"), NullValue(), StringValue("d")}},
	}
	return cfg, [][]Row{part0, part1}
}

// TestFixtureDistinctAggregation 是题面指定的综合验收测试。
func TestFixtureDistinctAggregation(t *testing.T) {
	cfg, partitions := buildFixture()

	workDir := t.TempDir()
	cfg.WorkDir = filepath.Join(workDir, "spill")

	// 仅在分区 0 的第一次尝试、第一次“段写完但未确认”时失败一次。
	// hook 会被多个分区 goroutine 并发调用，计数器必须用原子操作。
	var hookCalls atomic.Int64
	cfg.FaultHook = func(partition, attempt int, phase SpillPhase, spillSeq int) error {
		hookCalls.Add(1)
		if partition == 0 && attempt == 1 && phase == PhaseAfterWrite && spillSeq == 0 {
			return errInjected
		}
		return nil
	}

	engine, err := New(cfg)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	defer engine.Close()

	result, err := engine.Run(context.Background(), partitions)
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if hookCalls.Load() == 0 {
		t.Fatal("fault hook was never invoked")
	}

	// ---------- 重做与预算统计 ----------
	st := result.Stats
	tolerance := maxRowGrowth(cfg, partitions)
	t.Logf("fixture stats: partitions=%d attempts=%d retries=%d spillSegments=%d "+
		"bytesWritten=%d spilledBytes=%d peakLive=%d (budget=%d, one-row tolerance=%d)",
		st.Partitions, st.AttemptsTotal, st.Retries, st.SpillSegments,
		st.BytesWritten, st.SpilledBytes, st.PeakLiveBytes, cfg.MemoryBudgetBytes, tolerance)
	if st.Partitions != 2 {
		t.Errorf("Partitions = %d, want 2", st.Partitions)
	}
	if st.AttemptsTotal != 3 {
		t.Errorf("AttemptsTotal = %d, want 3 (2 first attempts + 1 retry)", st.AttemptsTotal)
	}
	if st.Retries != 1 {
		t.Errorf("Retries = %d, want 1", st.Retries)
	}
	if st.SpillSegments < 2 {
		t.Errorf("SpillSegments = %d, want >= 2 (every partition spills)", st.SpillSegments)
	}
	if st.PeakLiveBytes <= 0 {
		t.Errorf("PeakLiveBytes = %d, want > 0", st.PeakLiveBytes)
	}
	// 预算语义：每行写入前检查，到点即整段溢写并清空；
	// 因此峰值只可能超出预算“至多一行带来的增量”（一行会同时进入全部组集）。
	if st.PeakLiveBytes > int64(cfg.MemoryBudgetBytes)+int64(tolerance) {
		t.Errorf("PeakLiveBytes = %d exceeds budget %d by more than one row growth %d",
			st.PeakLiveBytes, cfg.MemoryBudgetBytes, tolerance)
	}
	if st.BytesWritten <= st.SpilledBytes {
		t.Errorf("BytesWritten (%d) should exceed committed SpilledBytes (%d) due to the aborted attempt",
			st.BytesWritten, st.SpilledBytes)
	}
	if st.SpilledBytes <= 0 {
		t.Errorf("SpilledBytes = %d, want > 0", st.SpilledBytes)
	}

	// 失败尝试的暂存目录必须被清理，已确认目录只保留获选尝试。
	if dirty, err := engine.store.hasStaging(0); err != nil || dirty {
		t.Errorf("partition 0 staging leftovers: dirty=%v err=%v", dirty, err)
	}
	winner0 := engine.winners[0]
	if winner0 != 2 {
		t.Errorf("partition 0 winner attempt = %d, want 2", winner0)
	}
	if engine.winners[1] != 1 {
		t.Errorf("partition 1 winner attempt = %d, want 1", engine.winners[1])
	}

	// ---------- 空组集：所有 8 行同组，跨分区去重 ----------
	g0 := result.FindRow("G_empty")
	if g0 == nil {
		t.Fatal("missing G_empty row")
	}
	if len(g0.GroupValues) != 0 {
		t.Errorf("G_empty GroupValues len = %d, want 0", len(g0.GroupValues))
	}
	if len(g0.MemberRowIDs) != 8 {
		t.Errorf("G_empty members = %v, want all 8 stable row IDs", g0.MemberRowIDs)
	}
	// 产品去重：apple/pear/plum/fig = 4
	if got := g0.Count("D_product"); got != 4 {
		t.Errorf("G_empty D_product = %d, want 4", got)
	}
	// (qty,tag) 去重：行 3、8 因 NULL 参数被排除；
	// (10,a) 在行 1/4/6 重复、(20,b) 在行 2/7 重复，各计一次 -> 3
	if got := g0.Count("D_qty_tag"); got != 3 {
		t.Errorf("G_empty D_qty_tag = %d, want 3", got)
	}
	for _, b := range g0.SelectedBitmap {
		if b {
			t.Error("empty grouping set must mark every column as not selected")
		}
	}

	// ---------- region 组集 ----------
	east := result.FindRow("G_region", StringValue("east"))
	west := result.FindRow("G_region", StringValue("west"))
	nullRegion := result.FindRow("G_region", NullValue())
	if east == nil || west == nil || nullRegion == nil {
		t.Fatalf("missing region groups: east=%v west=%v null=%v", east, west, nullRegion)
	}
	if east.Count("D_product") != 3 { // apple, pear, plum
		t.Errorf("east D_product = %d, want 3", east.Count("D_product"))
	}
	if east.Count("D_qty_tag") != 2 { // (10,a), (20,b)
		t.Errorf("east D_qty_tag = %d, want 2", east.Count("D_qty_tag"))
	}
	if len(east.MemberRowIDs) != 4 { // 行 1,2,3,6
		t.Errorf("east members = %v, want [1 2 3 6]", east.MemberRowIDs)
	}
	if west.Count("D_product") != 3 { // apple, pear, fig
		t.Errorf("west D_product = %d, want 3", west.Count("D_product"))
	}
	if west.Count("D_qty_tag") != 2 { // (10,a), (20,b)；行 8 qty NULL 被排除
		t.Errorf("west D_qty_tag = %d, want 2", west.Count("D_qty_tag"))
	}
	if len(west.MemberRowIDs) != 3 { // 行 4,7,8
		t.Errorf("west members = %v, want [4 7 8]", west.MemberRowIDs)
	}
	// 显式 NULL 键：region 列被选中且值为 NULL；未选择的 product/qty/tag 也是 NULL，
	// 两者必须通过 SelectedBitmap 区分。
	if !nullRegion.SelectedBitmap[0] {
		t.Error("NULL region key must still show column 0 as selected")
	}
	if nullRegion.SelectedBitmap[1] || nullRegion.SelectedBitmap[2] || nullRegion.SelectedBitmap[3] {
		t.Error("unselected columns must show as not selected even when value is NULL")
	}
	if !nullRegion.GroupValues[0].IsNull {
		t.Error("group value must carry the explicit NULL")
	}
	if nullRegion.Count("D_product") != 1 { // apple
		t.Errorf("null-region D_product = %d, want 1", nullRegion.Count("D_product"))
	}
	if nullRegion.Count("D_qty_tag") != 1 { // (40,c)
		t.Errorf("null-region D_qty_tag = %d, want 1", nullRegion.Count("D_qty_tag"))
	}
	if len(nullRegion.MemberRowIDs) != 1 || nullRegion.MemberRowIDs[0] != 5 {
		t.Errorf("null-region members = %v, want [5]", nullRegion.MemberRowIDs)
	}
	// east/west 组的未选择列位图。
	if east.SelectedBitmap[0] != true || east.SelectedBitmap[1] != false {
		t.Errorf("G_region bitmap = %v, want only column 0 selected", east.SelectedBitmap)
	}

	// ---------- region+product 双键组集 ----------
	ea := result.FindRow("G_region_product", StringValue("east"), StringValue("apple"))
	if ea == nil {
		t.Fatal("missing (east, apple) group")
	}
	if ea.Count("D_product") != 1 {
		t.Errorf("(east,apple) D_product = %d, want 1", ea.Count("D_product"))
	}
	if ea.Count("D_qty_tag") != 1 { // 行 1、6 的 (10,a) 跨分区只计一次
		t.Errorf("(east,apple) D_qty_tag = %d, want 1", ea.Count("D_qty_tag"))
	}
	if len(ea.MemberRowIDs) != 2 || ea.MemberRowIDs[0] != 1 || ea.MemberRowIDs[1] != 6 {
		t.Errorf("(east,apple) members = %v, want [1 6]", ea.MemberRowIDs)
	}
	wp := result.FindRow("G_region_product", StringValue("west"), StringValue("pear"))
	if wp == nil || wp.Count("D_qty_tag") != 1 { // 行 2 不在 west；west pear 行 7 -> (20,b) 一次
		t.Errorf("(west,pear) group wrong: %+v", wp)
	}
	fig := result.FindRow("G_region_product", StringValue("west"), StringValue("fig"))
	if fig == nil {
		t.Fatal("missing (west, fig) group")
	}
	if fig.Count("D_qty_tag") != 0 { // qty NULL -> 该组合不计入
		t.Errorf("(west,fig) D_qty_tag = %d, want 0", fig.Count("D_qty_tag"))
	}
	if fig.Count("D_product") != 1 {
		t.Errorf("(west,fig) D_product = %d, want 1", fig.Count("D_product"))
	}
	if len(fig.MemberRowIDs) != 1 || fig.MemberRowIDs[0] != 8 {
		t.Errorf("(west,fig) members = %v, want [8]", fig.MemberRowIDs)
	}
	// 双键组集位图：列 0、1 选中，列 2、3 未选中。
	for c, want := range []bool{true, true, false, false} {
		if ea.SelectedBitmap[c] != want {
			t.Errorf("G_region_product bitmap[%d] = %v, want %v", c, ea.SelectedBitmap[c], want)
		}
	}

	// ---------- 组集来源核对：同一行必须同时进入三种组集 ----------
	for _, id := range []int64{1, 5, 8} {
		var inSets []string
		for i := range result.Rows {
			for _, mid := range result.Rows[i].MemberRowIDs {
				if mid == id {
					inSets = append(inSets, result.Rows[i].SetName)
					break
				}
			}
		}
		wantSets := map[string]bool{"G_empty": true, "G_region": true, "G_region_product": true}
		gotSets := map[string]bool{}
		for _, n := range inSets {
			gotSets[n] = true
		}
		if len(gotSets) != 3 {
			t.Errorf("row %d appeared in %v, want all three grouping sets", id, inSets)
		}
		for n := range wantSets {
			if !gotSets[n] {
				t.Errorf("row %d missing from grouping set %s", id, n)
			}
		}
	}

	// 输出按 (SetID, 组键) 确定性排序。
	for i := 1; i < len(result.Rows); i++ {
		prev, cur := result.Rows[i-1], result.Rows[i]
		if prev.SetID > cur.SetID ||
			(prev.SetID == cur.SetID && encodeKey(prev.GroupValues) > encodeKey(cur.GroupValues)) {
			t.Errorf("rows not deterministically sorted at index %d", i)
		}
	}
}
