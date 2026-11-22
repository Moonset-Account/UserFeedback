package faultcoordinator

import (
	"errors"
	"fmt"
	"sort"
	"sync"
	"time"
)

// 哨兵错误：调用方可用 errors.Is 判定具体失败原因。
var (
	// ErrInvalidTopology 表示拓扑定义不合法（重复 ID、引用缺失、非正负载等）。
	ErrInvalidTopology = errors.New("invalid topology")
	// ErrInvalidArgument 表示入参不合法。
	ErrInvalidArgument = errors.New("invalid argument")
	// ErrDuplicateDrill 表示演练 ID 已存在且尚未完成全部归还流程。
	ErrDuplicateDrill = errors.New("drill id already in use")
	// ErrUnknownDrill 表示演练 ID 不存在。
	ErrUnknownDrill = errors.New("unknown drill")
	// ErrNotActive 表示演练当前不处于 ACTIVE 状态，无法执行该操作。
	ErrNotActive = errors.New("drill is not active")
	// ErrDuplicateFault 表示外部故障 ID 重复。
	ErrDuplicateFault = errors.New("fault id already used")
	// ErrUnknownDependency 表示引用了拓扑中不存在的依赖。
	ErrUnknownDependency = errors.New("unknown dependency")
)

// baseTime 是协调器模拟时钟的固定起点，保证测试可复现。
var baseTime = time.Date(2026, 11, 23, 0, 0, 0, 0, time.UTC)

// drillState 是协调器内部保存的演练运行态。
type drillState struct {
	drill              Drill
	status             DrillStatus
	generation         int64
	startTime          time.Time
	endTime            time.Time
	loads              map[string]int // 该演练在每个依赖上持有的容量预算
	receiptReceived    bool
	lastReceiptOutcome ReceiptOutcome
	// revoked 表示演练因外部故障被撤回：预算与实例立即释放，代次随之关闭。
	revoked bool
}

// Coordinator 是故障场景协调器，所有方法均为并发安全的。
type Coordinator struct {
	mu sync.Mutex

	topo    Topology
	now     time.Time
	drills  map[string]*drillState
	waiting []string // FIFO 等待队列，保存演练 ID

	// instanceOwner 记录实例当前被哪个演练持有（ACTIVE 与未归还的 RECOVERING）。
	instanceOwner map[string]string

	externalReduction map[string]int
	faultIDs          map[string]bool

	nextGen  int64
	auditSeq int64
	audit    []AuditEvent

	// 拓扑预计算结果。
	depCapacity    map[string]int
	instanceRoutes map[string]map[string]int
	pathDeps       map[string][]string
	pathFloor      map[string]int
}

// NewCoordinator 校验拓扑并创建协调器，模拟时钟从固定起点开始。
func NewCoordinator(topo Topology) (*Coordinator, error) {
	c := &Coordinator{
		topo:              topo,
		now:               baseTime,
		drills:            map[string]*drillState{},
		instanceOwner:     map[string]string{},
		externalReduction: map[string]int{},
		faultIDs:          map[string]bool{},
		nextGen:           1,
		auditSeq:          0,
		depCapacity:       map[string]int{},
		instanceRoutes:    map[string]map[string]int{},
		pathDeps:          map[string][]string{},
		pathFloor:         map[string]int{},
	}
	if err := c.validateTopology(); err != nil {
		return nil, err
	}
	return c, nil
}

