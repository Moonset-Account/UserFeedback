// Package backuprecovery 实现数据库检查点的低成本恢复规划。
//
// 问题背景：恢复一个检查点时，完整基础页和字节补丁分散在多个归档包中。
// 每个归档包有固定开包成本，包内每个条目有独立读取成本。页号可能被回收
// 后重新分配，同一页号的不同世代必须严格区分：补丁只适用于世代、长度和
// 完整前像摘要全部匹配的内容。
//
// 本包给出全局最优（最低总成本）的读取与打补丁方案，同价时按读取条目数、
// 再按合法操作串的字典序决胜，并支持读取中途归档包损坏后的最小追加成本重算。
package backuprecovery

// Hunk 描述一个字节补丁片段：从 Offset 开始的 Old 字节必须被 New 替换。
// 同一补丁内的多个 Hunk 区间不得重叠。
type Hunk struct {
	Offset int
	Old    []byte
	New    []byte
}

// Patch 是一条字节补丁：仅适用于世代为 Generation、页号为 PageID 的页，
// 且前像完整内容的 SHA-256 摘要必须等于 BeforeDigest、长度等于 BeforeLength。
// 成功应用后的内容摘要必须等于 AfterDigest。
type Patch struct {
	ID           string
	PageID       string
	Generation   int
	BeforeDigest []byte
	BeforeLength int
	AfterDigest  []byte
	Hunks        []Hunk
}

// BaseImage 是一个完整基础页（页的某个完整版本），可作为来源链的起点。
type BaseImage struct {
	PageID     string
	Generation int
	Digest     []byte
	Content    []byte
}

// Entry 是归档包中的一个条目，恰好引用一个基础页或一条补丁。
type Entry struct {
	ID       string
	Base     *BaseImage // 非空表示完整页条目
	PatchID  string     // 非空表示补丁条目
	ReadCost int
}

// Package 描述一个归档包：固定开包成本 OpenCost，加上每条目各自的读取成本。
type Package struct {
	ID       string
	OpenCost int
	Entries  []Entry
}

// Target 描述一个待恢复目标页及其期望内容摘要与长度。
type Target struct {
	PageID     string
	Generation int
	Digest     []byte
	Length     int
}

// SourceStep 是来源链中的一步。Kind 为 "base" 或 "patch"。
//   - base 步骤：Base 非空，给出完整页来源；
//   - patch 步骤：Patch 非空，给出补丁来源及其前像/后像摘要。
type SourceStep struct {
	Kind   string
	Base   *BaseImage
	Patch  *Patch
	From   []byte // 应用前内容摘要（base 步骤为 nil）
	To     []byte // 应用后内容摘要
	Length int    // 应用后内容长度
}

// SourceChain 是单个目标页的来源链：从一个基础页出发，顺序应用若干补丁。
// 链上所有补丁都与目标页世代一致，且全部经过真实应用校验。
type SourceChain struct {
	PageID     string
	Generation int
	Steps      []SourceStep
	ReadCost   int // 本链条目读取成本之和
	OpenCost   int // 本链所需包的开包成本之和（不含共享减免）
	Packages   []string
	EntryCount int
}

// Plan 是全部目标页的最低成本恢复方案。
type Plan struct {
	Chains     []SourceChain
	TotalCost  int
	OpenCost   int // 所有实际打开包的开包成本之和
	ReadCost   int // 所有去重条目读取成本之和
	Packages   []string
	EntryCount int
	Operations []string // 合法操作串（满足依赖的字典序最小拓扑序）
}

// MissingBeforeImage 描述一个无法满足的前像需求。
type MissingBeforeImage struct {
	PageID             string
	Generation         int
	RequiredDigest     []byte
	AfterDigest        []byte
	HasOtherGeneration bool // 是否存在另一世代的同摘要内容（不能作为补丁起点）
}

// ImpossibleError 表示至少有一个目标页在给定输入下不可能恢复。
type ImpossibleError struct {
	Missing []MissingBeforeImage
	// Reason 给出结构性原因，例如世代不匹配或超过每页数版本上限。
	Reason string
}

func (e *ImpossibleError) Error() string {
	if e == nil {
		return "backuprecovery: impossible target"
	}
	if len(e.Missing) == 0 {
		return "backuprecovery: 目标不可恢复: " + e.Reason
	}
	return "backuprecovery: 目标不可恢复，缺少 " + itoa(len(e.Missing)) + " 个前像"
}

// Corruption 描述读取中途发现的归档包损坏。
type Corruption struct {
	PackageID string
	// EntryID 为最先发现损坏的条目；为空表示开包阶段即损坏。
	EntryID string
	Reason  string
}

// ExecProgress 是执行器在损坏发生点之前已经完成并校验通过的进度。
type ExecProgress struct {
	CompletedOps   []string
	OpenedPackages []string
	ReadEntries    []string
	Verified       []VerifiedPage
	SpentCost      int
}

// VerifiedPage 是一个已经过完整校验的中间页，损坏重算时可作为自由起点。
type VerifiedPage struct {
	PageID     string
	Generation int
	Digest     []byte
	Content    []byte
}

// RecoveryPlan 是损坏发生后的最小追加成本方案。
type RecoveryPlan struct {
	// Additional 是在已付成本之外还需追加的最小成本。
	Additional int
	// Cumulative 是已发生成本与追加成本之和。
	Cumulative int
	// Retained 是保留下来的已校验中间页。
	Retained []VerifiedPage
	// AdditionalOps 是追加的合法操作串（RETAIN 操作记录保留的中间页）。
	AdditionalOps []string
	// Chains 是追加方案中各目标页的完整来源链（含 RETAIN 起点）。
	Chains     []SourceChain
	Packages   []string
	EntryCount int
	OpenCost   int // 追加方案中新开包的开包成本
	ReadCost   int // 追加方案中新读取条目的成本
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	neg := n < 0
	if neg {
		n = -n
	}
	var buf [20]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	if neg {
		i--
		buf[i] = '-'
	}
	return string(buf[i:])
}
