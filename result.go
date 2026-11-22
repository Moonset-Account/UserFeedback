package distinctagg

import "sort"

// OutputRow 是一个组集内一个组的最终输出。
type OutputRow struct {
	SetID   int
	SetName string
	// GroupValues 与该组集声明的 KeyColumns 一一对应；空组集时长度为 0。
	GroupValues []Value
	// SelectedBitmap 与输入行宽度等长：组集选中的列为 true。
	// 未选中的列恒为 NULL；若选中列本身就是 NULL，SelectedBitmap 仍为 true，
	// 从而把“原本就是 NULL”与“该组集未选择此列”严格区分开。
	SelectedBitmap []bool
	// DistinctCounts 按 Config.Distincts 的顺序给出各 DISTINCT 组合的去重计数。
	DistinctCounts []int
	// MemberRowIDs 是归入该组的全部稳定行序号（已排序、去重）。
	MemberRowIDs []int64

	// specNames 与 DistinctCounts 一一对应，支撑按名称取计数。
	specNames []string
}

// Count 按 DISTINCT 组合名称取计数，名称不存在时返回 -1。
func (r *OutputRow) Count(specName string) int {
	for i, name := range r.specNames {
		if name == specName {
			return r.DistinctCounts[i]
		}
	}
	return -1
}

// Result 是一次完整聚合的输出与统计。
type Result struct {
	// Rows 已按 (SetID, GroupValues) 确定性排序。
	Rows []OutputRow
	// Stats 是执行统计，见 Stats 字段说明。
	Stats Stats
}

// FindRow 按组集名称与组键查找输出行；group 长度为 0 表示空组集的唯一行。
// 找不到返回 nil。
func (r *Result) FindRow(setName string, group ...Value) *OutputRow {
	for i := range r.Rows {
		row := &r.Rows[i]
		if row.SetName != setName {
			continue
		}
		if len(row.GroupValues) != len(group) {
			continue
		}
		equal := true
		for j := range group {
			if !row.GroupValues[j].Equal(group[j]) {
				equal = false
				break
			}
		}
		if equal {
			return row
		}
	}
	return nil
}

// Stats 汇总预算、溢写与重做信息，便于调用方核对预算与来源。
type Stats struct {
	Partitions    int
	AttemptsTotal int // 所有分区实际执行的尝试次数（含失败尝试）
	Retries       int // 重做次数（AttemptsTotal - Partitions）
	SpillSegments int // 获选尝试提交的落盘段总数
	BytesWritten  int // 所有尝试（含失败尝试）写入暂存段的总字节
	SpilledBytes  int // 仅获选尝试已确认片段的总字节
	PeakLiveBytes int64
	// PeakLiveExceeded 报告峰值是否严格超过预算。预算在每行写入前检查、
	// 到点即溢写；一行会同时进入全部组集，因此峰值可能短暂超过预算，
	// 但超出量不超过单行最大增量，下一行开始前状态必定已被清空。
	PeakLiveExceeded bool
}

func sortedMembers(m map[int64]struct{}) []int64 {
	out := make([]int64, 0, len(m))
	for id := range m {
		out = append(out, id)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}
