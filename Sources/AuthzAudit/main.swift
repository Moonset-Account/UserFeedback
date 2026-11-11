import Foundation

// MARK: - 输入模型（合成数据，全部为离线 JSON）

struct Input: Codable {
    let evaluationTime: Int
    let sync: SyncConfig
    let users: [User]
    let groups: [GroupNode]
    let memberships: [Membership]
    let grants: [Grant]
    let revocations: [Revocation]
    let observations: [Observation]
    let snapshots: [SnapshotInfo]
}

struct SyncConfig: Codable {
    let watermark: Int          // 同步水位：同步系统已处理到该时间点的事件
    let periodSeconds: Int      // 撤权事件生效宽限期（同步期限）
}

struct User: Codable { let id: String; let displayName: String }
struct GroupNode: Codable { let id: String; let displayName: String }

/// parentGroup 包含 member；member 可以是用户或子组（组继承边）
struct Membership: Codable {
    let id: String
    let parentGroup: String
    let member: String
}

struct Grant: Codable {
    let id: String
    let subject: String            // 用户或组
    let resource: String
    let action: String
    let effect: String             // allow | deny
    let validFrom: Int
    let validUntil: Int?           // nil 表示长期有效
}

struct Revocation: Codable {
    let id: String
    let grantId: String
    let revokedAt: Int
}

struct Observation: Codable {
    let id: String                 // 观察记录/证据编号
    let user: String
    let resource: String
    let action: String
    let observed: String           // allow | deny | missing（快照缺失）
    let snapshotId: String
    let snapshotTime: Int
}

struct SnapshotInfo: Codable {
    let id: String
    let recordedAt: Int
    let present: Bool
}

// MARK: - 输出模型

struct EffectivePermission: Codable {
    let user: String
    let resource: String
    let action: String
    let decision: String           // allow | deny | none
    let matchedGrants: [String]
    let denyGrants: [String]
    let ancestry: [String]
    let revokedGrants: [String]
    let cyclicAncestry: Bool
}

struct Finding: Codable {
    let code: String
    let severity: String
    let user: String
    let resource: String
    let action: String
    let expectedDecision: String
    let observed: String
    let revocationEvent: String?
    let evidenceId: String?
    let detail: String
    let check: String
}

struct GraphCycle: Codable {
    let cycleGroups: [String]
    let membershipChain: [String]
    let detail: String
}

struct Report: Codable {
    let evaluationTime: Int
    let syncWatermark: Int
    let syncPeriodSeconds: Int
    let rules: [RuleDoc]
    let effectivePermissions: [EffectivePermission]
    let cycles: [GraphCycle]
    let findings: [Finding]
    let findingCounts: [String: Int]
}

struct RuleDoc: Codable {
    let ruleId: String
    let priority: Int
    let title: String
    let statement: String
}

// MARK: - 引擎

final class Engine {
    let input: Input

    // parentGroup -> 成员（用户/组）
    var childrenOf: [String: [String]] = [:]
    // 成员组 -> 父组（反向边，用于从用户向上找祖先组）
    var parentsOf: [String: [String]] = [:]
    var revocationByGrant: [String: Revocation] = [:]

    init(input: Input) {
        self.input = input
        for m in input.memberships {
            childrenOf[m.parentGroup, default: []].append(m.member)
            if input.groups.contains(where: { $0.id == m.member }) {
                parentsOf[m.member, default: []].append(m.parentGroup)
            }
        }
        for r in input.revocations {
            revocationByGrant[r.grantId] = r
        }
    }

