// Package faultcoordinator 实现隔离演练环境中的故障场景协调器。
//
// 协调器围绕“冗余拓扑 + 共享依赖容量底线”工作：
//   - 演练 (Drill) 声明要占用的实例集合、持续时间与优先级；
//   - 只有在同时占用后，每条被要求的容量路径 (RequiredPath)
//     的剩余容量仍不低于其底线时，演练才被准入，否则进入等待队列；
//   - 演练退出后并不立即归还影响预算，必须收到属于本次代次 (generation)
//     的健康恢复证据 (receipt) 才归还；过期、取消、重复回执各自留下可核记录；
//   - 外部模拟故障可在演练之间插入；若跌破底线，协调器撤回能够恢复可行性的
//     最少演练集合，同数量时优先撤回低优先级、再按演练 ID 定序；
//   - 任何撤回组合都无法恢复时，报告外部故障造成的逐路径剩余缺口。
package faultcoordinator

import "time"

// Dependency 是被实例共享的下游能力（例如共享数据库、带宽池、配额池）。
type Dependency struct {
	// ID 是依赖的唯一标识。
	ID string
	// Capacity 是该依赖的总容量。
	Capacity int
}

// Instance 是模拟服务拓扑中的一个实例。它可以路由到多个依赖，
// 每个依赖上声明该实例被演练占用时消耗的容量 Load。
type Instance struct {
	// ID 是实例的唯一标识。
	ID string
	// Routes 描述该实例使用到的依赖及在该依赖上消耗的容量。
	// 同一次拓扑定义中，同一依赖至多出现一次。
	Routes []Route
}

// Route 表示实例对某个依赖的容量占用。
type Route struct {
	// DependencyID 指向 Topology.Dependencies 中声明的依赖。
	DependencyID string
	// Load 是实例被占用时在该依赖上消耗的容量，必须为正数。
	Load int
}

// RequiredPath 是一条必须始终满足容量底线的要求路径。
// 一条路径聚合多个共享依赖：路径上的每个依赖都必须满足底线，
// 路径才算满足（短板语义）。
type RequiredPath struct {
	// ID 是要求路径的唯一标识。
	ID string
	// Floor 是该路径上每个依赖必须保留的剩余容量底线。
	Floor int
	// DependencyIDs 是路径覆盖的依赖集合（顺序无关，内部去重）。
	DependencyIDs []string
}

// Topology 是演练协调所基于的模拟冗余拓扑。
type Topology struct {
	// Dependencies 是全部共享下游能力。
	Dependencies []Dependency
	// Instances 是全部可被演练占用的实例。
	Instances []Instance
	// RequiredPaths 是必须持续满足的容量底线路径。
	RequiredPaths []RequiredPath
}

// Drill 描述一次演练请求。
type Drill struct {
	// ID 是演练的唯一标识，同一时刻不能重复提交未结束的演练。
	ID string
	// Instances 是本次演练要占用的实例集合。
	Instances []string
	// Duration 是演练计划持续时间。
	Duration time.Duration
	// Priority 是优先级，数值越小优先级越低；撤回同数量时优先撤回低优先级。
	Priority int
}

// DrillStatus 表示演练在协调器中的生命周期状态。
type DrillStatus int

const (
	// StatusWaiting 表示演练已提交但因容量底线约束在等待队列中。
	StatusWaiting DrillStatus = iota
	// StatusActive 表示演练已准入，正在占用实例与影响预算。
	StatusActive
	// StatusRecovering 表示演练已退出（或被撤回），等待本次代次的健康恢复证据。
	StatusRecovering
	// StatusReturned 表示已收到有效恢复回执，影响预算已归还。
	StatusReturned
)

// String 返回状态的可读名称。
func (s DrillStatus) String() string {
	switch s {
	case StatusWaiting:
		return "WAITING"
	case StatusActive:
		return "ACTIVE"
	case StatusRecovering:
		return "RECOVERING"
	case StatusReturned:
		return "RETURNED"
	default:
		return "UNKNOWN"
	}
}

// ReceiptKind 描述健康恢复证据的类型。
type ReceiptKind int

const (
	// ReceiptHealthy 是健康恢复证据，校验通过后归还影响预算。
	ReceiptHealthy ReceiptKind = iota
	// ReceiptCanceled 表示恢复流程被取消，不归还预算，仅留可核记录。
	ReceiptCanceled
)

// String 回执类型的可读名称。
func (k ReceiptKind) String() string {
	switch k {
	case ReceiptHealthy:
		return "HEALTHY"
	case ReceiptCanceled:
		return "CANCELED"
	default:
		return "UNKNOWN"
	}
}

// Receipt 是演练退出后提交的健康恢复证据。
type Receipt struct {
	// DrillID 是回执对应的演练。
	DrillID string
	// Generation 是回执声明所属的代次；只有与协调器记录的本次代次一致才有效。
	Generation int64
	// Kind 区分健康恢复证据与取消通知。
	Kind ReceiptKind
}

// ReceiptOutcome 是回执处理结论。
type ReceiptOutcome int

const (
	// OutcomeAccepted 表示回执有效，影响预算已归还。
	OutcomeAccepted ReceiptOutcome = iota
	// OutcomeExpired 表示代次不匹配（过期或超前），不归还预算。
	OutcomeExpired
	// OutcomeCanceled 表示取消回执，登记但不归还预算。
	OutcomeCanceled
	// OutcomeDuplicate 表示同一代次的回执已处理过，重复登记不产生副作用。
	OutcomeDuplicate
	// OutcomeUnknown 表示演练不存在或已被清理，无法核对。
	OutcomeUnknown
)

