package distinctagg

// GroupingSet 描述一个组集：按哪些输入列分组。
// KeyColumns 为空表示空组集（所有行归入同一个全 NULL 键的组）。
type GroupingSet struct {
	Name       string
	KeyColumns []int
}

// DistinctSpec 描述一个独立的 DISTINCT 聚合输入列组合。
// 任一参数列为 NULL 的行不计入该组合。
type DistinctSpec struct {
	Name       string
	ArgColumns []int
}

// Row 是一条带稳定行序号的输入记录，序号由调用方保证全局唯一稳定。
type Row struct {
	ID     int64
	Values []Value
}

// SpillPhase 标识注入故障时所处的溢写阶段。
type SpillPhase int

const (
	// PhaseBeforeWrite：段文件被创建/写入之前。
	PhaseBeforeWrite SpillPhase = iota
	// PhaseAfterWrite：段文件已经完整写入暂存目录、但所在尝试尚未提交。
	PhaseAfterWrite
	// PhaseBeforeCommit：整个分区尝试的所有片段就绪、提交目录之前。
	PhaseBeforeCommit
)

// FaultHook 是测试用故障注入点。返回非 nil 错误会令本次分区尝试失败并重做。
type FaultHook func(partition int, attempt int, phase SpillPhase, spillSeq int) error

// Config 是聚合器配置。
type Config struct {
	GroupingSets []GroupingSet
	Distincts    []DistinctSpec
	// MemoryBudgetBytes 是单个分区内存中状态的估算上限（字节）。
	// 到点时当前状态整体溢写为一个落盘段并清空。
	MemoryBudgetBytes int
	// MaxAttempts 是单个分区允许的最大尝试次数（含第一次），默认 3。
	MaxAttempts int
	// WorkDir 是落盘根目录；为空时 Run 会创建临时目录并在 Close 时删除。
	WorkDir string
	// FaultHook 可选，仅用于故障注入测试。
	FaultHook FaultHook
}