    /// 从用户直接所属组出发，沿组成员边向上收集全部祖先组。
    /// 用 onStack 检测环：命中环即记录链路，节点不再重复入栈，保证离线计算可终止。
    func ancestry(of user: String) -> (groups: [String], membershipChain: [String], cyclic: Bool) {
        var result = Set<String>()
        var chain: [String] = []
        var cyclic = false
        var onStack = Set<String>()

        func dfs(_ group: String) {
            if onStack.contains(group) {
                cyclic = true
                if !chain.contains(group) { chain.append(group) }
                return
            }
            if result.contains(group) { return }
            onStack.insert(group)
            result.insert(group)
            chain.append(group)
            for p in parentsOf[group] ?? [] { dfs(p) }
            onStack.remove(group)
        }

        for m in input.memberships where m.member == user {
            dfs(m.parentGroup)
        }
        let sorted = result.sorted()
        return (sorted, chain, cyclic)
    }

    /// 图级别的环检测（独立于具体用户，覆盖成环的组集合）
    func detectCycles() -> [GraphCycle] {
        var color: [String: Int] = [:]   // 0=白 1=灰 2=黑
        var stack: [String] = []
        var cycles: [GraphCycle] = []
        var seenCycleKeys = Set<String>()

        func dfs(_ g: String) {
            color[g] = 1
            stack.append(g)
            for p in parentsOf[g] ?? [] {
                let c = color[p] ?? 0
                if c == 1, let idx = stack.firstIndex(of: p) {
                    let cycleGroups = Array(stack[idx...])
                    let key = cycleGroups.sorted().joined(separator: "->")
                    if !seenCycleKeys.contains(key) {
                        seenCycleKeys.insert(key)
                        let chainIds = input.memberships
                            .filter { cycleGroups.contains($0.parentGroup) && cycleGroups.contains($0.member) }
                            .map(\.id).sorted()
                        cycles.append(GraphCycle(
                            cycleGroups: cycleGroups,
                            membershipChain: chainIds,
                            detail: "组继承关系成环：\(cycleGroups.joined(separator: " -> ")) -> \(p)，成员关系按规则 R1 做有界遍历，环上授权结论需人工复核"
                        ))
                    }
                } else if c == 0 {
                    dfs(p)
                }
            }
            stack.removeLast()
            color[g] = 2
        }

        for g in input.groups.map(\.id).sorted() where (color[g] ?? 0) == 0 {
            dfs(g)
        }
        return cycles
    }

    func isGrantActive(_ g: Grant, at t: Int) -> Bool {
        if t < g.validFrom { return false }
        if let until = g.validUntil, t >= until { return false }
        return true
    }

    func effectivePermission(user: String, resource: String, action: String) -> EffectivePermission {
        let anc = ancestry(of: user)
        let scope = Set(anc.groups + [user])

        var activeAllow: [Grant] = []
        var activeDeny: [Grant] = []
        var revokedOnPath: [String] = []

        for g in input.grants where g.resource == resource && g.action == action && scope.contains(g.subject) {
            // R4：撤权事件一旦存在，该授权即视为撤销（撤权本身无有效期概念）
            if let rev = revocationByGrant[g.id] {
                revokedOnPath.append("\(g.id)(撤权事件 \(rev.id) @\(rev.revokedAt))")
                continue
            }
            // R3：有效期窗口之外的授权不参与判定
            guard isGrantActive(g, at: input.evaluationTime) else { continue }
            if g.effect == "deny" { activeDeny.append(g) } else { activeAllow.append(g) }
        }

        let decision: String
        if !activeDeny.isEmpty {
            decision = "deny"                               // R2：显式拒绝优先
        } else if !activeAllow.isEmpty {
            decision = "allow"                              // R1：组继承/直接允许
        } else {
            decision = "none"
        }

        return EffectivePermission(
            user: user,
            resource: resource,
            action: action,
            decision: decision,
            matchedGrants: activeAllow.map(\.id).sorted(),
            denyGrants: activeDeny.map(\.id).sorted(),
            ancestry: anc.groups,
            revokedGrants: revokedOnPath.sorted(),
            cyclicAncestry: anc.cyclic
        )
    }

    // MARK: 差异分类