func (c *Coordinator) validateTopology() error {
	if len(c.topo.Dependencies) == 0 {
		return fmt.Errorf("%w: at least one dependency is required", ErrInvalidTopology)
	}
	seenDep := map[string]bool{}
	for _, d := range c.topo.Dependencies {
		if d.ID == "" {
			return fmt.Errorf("%w: dependency has empty id", ErrInvalidTopology)
		}
		if seenDep[d.ID] {
			return fmt.Errorf("%w: duplicate dependency id %q", ErrInvalidTopology, d.ID)
		}
		if d.Capacity < 0 {
			return fmt.Errorf("%w: dependency %q has negative capacity", ErrInvalidTopology, d.ID)
		}
		seenDep[d.ID] = true
		c.depCapacity[d.ID] = d.Capacity
	}
	if len(c.topo.Instances) == 0 {
		return fmt.Errorf("%w: at least one instance is required", ErrInvalidTopology)
	}
	seenInst := map[string]bool{}
	for _, in := range c.topo.Instances {
		if in.ID == "" {
			return fmt.Errorf("%w: instance has empty id", ErrInvalidTopology)
		}
		if seenInst[in.ID] {
			return fmt.Errorf("%w: duplicate instance id %q", ErrInvalidTopology, in.ID)
		}
		seenInst[in.ID] = true
		routes := map[string]int{}
		for _, r := range in.Routes {
			if !seenDep[r.DependencyID] {
				return fmt.Errorf("%w: instance %q routes unknown dependency %q",
					ErrInvalidTopology, in.ID, r.DependencyID)
			}
			if r.Load <= 0 {
				return fmt.Errorf("%w: instance %q route to %q must have positive load",
					ErrInvalidTopology, in.ID, r.DependencyID)
			}
			if _, dup := routes[r.DependencyID]; dup {
				return fmt.Errorf("%w: instance %q lists dependency %q more than once",
					ErrInvalidTopology, in.ID, r.DependencyID)
			}
			routes[r.DependencyID] = r.Load
		}
		c.instanceRoutes[in.ID] = routes
	}
	if len(c.topo.RequiredPaths) == 0 {
		return fmt.Errorf("%w: at least one required path is required", ErrInvalidTopology)
	}
	seenPath := map[string]bool{}
	for _, p := range c.topo.RequiredPaths {
		if p.ID == "" {
			return fmt.Errorf("%w: required path has empty id", ErrInvalidTopology)
		}
		if seenPath[p.ID] {
			return fmt.Errorf("%w: duplicate required path id %q", ErrInvalidTopology, p.ID)
		}
		seenPath[p.ID] = true
		if p.Floor < 0 {
			return fmt.Errorf("%w: path %q has negative floor", ErrInvalidTopology, p.ID)
		}
		dedup := map[string]bool{}
		var deps []string
		for _, id := range p.DependencyIDs {
			if !seenDep[id] {
				return fmt.Errorf("%w: path %q references unknown dependency %q",
					ErrInvalidTopology, p.ID, id)
			}
			if !dedup[id] {
				dedup[id] = true
				deps = append(deps, id)
			}
		}
		sort.Strings(deps)
		c.pathDeps[p.ID] = deps
		c.pathFloor[p.ID] = p.Floor
	}
	return nil
}

// drillLoads 汇总演练在每个依赖上的总容量需求，并校验实例集合。
func (c *Coordinator) drillLoads(d Drill) (map[string]int, error) {
	if d.ID == "" {
		return nil, fmt.Errorf("%w: drill id is empty", ErrInvalidArgument)
	}
	if d.Duration <= 0 {
		return nil, fmt.Errorf("%w: drill %q duration must be positive", ErrInvalidArgument, d.ID)
	}
	if len(d.Instances) == 0 {
		return nil, fmt.Errorf("%w: drill %q must occupy at least one instance", ErrInvalidArgument, d.ID)
	}
	loads := map[string]int{}
	seen := map[string]bool{}
	for _, instID := range d.Instances {
		if seen[instID] {
			return nil, fmt.Errorf("%w: drill %q lists instance %q more than once",
				ErrInvalidArgument, d.ID, instID)
		}
		seen[instID] = true
		routes, ok := c.instanceRoutes[instID]
		if !ok {
			return nil, fmt.Errorf("%w: drill %q references unknown instance %q",
				ErrInvalidArgument, d.ID, instID)
		}
		for dep, load := range routes {
			loads[dep] += load
		}
	}
	return loads, nil
}

// heldLoads 计算当前仍在持有影响预算的演练（ACTIVE 与未撤回的 RECOVERING）
// 在每个依赖上的总占用；ignore 中的演练视为已撤回而跳过。
func (c *Coordinator) heldLoads(ignore map[string]bool) map[string]int {
	used := map[string]int{}
	for id, st := range c.drills {
		if ignore[id] {
			continue
		}
		if st.status != StatusActive && !(st.status == StatusRecovering && !st.revoked) {
			continue
		}
		for dep, load := range st.loads {
			used[dep] += load
		}
	}
	return used
}

// residualIgnoring 计算给定撤回集合下每个依赖的剩余容量。
func (c *Coordinator) residualIgnoring(ignore map[string]bool) map[string]int {
	used := c.heldLoads(ignore)
	residual := map[string]int{}
	for dep, cap_ := range c.depCapacity {
		residual[dep] = cap_ - c.externalReduction[dep] - used[dep]
	}
	return residual
}

