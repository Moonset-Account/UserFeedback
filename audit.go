// Package modaudit 是一个离线审查库，用于核对特权服务的模块最终会从哪个目录加载。
//
// 输入：服务清单（批准摘要）、调用方模块及其依赖、按启动配置排序的候选目录，
// 以及目录信任、目录内容（模块摘要）与兼容性快照。
//
// 核心规则：
//  1. 解析某模块的依赖时，有效候选目录 = 链上各模块追加的目录（近者优先，仅对追加者
//     自身及后代生效）+ 启动配置目录（按配置顺序）。
//  2. 按顺序扫描，首个兼容的模块文件即为选中项；它必须来自可信目录，且摘要命中该服务
//     的批准清单。搜索后续位置的合规副本不能作为放行依据。
//  3. 按配置分别查完传递依赖，报告首个有风险的来源链（深度优先、按依赖声明顺序）。
//  4. 若选中位置之前存在尚未核实、且足以改变选择的候选目录，结果标为待补证。
package modaudit

import "fmt"

// Digest 模块文件摘要。
type Digest string

// Module 调用方模块，或目录快照中记录的一个模块文件。
type Module struct {
	Name    string
	Version string
	Digest  Digest
	// Deps 依赖模块名，按声明顺序；首个风险链按此顺序深度优先确定。
	Deps []string
	// AddDirs 追加的候选目录，仅对本模块自身及其后代（传递依赖）生效。
	AddDirs []string
}

// ServiceManifest 服务清单：该服务批准放行的模块摘要。
type ServiceManifest struct {
	Name string
	// Approved 模块名 -> 批准摘要清单。
	Approved map[string][]Digest
}

// Config 一种启动配置：按启动顺序排列的候选目录。
type Config struct {
	Name string
	Dirs []string
}

// Input 审查输入快照。
type Input struct {
	Service ServiceManifest
	// Callers 调用方模块（解析入口，自身不参与合规检查）。
	Callers []Module
	Configs []Config
	// Trusted 目录信任快照：true 表示可信目录。
	Trusted map[string]bool
	// Dirs 目录内容快照：目录 -> 已核实的模块文件列表。
	Dirs map[string][]Module
	// Unverified 尚未核实的目录：true 表示无可靠快照。
	Unverified map[string]bool
	// Compat 兼容性快照：需求方模块名 -> 依赖名 -> 兼容版本列表。
	Compat map[string]map[string][]string
}

// Step 来源链上的一环：从某目录选中的模块文件。入口调用方的 Dir 为空。
type Step struct {
	Dir     string
	Module  string
	Version string
	Digest  Digest
}

// Reason 风险原因。
type Reason string

const (
	ReasonUntrustedDir      Reason = "untrusted_dir"       // 首个兼容模块来自不可信目录
	ReasonDigestNotApproved Reason = "digest_not_approved" // 摘要不在服务批准清单
	ReasonUnresolved        Reason = "unresolved"          // 已核实目录中无兼容模块
)

// Finding 首个有风险的来源链。
type Finding struct {
	Chain  []Step // 从调用方到风险来源的完整链
	Reason Reason
	Detail string
}

// Resolution 一次依赖解析的选中结果。
type Resolution struct {
	Requirer string // 需求方模块名
	Dep      string // 依赖名
	Step     Step   // 选中的模块文件
}

// Status 单配置审查结论。
type Status string

const (
	StatusOK      Status = "ok"               // 全部合规
	StatusRisk    Status = "risk"             // 存在有风险的来源链
	StatusPending Status = "pending_evidence" // 存在足以改变选择的未核实早期候选
)

// ConfigReport 一种启动配置的审查报告。
type ConfigReport struct {
	Config      string
	Status      Status
	Finding     *Finding     // 首个有风险的来源链；无风险时为 nil
	Resolutions []Resolution // 本次配置下全部依赖的选中结果
	// Pending 是否存在足以改变选择的未核实早期候选。
	Pending     bool
	PendingDirs []string
}

// Audit 对每种启动配置分别查完传递依赖并给出审查报告。
func Audit(in Input) []ConfigReport {
	reports := make([]ConfigReport, 0, len(in.Configs))
	for _, cfg := range in.Configs {
		r := &resolver{
			in:         in,
			cfg:        cfg,
			pendingSet: map[string]bool{},
			visited:    map[string]bool{},
		}
		var first *Finding
		for _, caller := range in.Callers {
			key := caller.Name + "@" + caller.Version
			if r.visited[key] {
				continue
			}
			r.visited[key] = true
			root := Step{Module: caller.Name, Version: caller.Version, Digest: caller.Digest}
			scoped := append([]string(nil), caller.AddDirs...)
			if f := r.resolveDeps([]Step{root}, caller.Name, scoped, caller.Deps); f != nil && first == nil {
				first = f
			}
		}
		status := StatusOK
		if first != nil {
			status = StatusRisk
		}
		if len(r.pending) > 0 {
			// 存在足以改变选择的未核实早期候选：结论（含风险判定）尚待补证。
			status = StatusPending
		}
		reports = append(reports, ConfigReport{
			Config:      cfg.Name,
			Status:      status,
			Finding:     first,
			Resolutions: r.resolutions,
			Pending:     len(r.pending) > 0,
			PendingDirs: r.pending,
		})
	}
	return reports
}