    func classify(_ obs: Observation, _ eff: EffectivePermission) -> Finding? {
        let t = input.evaluationTime
        let wm = input.sync.watermark
        let period = input.sync.periodSeconds

        // R5：快照清单声明缺失，或观察记录引用了不存在的快照
        let snapshotKnown = input.snapshots.contains { $0.id == obs.snapshotId }
        let snapshotPresent = input.snapshots.first { $0.id == obs.snapshotId }?.present ?? false
        if obs.observed == "missing" || !snapshotKnown || !snapshotPresent {
            return Finding(
                code: "EVIDENCE_MISSING",
                severity: "high",
                user: obs.user, resource: obs.resource, action: obs.action,
                expectedDecision: eff.decision, observed: obs.observed,
                revocationEvent: relevantRevocation(eff),
                evidenceId: obs.id,
                detail: "证据缺失：观察记录 \(obs.id) 引用快照 \(obs.snapshotId)，该快照缺失或未在快照清单中登记，无法证明观察值",
                check: "补拍/核对快照 \(obs.snapshotId)（规则 R5：证据完整性）"
            )
        }

        // 观察记录早于同步水位：水位之前的快照不包含水位线内的同步结果
        if obs.snapshotTime < wm {
            return Finding(
                code: "EVIDENCE_STALE",
                severity: "medium",
                user: obs.user, resource: obs.resource, action: obs.action,
                expectedDecision: eff.decision, observed: obs.observed,
                revocationEvent: relevantRevocation(eff),
                evidenceId: obs.id,
                detail: "观察记录 \(obs.id) 的快照时间 \(obs.snapshotTime) 早于同步水位 \(wm)，其观察值不能用于评判水位之后的撤权传播",
                check: "核对晚于同步水位 \(wm) 的新快照（规则 R5）"
            )
        }

        // 观察=允许
        if obs.observed == "allow" {
            // 独立授权来源：用户自身存在未撤销且在有效期内的 allow（组来源已被撤销或不成立）
            let independent = input.grants.first { g in
                g.subject == obs.user && g.resource == obs.resource && g.action == obs.action
                    && g.effect == "allow" && revocationByGrant[g.id] == nil && isGrantActive(g, at: t)
            }

            // 有效决策本身就是 allow：仅当路径上确实发生过撤权、且访问由个人独立授权支撑时，
            // 产出 info 级说明项，解释"撤权后仍可访问"的合理来源
            if eff.decision == "allow" {
                if let g = independent, !eff.revokedGrants.isEmpty {
                    return Finding(
                        code: "INDEPENDENT_GRANT",
                        severity: "info",
                        user: obs.user, resource: obs.resource, action: obs.action,
                        expectedDecision: eff.decision, observed: obs.observed,
                        revocationEvent: relevantRevocation(eff),
                        evidenceId: obs.id,
                        detail: "组来源已撤销（\(eff.revokedGrants.joined(separator: ", "))），但用户持有独立直接授权 \(g.id)（有效期 \(g.validFrom)..\(g.validUntil.map(String.init) ?? "∞")），观察到的访问有合理来源，非撤权失效",
                        check: "核对独立授权 \(g.id) 是否仍应保留（规则 R1/R3）；如应回收则对其单独发起撤权"
                    )
                }
                return nil
            }

            // 以下为有效决策 deny/none 却观察到 allow 的真实差异
            if let g = independent, eff.decision != "deny" {
                return Finding(
                    code: "INDEPENDENT_GRANT",
                    severity: "info",
                    user: obs.user, resource: obs.resource, action: obs.action,
                    expectedDecision: eff.decision, observed: obs.observed,
                    revocationEvent: relevantRevocation(eff),
                    evidenceId: obs.id,
                    detail: "组来源不成立，但用户持有独立直接授权 \(g.id)（有效期 \(g.validFrom)..\(g.validUntil.map(String.init) ?? "∞")），观察到的访问有合理来源",
                    check: "核对独立授权 \(g.id) 是否仍应保留（规则 R1/R3）"
                )
            }

            // 环上的授权结论证据力不足
            if eff.cyclicAncestry {
                return Finding(
                    code: "EVIDENCE_CYCLE",
                    severity: "high",
                    user: obs.user, resource: obs.resource, action: obs.action,
                    expectedDecision: eff.decision, observed: obs.observed,
                    revocationEvent: relevantRevocation(eff),
                    evidenceId: obs.id,
                    detail: "用户祖先组成环，遍历在环上被截断，无法确定性地证明撤权已沿环传播，观察到的访问无法被现有图证据解释",
                    check: "核对组成员关系环（规则 R1）与环上组授权快照，断开环后重算"
                )
            }

            // 暂态 vs 超期残留：以最近一次相关撤权事件判定
            guard let rev = latestRevocation(eff) else {
                let expired = expiredGrantsOnPath(obs)
                return Finding(
                    code: "UNEXPECTED_ALLOW",
                    severity: "high",
                    user: obs.user, resource: obs.resource, action: obs.action,
                    expectedDecision: eff.decision, observed: obs.observed,
                    revocationEvent: nil, evidenceId: obs.id,
                    detail: "无任何相关撤权事件，却观察到 allow 且引擎决策为 \(eff.decision)"
                        + (expired.isEmpty ? "，存在模型外授权来源" : "；窗口外授权不参与判定：\(expired)（规则 R3）"),
                    check: "核对资源 \(obs.resource) 的全部授权快照与规则 R1-R3"
                )
            }
            let deadline = rev.revokedAt + period
            if t <= deadline {
                return Finding(
                    code: "TRANSIENT",
                    severity: "low",
                    user: obs.user, resource: obs.resource, action: obs.action,
                    expectedDecision: eff.decision, observed: obs.observed,
                    revocationEvent: rev.id, evidenceId: obs.id,
                    detail: "撤权事件 \(rev.id) @\(rev.revokedAt) 仍在同步期限内（截止 \(deadline)，评估时点 \(t)），观察到的访问属于允许的传播暂态",
                    check: "在 \(deadline) 后重取快照核对（规则 R4：同步期限）"
                )
            } else {
                return Finding(
                    code: "RESIDUAL",
                    severity: "critical",
                    user: obs.user, resource: obs.resource, action: obs.action,
                    expectedDecision: eff.decision, observed: obs.observed,
                    revocationEvent: rev.id, evidenceId: obs.id,
                    detail: "撤权事件 \(rev.id) @\(rev.revokedAt) 的同步期限已于 \(deadline) 届满，评估时点 \(t) 仍观察到 allow，属超期撤权残留",
                    check: "立即核对同步管道与资源 \(obs.resource) 的授权快照（规则 R4），定位未传播的撤权"
                )
            }
        }

        // 观察=deny：与有效决策 deny/none 一致即无差异；有效决策为 allow 则属拒绝过度
        let denyMatches = eff.decision == "deny" || eff.decision == "none"
        if denyMatches { return nil }
        return Finding(
            code: "UNEXPECTED_DENY",
            severity: "medium",
            user: obs.user, resource: obs.resource, action: obs.action,
            expectedDecision: eff.decision, observed: obs.observed,
            revocationEvent: relevantRevocation(eff),
            evidenceId: obs.id,
            detail: "引擎按规则应得 allow，但观察值为 deny，可能存在快照外的拒绝策略",
            check: "核对显式拒绝清单（规则 R2）与快照 \(obs.snapshotId)"
        )
    }