// feasibleIgnoring 判断在撤回 ignore 中的演练后，全部要求路径是否仍满足底线。
func (c *Coordinator) feasibleIgnoring(ignore map[string]bool) bool {
	residual := c.residualIgnoring(ignore)
	for pathID, deps := range c.pathDeps {
		floor := c.pathFloor[pathID]
		for _, dep := range deps {
			if residual[dep] < floor {
				return false
			}
		}
	}
	return true
}

// instancesFree 判断候选实例当前是否都未被持有。
func (c *Coordinator) instancesFree(instances []string) bool {
	for _, inst := range instances {
		if _, taken := c.instanceOwner[inst]; taken {
			return false
		}
	}
	return true
}

// canAdmit 判断在当前持有量上叠加候选演练后，是否仍满足全部底线与实例互斥。
func (c *Coordinator) canAdmit(st *drillState) bool {
	if !c.instancesFree(st.drill.Instances) {
		return false
	}
	used := c.heldLoads(nil)
	for dep, load := range st.loads {
		used[dep] += load
	}
	for pathID, deps := range c.pathDeps {
		floor := c.pathFloor[pathID]
		for _, dep := range deps {
			residual := c.depCapacity[dep] - c.externalReduction[dep] - used[dep]
			if residual < floor {
				return false
			}
		}
	}
	return true
}

func (c *Coordinator) log(kind, drillID string, generation int64, detail string) {
	c.auditSeq++
	c.audit = append(c.audit, AuditEvent{
		Seq:        c.auditSeq,
		Time:       c.now,
		Kind:       kind,
		DrillID:    drillID,
		Generation: generation,
		Detail:     detail,
	})
}

// admit 在调用方已持锁且 canAdmit 通过的前提下完成准入。
func (c *Coordinator) admit(st *drillState) {
	st.status = StatusActive
	st.generation = c.nextGen
	c.nextGen++
	st.startTime = c.now
	st.endTime = c.now.Add(st.drill.Duration)
	for _, inst := range st.drill.Instances {
		c.instanceOwner[inst] = st.drill.ID
	}
	c.log("DRILL_ADMITTED", st.drill.ID, st.generation,
		fmt.Sprintf("drill admitted with %d instance(s), ends at %s",
			len(st.drill.Instances), st.endTime.Format(time.RFC3339)))
}

// pumpWaiting 按 FIFO 顺序尝试准入等待队列；队头不满足则停止（队头阻塞），
// 队头之后的等待者即使自身可准入也继续保留，严格维持提交次序。
func (c *Coordinator) pumpWaiting() {
	remaining := make([]string, 0, len(c.waiting))
	blocked := false
	for _, id := range c.waiting {
		st := c.drills[id]
		if st == nil || st.status != StatusWaiting {
			continue // 防御性清理，正常流程不会发生。
		}
		if blocked {
			remaining = append(remaining, id)
			continue
		}
		if !c.canAdmit(st) {
			blocked = true
			remaining = append(remaining, id)
			continue
		}
		c.admit(st)
	}
	c.waiting = remaining
}

// Submit 提交一次演练：满足全部容量底线则立即准入，否则进入 FIFO 等待队列。
// 已完成归还（RETURNED）的演练 ID 可以被重新提交，重新准入时获得新代次；
// 仍处于 WAITING/ACTIVE/RECOVERING 的 ID 重复提交返回 ErrDuplicateDrill。
func (c *Coordinator) Submit(d Drill) (DrillSnapshot, error) {
	loads, err := c.drillLoads(d)
	if err != nil {
		return DrillSnapshot{}, err
	}
	c.mu.Lock()
	defer c.mu.Unlock()

	if existing, ok := c.drills[d.ID]; ok && existing.status != StatusReturned {
		return DrillSnapshot{}, fmt.Errorf("%w: drill %q is %s",
			ErrDuplicateDrill, d.ID, existing.status)
	}
	st := &drillState{
		drill:              d,
		status:             StatusWaiting,
		loads:              loads,
		lastReceiptOutcome: OutcomeUnknown,
	}
	c.drills[d.ID] = st
	if c.canAdmit(st) {
		c.admit(st)
	} else {
		c.waiting = append(c.waiting, d.ID)
		c.log("DRILL_QUEUED", d.ID, 0,
			"capacity floor or instance conflict blocks admission; drill queued (FIFO)")
	}
	c.pumpWaiting()
	return c.snapshotDrill(st), nil
}