// String 回执结论的可读名称。
func (o ReceiptOutcome) String() string {
	switch o {
	case OutcomeAccepted:
		return "ACCEPTED"
	case OutcomeExpired:
		return "EXPIRED"
	case OutcomeCanceled:
		return "CANCELED"
	case OutcomeDuplicate:
		return "DUPLICATE"
	case OutcomeUnknown:
		return "UNKNOWN"
	default:
		return "UNKNOWN"
	}
}

// ReceiptResult 是 ReceiveReceipt 的返回结果。
type ReceiptResult struct {
	// DrillID 是对应演练。
	DrillID string
	// Generation 是回执声明的代次。
	Generation int64
	// Kind 是回执类型。
	Kind ReceiptKind
	// Outcome 是协调器的核对结论。
	Outcome ReceiptOutcome
	// Detail 给出人类可读的核对说明。
	Detail string
}

// ExternalFault 描述在演练之间插入的外部模拟故障。
// 它会永久性（直到被显式修复前）削减某个依赖的可用容量。
type ExternalFault struct {
	// ID 是外部故障的唯一标识，重复 ID 返回错误。
	ID string
	// DependencyID 是受影响的依赖。
	DependencyID string
	// Reduction 是容量削减量，必须为正数。
	Reduction int
}

// PathGap 描述某条要求路径在外部故障后仍无法满足底线的剩余缺口。
type PathGap struct {
	// PathID 是跌破底线的要求路径。
	PathID string
	// DependencyGaps 按依赖给出剩余缺口（依赖剩余容量距离底线还差多少）。
	// 只包含实际跌破的依赖。
	DependencyGaps []DependencyGap
}

// DependencyGap 是单个依赖上的剩余容量缺口。
type DependencyGap struct {
	// DependencyID 受影响依赖。
	DependencyID string
	// Remaining 是撤回尝试后的实际剩余容量。
	Remaining int
	// Floor 是该路径要求的底线。
	Floor int
	// Shortfall = Floor - Remaining，即还差多少容量才能回到底线之上。
	Shortfall int
}

// InfeasibilityReport 在任何撤回组合都无法恢复容量时给出剩余缺口。
type InfeasibilityReport struct {
	// FaultID 是触发不可行情况的外部故障。
	FaultID string
	// Gaps 是逐条要求路径的剩余缺口，只包含仍被跌破的路径。
	Gaps []PathGap
}

// FaultResult 是 InjectExternalFault 的处理结果。
type FaultResult struct {
	// FaultID 是外部故障标识。
	FaultID string
	// Recovered 表示注入后（含必要撤回）容量底线是否全部恢复。
	Recovered bool
	// Revoked 是被撤回的演练 ID 集合，按 (优先级升序, ID 升序) 定序。
	// 当不需要撤回即可恢复时为空。
	Revoked []string
	// Infeasibility 仅在 Recovered 为 false 时填充，指出剩余缺口。
	Infeasibility *InfeasibilityReport
}

// AuditEvent 是一条可核审计记录。所有准入、退出、回执、撤回与故障动作都会记录。
type AuditEvent struct {
	// Seq 是单调递增的审计序号，从 1 开始。
	Seq int64
	// Time 是事件发生时协调器的模拟时钟。
	Time time.Time
	// Kind 是事件类型。
	Kind string
	// DrillID 在与演练相关的事件中填写，否则为空。
	DrillID string
	// Generation 在与代次相关的事件中填写，否则为 0。
	Generation int64
	// Detail 是人类可读的事件说明。
	Detail string
}

// DrillSnapshot 是演练状态的只读快照。
type DrillSnapshot struct {
	// ID 演练标识。
	ID string
	// Status 当前生命周期状态。
	Status DrillStatus
	// Generation 当前代次；等待中的演练代次为 0。
	Generation int64
	// Priority 优先级。
	Priority int
	// Instances 占用的实例集合（按提交顺序保存的副本）。
	Instances []string
	// StartTime 准入时间；未准入为零值。
	StartTime time.Time
	// EndTime 计划退出时间；未准入为零值。
	EndTime time.Time
	// ReceiptReceived 标记是否已处理过本代次回执。
	ReceiptReceived bool
	// LastReceiptOutcome 记录最近一次回执结论，未收到回执时为零值 OutcomeUnknown。
	LastReceiptOutcome ReceiptOutcome
}

// Snapshot 是协调器整体状态的只读快照，供测试与观测使用。
type Snapshot struct {
	// Now 是协调器当前模拟时钟。
	Now time.Time
	// Active 是处于 ACTIVE 状态的演练快照，按 ID 排序。
	Active []DrillSnapshot
	// Waiting 是等待队列中的演练快照，按 FIFO 顺序。
	Waiting []DrillSnapshot
	// Recovering 是等待恢复证据的演练快照，按 ID 排序。
	Recovering []DrillSnapshot
	// Returned 是已完成预算归还的演练快照，按 ID 排序。
	Returned []DrillSnapshot
	// Residual 是每个依赖当前的剩余容量（已扣除外部故障与在持预算）。
	Residual map[string]int
	// ExternalReduction 是每个依赖累计的外部故障削减量。
	ExternalReduction map[string]int
	// NextGeneration 是下一个准入演练将获得的代次。
	NextGeneration int64
}