    /// 路径上（用户+祖先组）已过有效期或未生效的授权，用于解释无撤权事件下的异常 allow
    private func expiredGrantsOnPath(_ obs: Observation) -> [String] {
        let anc = ancestry(of: obs.user)
        let scope = Set(anc.groups + [obs.user])
        return input.grants
            .filter { g in
                g.resource == obs.resource && g.action == obs.action
                    && scope.contains(g.subject) && revocationByGrant[g.id] == nil
                    && !isGrantActive(g, at: input.evaluationTime)
            }
            .map { g in
                if input.evaluationTime < g.validFrom {
                    return "\(g.id)(未到生效时间 \(g.validFrom))"
                }
                return "\(g.id)(已于 \(g.validUntil ?? -1) 过期)"
            }
            .sorted()
    }

    private func relevantRevocation(_ eff: EffectivePermission) -> String? {
        // revokedGrants 形如 "GR-1(撤权事件 REV-1 @100)"，提取撤权事件号
        guard let first = eff.revokedGrants.sorted().first,
              let lo = first.range(of: "撤权事件 ")?.upperBound,
              let hi = first[lo...].range(of: " ")?.lowerBound else { return nil }
        return String(first[lo..<hi])
    }

    private func latestRevocation(_ eff: EffectivePermission) -> Revocation? {
        let ids = eff.revokedGrants.compactMap { s -> String? in
            guard let lo = s.range(of: "撤权事件 ")?.upperBound,
                  let hi = s[lo...].range(of: " ")?.lowerBound else { return nil }
            return String(s[lo..<hi])
        }
        return ids.compactMap { id in input.revocations.first { $0.id == id } }
                  .max(by: { $0.revokedAt < $1.revokedAt })
    }