// Exit 让 ACTIVE 演练主动退出：状态转为 RECOVERING，但影响预算继续持有，
// 直到收到本次代次的健康恢复证据。
func (c *Coordinator) Exit(drillID string) (DrillSnapshot, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	st, ok := c.drills[drillID]
	if !ok {
		return DrillSnapshot{}, fmt.Errorf("%w: %q", ErrUnknownDrill, drillID)
	}
	if st.status != StatusActive {
		return DrillSnapshot{}, fmt.Errorf("%w: drill %q is %s", ErrNotActive, drillID, st.status)
	}
	st.status = StatusRecovering
	c.log("DRILL_EXITED", drillID, st.generation,
		"drill exited; impact budget remains held until matching healthy receipt arrives")
	// 退出不释放预算，因此不触发等待队列准入。
	return c.snapshotDrill(st), nil
}

// Advance 推进模拟时钟；到达计划退出时间的 ACTIVE 演练自动退出（规则同 Exit）。
// 自动退出同样继续持有预算，等待健康恢复证据。
func (c *Coordinator) Advance(d time.Duration) (time.Time, []string) {
	if d <= 0 {
		c.mu.Lock()
		now := c.now
		c.mu.Unlock()
		return now, nil
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.now = c.now.Add(d)
	var autoExited []string
	for _, st := range c.drills {
		if st.status == StatusActive && !st.endTime.After(c.now) {
			st.status = StatusRecovering
			autoExited = append(autoExited, st.drill.ID)
			c.log("DRILL_AUTO_EXITED", st.drill.ID, st.generation,
				"planned duration elapsed; drill auto-exited, budget still held")
		}
	}
	sort.Strings(autoExited)
	return c.now, autoExited
}

// ReceiveReceipt 处理健康恢复证据。只有代次匹配、类型为健康、且是该代次第一份
// 回执时才归还影响预算；过期、取消、重复、未知回执均留下审计记录且不产生副作用。
func (c *Coordinator) ReceiveReceipt(r Receipt) (ReceiptResult, error) {
	if r.DrillID == "" {
		return ReceiptResult{}, fmt.Errorf("%w: receipt has empty drill id", ErrInvalidArgument)
	}
	c.mu.Lock()
	defer c.mu.Unlock()

	res := ReceiptResult{DrillID: r.DrillID, Generation: r.Generation, Kind: r.Kind}
	st, ok := c.drills[r.DrillID]
	if !ok {
		res.Outcome = OutcomeUnknown
		res.Detail = "no drill matches the receipt; recorded only"
		c.log("RECEIPT_UNKNOWN", r.DrillID, r.Generation, res.Detail)
		return res, nil
	}

	// 代次不匹配：过期/超前回执；被撤回演练的代次也已关闭，同样按过期登记。
	if r.Generation != st.generation {
		res.Outcome = OutcomeExpired
		res.Detail = fmt.Sprintf("stale generation: receipt gen=%d, current gen=%d",
			r.Generation, st.generation)
		st.lastReceiptOutcome = OutcomeExpired
		c.log("RECEIPT_EXPIRED", r.DrillID, r.Generation, res.Detail)
		return res, nil
	}

	// 同一代次的第一份回执之后到达的任何回执均视为重复，锁定不再产生副作用。
	if st.receiptReceived {
		res.Outcome = OutcomeDuplicate
		res.Detail = fmt.Sprintf("generation %d already has a processed receipt; duplicate recorded",
			r.Generation)
		st.lastReceiptOutcome = OutcomeDuplicate
		c.log("RECEIPT_DUPLICATE", r.DrillID, r.Generation, res.Detail)
		return res, nil
	}

	st.receiptReceived = true
	switch r.Kind {
	case ReceiptCanceled:
		st.status = StatusRecovering
		st.lastReceiptOutcome = OutcomeCanceled
		res.Outcome = OutcomeCanceled
		res.Detail = "recovery canceled; impact budget remains held"
		c.log("RECEIPT_CANCELED", r.DrillID, r.Generation, res.Detail)
		return res, nil
	case ReceiptHealthy:
		// 被撤回的演练代次已关闭：匹配代次的健康回执也不能再次归还（预算早已释放）。
		if st.revoked {
			res.Outcome = OutcomeExpired
			res.Detail = "drill was revoked; its generation is closed and budget was already released"
			st.lastReceiptOutcome = OutcomeExpired
			c.log("RECEIPT_EXPIRED", r.DrillID, r.Generation, res.Detail)
			return res, nil
		}
		st.status = StatusReturned
		st.lastReceiptOutcome = OutcomeAccepted
		res.Outcome = OutcomeAccepted
		res.Detail = "healthy recovery evidence accepted; impact budget returned"
		c.log("DRILL_RETURNED", r.DrillID, r.Generation, res.Detail)
		for _, inst := range st.drill.Instances {
			if c.instanceOwner[inst] == st.drill.ID {
				delete(c.instanceOwner, inst)
			}
		}
		c.pumpWaiting()
		return res, nil
	default:
		st.receiptReceived = false // 未知类型不占用“首份回执”名额。
		return ReceiptResult{}, fmt.Errorf("%w: unknown receipt kind %d", ErrInvalidArgument, r.Kind)
	}
}

// sortedActiveDrills 返回按 (优先级升序, ID 升序) 定序的 ACTIVE 演练。
func (c *Coordinator) sortedActiveDrills() []*drillState {
	var active []*drillState
	for _, st := range c.drills {
		if st.status == StatusActive {
			active = append(active, st)
		}
	}
	sort.Slice(active, func(i, j int) bool {
		if active[i].drill.Priority != active[j].drill.Priority {
			return active[i].drill.Priority < active[j].drill.Priority
		}
		return active[i].drill.ID < active[j].drill.ID
	})
	return active
}

// combinations 按字典序枚举 n 个元素的全部 k 元组合（元素为下标）。
func combinations(n, k int) [][]int {
	if k <= 0 || k > n {
		return nil
	}
	var out [][]int
	combo := make([]int, k)
	var rec func(start, idx int)
	rec = func(start, idx int) {
		if idx == k {
			cp := make([]int, k)
			copy(cp, combo)
			out = append(out, cp)
			return
		}
		for i := start; i <= n-(k-idx); i++ {
			combo[idx] = i
			rec(i+1, idx+1)
		}
	}
	rec(0, 0)
	return out
}

// InjectExternalFault 在演练之间插入外部模拟故障。
// 若故障后跌破底线，枚举寻找能恢复可行性的最少撤回集合；同数量时按
// (优先级升序, ID 升序) 的字典序选择。任何组合都无法恢复时返回剩余缺口。
func (c *Coordinator) InjectExternalFault(f ExternalFault) (FaultResult, error) {
	if f.ID == "" {
		return FaultResult{}, fmt.Errorf("%w: fault has empty id", ErrInvalidArgument)
	}
	if f.Reduction <= 0 {
		return FaultResult{}, fmt.Errorf("%w: fault %q reduction must be positive",
			ErrInvalidArgument, f.ID)
	}
	if _, ok := c.depCapacity[f.DependencyID]; !ok {
		return FaultResult{}, fmt.Errorf("%w: fault %q references %q",
			ErrUnknownDependency, f.ID, f.DependencyID)
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.faultIDs[f.ID] {
		return FaultResult{}, fmt.Errorf("%w: fault %q", ErrDuplicateFault, f.ID)
	}
	c.faultIDs[f.ID] = true
	c.externalReduction[f.DependencyID] += f.Reduction

	result := FaultResult{FaultID: f.ID}
	if c.feasibleIgnoring(nil) {
		result.Recovered = true
		c.log("FAULT_APPLIED", f.ID, 0, fmt.Sprintf(
			"external fault reduced dependency %q capacity by %d; floors still hold",
			f.DependencyID, f.Reduction))
		return result, nil
	}

	active := c.sortedActiveDrills()
	n := len(active)
	var chosen []*drillState
	for k := 1; k <= n; k++ {
		for _, idx := range combinations(n, k) {
			ignore := make(map[string]bool, k)
			for _, i := range idx {
				ignore[active[i].drill.ID] = true
			}
			if c.feasibleIgnoring(ignore) {
				for _, i := range idx {
					chosen = append(chosen, active[i])
				}
				break
			}
		}
		if chosen != nil {
			break
		}
	}

	if chosen == nil {
		// 任何撤回组合都无法恢复：不做部分撤回，如实报告当前持有下的剩余缺口。
		result.Recovered = false
		result.Infeasibility = c.buildGapReport(f.ID)
		c.log("FAULT_INFEASIBLE", f.ID, 0, fmt.Sprintf(
			"no withdrawal set restores floors; %d path(s) still breached",
			len(result.Infeasibility.Gaps)))
		return result, nil
	}

	for _, st := range chosen {
		st.status = StatusRecovering
		st.revoked = true
		for _, inst := range st.drill.Instances {
			if c.instanceOwner[inst] == st.drill.ID {
				delete(c.instanceOwner, inst)
			}
		}
		result.Revoked = append(result.Revoked, st.drill.ID)
		c.log("FAULT_REVOKED", st.drill.ID, st.generation, fmt.Sprintf(
			"drill revoked to restore feasibility after fault %q; generation %d closed",
			f.ID, st.generation))
	}
	result.Recovered = true
	c.log("FAULT_APPLIED", f.ID, 0, fmt.Sprintf(
		"external fault on %q absorbed by revoking %d drill(s)", f.DependencyID, len(chosen)))
	c.pumpWaiting()
	return result, nil
}

// buildGapReport 在调用方持锁时计算逐路径剩余缺口（仅包含仍被跌破的路径/依赖）。
func (c *Coordinator) buildGapReport(faultID string) *InfeasibilityReport {
	residual := c.residualIgnoring(nil)
	report := &InfeasibilityReport{FaultID: faultID}
	pathIDs := make([]string, 0, len(c.pathDeps))
	for p := range c.pathDeps {
		pathIDs = append(pathIDs, p)
	}
	sort.Strings(pathIDs)
	for _, pathID := range pathIDs {
		floor := c.pathFloor[pathID]
		pg := PathGap{PathID: pathID}
		deps := append([]string(nil), c.pathDeps[pathID]...)
		sort.Strings(deps)
		for _, dep := range deps {
			if residual[dep] < floor {
				pg.DependencyGaps = append(pg.DependencyGaps, DependencyGap{
					DependencyID: dep,
					Remaining:    residual[dep],
					Floor:        floor,
					Shortfall:    floor - residual[dep],
				})
			}
		}
		if len(pg.DependencyGaps) > 0 {
			report.Gaps = append(report.Gaps, pg)
		}
	}
	return report
}

// Now 返回协调器当前模拟时钟。
func (c *Coordinator) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

// AuditLog 返回审计记录的副本，序号单调递增。
func (c *Coordinator) AuditLog() []AuditEvent {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]AuditEvent, len(c.audit))
	copy(out, c.audit)
	return out
}