type resolver struct {
	in          Input
	cfg         Config
	resolutions []Resolution
	pending     []string
	pendingSet  map[string]bool
	visited     map[string]bool
}

func (r *resolver) markPending(dir string) {
	if !r.pendingSet[dir] {
		r.pendingSet[dir] = true
		r.pending = append(r.pending, dir)
	}
}

// resolveDeps 解析某模块的全部依赖；返回首个风险链，但继续查完其余依赖。
func (r *resolver) resolveDeps(chain []Step, requirer string, scoped []string, deps []string) *Finding {
	var first *Finding
	for _, dep := range deps {
		if f := r.resolveOne(chain, requirer, scoped, dep); f != nil && first == nil {
			first = f
		}
	}
	return first
}

// resolveOne 在有效候选目录中解析单个依赖：链上追加目录（近者优先）+ 配置目录。
func (r *resolver) resolveOne(chain []Step, requirer string, scoped []string, dep string) *Finding {
	dirs := effectiveDirs(scoped, r.cfg.Dirs)
	var (
		found    *Module
		foundDir string
		earlier  []string // 选中位置之前的未核实目录
	)
	for _, d := range dirs {
		if r.in.Unverified[d] {
			earlier = append(earlier, d)
			continue
		}
		files := r.in.Dirs[d]
		for i := range files {
			m := files[i]
			if m.Name != dep {
				continue
			}
			if !compatible(r.in.Compat, requirer, dep, m.Version) {
				continue
			}
			found = &m
			foundDir = d
			break
		}
		if found != nil {
			break
		}
	}

	if found == nil {
		// 未解析：任一未核实目录都可能藏有兼容副本，足以改变结论。
		for _, d := range earlier {
			r.markPending(d)
		}
		return &Finding{
			Chain:  extend(chain, Step{Module: dep}),
			Reason: ReasonUnresolved,
			Detail: fmt.Sprintf("依赖 %q 在已核实目录中无兼容模块（需求方 %q）", dep, requirer),
		}
	}

	// 选中位置之前的未核实目录可能提供更早的兼容副本，足以改变选择。
	for _, d := range earlier {
		r.markPending(d)
	}

	step := Step{Dir: foundDir, Module: found.Name, Version: found.Version, Digest: found.Digest}
	r.resolutions = append(r.resolutions, Resolution{Requirer: requirer, Dep: dep, Step: step})

	// 首个兼容模块必须来自可信目录。
	if !r.in.Trusted[foundDir] {
		return &Finding{
			Chain:  extend(chain, step),
			Reason: ReasonUntrustedDir,
			Detail: fmt.Sprintf("依赖 %q 的首个兼容模块来自不可信目录 %q", dep, foundDir),
		}
	}
	// 摘要必须命中该服务的批准清单。
	if !approved(r.in.Service.Approved, dep, found.Digest) {
		return &Finding{
			Chain:  extend(chain, step),
			Reason: ReasonDigestNotApproved,
			Detail: fmt.Sprintf("依赖 %q 的摘要 %q 不在服务 %q 的批准清单中", dep, found.Digest, r.in.Service.Name),
		}
	}

	// 合规：继续查传递依赖。追加目录仅对该模块自身及后代生效。
	key := found.Name + "@" + found.Version
	if r.visited[key] {
		return nil
	}
	r.visited[key] = true
	childScoped := append(append([]string(nil), found.AddDirs...), scoped...)
	return r.resolveDeps(extend(chain, step), found.Name, childScoped, found.Deps)
}

// effectiveDirs 有效候选目录：链上追加目录（近者优先）在前，配置目录按启动顺序在后。
func effectiveDirs(scoped, cfgDirs []string) []string {
	out := make([]string, 0, len(scoped)+len(cfgDirs))
	out = append(out, scoped...)
	out = append(out, cfgDirs...)
	return out
}

func extend(chain []Step, s Step) []Step {
	out := make([]Step, len(chain)+1)
	copy(out, chain)
	out[len(chain)] = s
	return out
}

func compatible(compat map[string]map[string][]string, requirer, dep, version string) bool {
	for _, v := range compat[requirer][dep] {
		if v == version {
			return true
		}
	}
	return false
}

func approved(list map[string][]Digest, dep string, d Digest) bool {
	for _, x := range list[dep] {
		if x == d {
			return true
		}
	}
	return false
}