    let rules = [
        RuleDoc(ruleId: "R1", priority: 1,
                title: "组继承授权",
                statement: "用户通过自身直接所属组及全部祖先组成员关系继承组上的 allow/deny；成员图有环时按已访问集合有界遍历，环上结论标记为证据问题"),
        RuleDoc(ruleId: "R2", priority: 2,
                title: "显式拒绝优先",
                statement: "同一 资源+动作 上只要存在生效中的 deny（直接或继承），即覆盖一切 allow，决策为 deny"),
        RuleDoc(ruleId: "R3", priority: 3,
                title: "有效期窗口",
                statement: "授权仅在 [validFrom, validUntil) 内参与判定；validUntil 为空视为长期有效；窗口外授权等同不存在"),
        RuleDoc(ruleId: "R4", priority: 4,
                title: "撤权与同步期限",
                statement: "撤权事件使目标授权立即退出判定；撤权时间 + sync.periodSeconds 为传播宽限期，期内观察差异为暂态，超期仍可访问为撤权残留"),
        RuleDoc(ruleId: "R5", priority: 5,
                title: "证据水位与完整性",
                statement: "快照时间早于同步水位的观察记录不得用于评判水位后的撤权；快照缺失、引用未知快照或成员图成环均作为证据问题报告")
    ]

    func run() -> Report {
        let cycles = detectCycles()

        // 去重后计算每条观察涉及的有效权限
        var effs: [EffectivePermission] = []
        var seen = Set<String>()
        for obs in input.observations {
            let key = [obs.user, obs.resource, obs.action].joined(separator: "|")
            if seen.contains(key) { continue }
            seen.insert(key)
            effs.append(effectivePermission(user: obs.user, resource: obs.resource, action: obs.action))
        }
        effs.sort { a, b in
            let ka = (a.user, a.resource, a.action), kb = (b.user, b.resource, b.action)
            return ka < kb
        }

        var findings: [Finding] = []
        for obs in input.observations {
            let eff = effs.first { $0.user == obs.user && $0.resource == obs.resource && $0.action == obs.action }!
            if let f = classify(obs, eff) { findings.append(f) }
        }
        findings.sort { ($0.user, $0.resource, $0.action, $0.code) < ($1.user, $1.resource, $1.action, $1.code) }

        var counts: [String: Int] = [:]
        for f in findings { counts[f.code, default: 0] += 1 }

        return Report(
            evaluationTime: input.evaluationTime,
            syncWatermark: input.sync.watermark,
            syncPeriodSeconds: input.sync.periodSeconds,
            rules: rules,
            effectivePermissions: effs,
            cycles: cycles,
            findings: findings,
            findingCounts: counts
        )
    }
}