// Drill 返回指定演练的状态快照；不存在时 ok 为 false。
func (c *Coordinator) Drill(id string) (DrillSnapshot, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	st, ok := c.drills[id]
	if !ok {
		return DrillSnapshot{}, false
	}
	return c.snapshotDrill(st), true
}

func (c *Coordinator) snapshotDrill(st *drillState) DrillSnapshot {
	instances := append([]string(nil), st.drill.Instances...)
	return DrillSnapshot{
		ID:                 st.drill.ID,
		Status:             st.status,
		Generation:         st.generation,
		Priority:           st.drill.Priority,
		Instances:          instances,
		StartTime:          st.startTime,
		EndTime:            st.endTime,
		ReceiptReceived:    st.receiptReceived,
		LastReceiptOutcome: st.lastReceiptOutcome,
	}
}

// Snapshot 返回协调器整体状态的只读快照。
func (c *Coordinator) Snapshot() Snapshot {
	c.mu.Lock()
	defer c.mu.Unlock()

	snap := Snapshot{
		Now:               c.now,
		Residual:          c.residualIgnoring(nil),
		ExternalReduction: map[string]int{},
		NextGeneration:    c.nextGen,
	}
	for dep, red := range c.externalReduction {
		snap.ExternalReduction[dep] = red
	}
	// 等待队列严格按 FIFO。
	for _, id := range c.waiting {
		if st := c.drills[id]; st != nil {
			snap.Waiting = append(snap.Waiting, c.snapshotDrill(st))
		}
	}
	var ids []string
	for id := range c.drills {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		st := c.drills[id]
		s := c.snapshotDrill(st)
		switch st.status {
		case StatusActive:
			snap.Active = append(snap.Active, s)
		case StatusRecovering:
			snap.Recovering = append(snap.Recovering, s)
		case StatusReturned:
			snap.Returned = append(snap.Returned, s)
		}
	}
	return snap
}