// MARK: - 文本报告

func renderText(_ report: Report) -> String {
    var out = ""
    out += "== 授权撤权离线诊断报告 ==\n"
    out += "评估时点: \(report.evaluationTime)  同步水位: \(report.syncWatermark)  同步期限: \(report.syncPeriodSeconds)s\n\n"

    out += "授权规则（优先级从高到低）:\n"
    for r in report.rules {
        out += "  [\(r.ruleId)] P\(r.priority) \(r.title) — \(r.statement)\n"
    }
    out += "\n"

    out += "成员图环检测: \(report.cycles.isEmpty ? "未发现环" : "发现 \(report.cycles.count) 个环")\n"
    for c in report.cycles {
        out += "  - \(c.detail)\n    证据(成员关系): \(c.membershipChain.joined(separator: ", "))\n"
    }
    out += "\n"

    out += "有效权限（评估时点应有状态）:\n"
    for e in report.effectivePermissions {
        out += "  - \(e.user) / \(e.action) / \(e.resource) => \(e.decision.uppercased())"
        out += " [祖先组: \(e.ancestry.joined(separator: ",") )]\(e.cyclicAncestry ? " [成环!]" : "")\n"
        if !e.matchedGrants.isEmpty { out += "      生效 allow: \(e.matchedGrants.joined(separator: ", "))\n" }
        if !e.denyGrants.isEmpty { out += "      生效 deny:  \(e.denyGrants.joined(separator: ", "))\n" }
        if !e.revokedGrants.isEmpty { out += "      路径上已撤销: \(e.revokedGrants.joined(separator: ", "))\n" }
    }
    out += "\n"

    out += "差异项（共 \(report.findings.count) 项）:\n"
    if report.findings.isEmpty {
        out += "  无差异，全部观察值与有效权限一致。\n"
    }
    for f in report.findings {
        out += "  [\(f.severity.uppercased())] \(f.code)  \(f.user)/\(f.action)/\(f.resource)\n"
        out += "      期望=\(f.expectedDecision) 观察=\(f.observed) 撤权事件=\(f.revocationEvent ?? "-") 证据=\(f.evidenceId ?? "-")\n"
        out += "      说明: \(f.detail)\n"
        out += "      核对: \(f.check)\n"
    }
    out += "\n分类计数: \(report.findingCounts.sorted(by: { $0.key < $1.key }).map { "\($0.key)=\($0.value)" }.joined(separator: ", "))\n"
    return out
}

// MARK: - CLI

let args = CommandLine.arguments
func usage() -> Never {
    FileHandle.standardError.write(Data("usage: AuthzAudit <input.json> [--format json|text] [-o output]\n".utf8))
    exit(2)
}

guard args.count >= 2 else { usage() }
let inputPath = args[1]
var format = "json"
var outputPath: String?
var i = 2
while i < args.count {
    switch args[i] {
    case "--format":
        i += 1
        guard i < args.count, ["json", "text"].contains(args[i]) else { usage() }
        format = args[i]
    case "-o":
        i += 1
        guard i < args.count else { usage() }
        outputPath = args[i]
    default:
        usage()
    }
    i += 1
}

let url = URL(fileURLWithPath: inputPath)
let data = try Data(contentsOf: url)
let decoder = JSONDecoder()
let input: Input
do {
    input = try decoder.decode(Input.self, from: data)
} catch {
    FileHandle.standardError.write(Data("输入 JSON 解析失败: \(error)\n".utf8))
    exit(1)
}

let report = Engine(input: input).run()
let rendered: String
if format == "text" {
    rendered = renderText(report)
} else {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
    rendered = String(data: try encoder.encode(report), encoding: .utf8)!
}

if let out = outputPath {
    try rendered.write(toFile: out, atomically: true, encoding: .utf8)
    print("报告已写入 \(out)（\(report.findings.count) 项差异）")
} else {
    print(rendered)
}
