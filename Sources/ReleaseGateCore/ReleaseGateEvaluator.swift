import Foundation

/// 发布门禁评估引擎。
///
/// 判定顺序（每条跨组件间接调用边）：
/// 1. 两端组件身份必须存在，否则 unknown_component；
/// 2. ABI 版本必须一致，否则 abi_mismatch（豁免不可压制）；
/// 3. 目标接口必须被目标组件的覆盖声明包含：
///    - 无声明 → missing_declaration；有声明但缺接口 → uncovered_edge；
///    - 存在匹配该「组件身份 + 接口」且未过期的豁免 → 放行并记入 exemptedCalls；
///    - 匹配的豁免已过期 → expired_exemption（过期豁免不提供放行）。
/// 4. 依赖图含环 → dependency_cycle（独立于调用边，豁免不可压制）。
public struct ReleaseGateEvaluator: Sendable {
    public let now: Date

    public init(now: Date = Date()) {
        self.now = now
    }

    public func evaluate(_ manifest: BuildManifest) -> GateReport {
        let componentsById = Dictionary(uniqueKeysWithValues: manifest.components.map { ($0.id, $0) })
        let claimsByComponent = Dictionary(
            manifest.coverageClaims.map { ($0.component, Set($0.coveredInterfaces)) }
        ) { first, _ in first }

        var violations: [Violation] = []
        var evidenced: [EvidencedCall] = []
        var exempted: [ExemptedCall] = []
        var crossComponentCount = 0

        for edge in manifest.edges {
            // 组件内部调用不在独立构建插件的跨组件集成面内，不纳入门禁判定。
            if edge.source == edge.target { continue }
            crossComponentCount += 1

            guard let targetComponent = componentsById[edge.target],
                  let sourceComponent = componentsById[edge.source] else {
                let missing = componentsById[edge.source] == nil ? edge.source : edge.target
                violations.append(Violation(
                    kind: .unknownComponent,
                    message: "调用边引用了清单中不存在的组件 \(missing.name)@\(missing.version)（接口 \(edge.interface)）",
                    buildTarget: componentsById[edge.target]?.buildTarget,
                    interface: edge.interface,
                    sourceBuildTarget: componentsById[edge.source]?.buildTarget,
                    team: componentsById[edge.target]?.team,
                    sourceTeam: componentsById[edge.source]?.team,
                    source: edge.source,
                    target: edge.target
                ))
                continue
            }

            // ABI 不一致属于硬错误，任何豁免都不能压制；记录后不再评估覆盖。
            if sourceComponent.abiVersion != targetComponent.abiVersion {
                violations.append(Violation(
                    kind: .abiMismatch,
                    message: """
                    ABI 版本不一致：\(sourceComponent.id.name)@\(sourceComponent.id.version) 为 \
                    ABI \(sourceComponent.abiVersion)，\(targetComponent.id.name)@\
                    \(targetComponent.id.version) 为 ABI \(targetComponent.abiVersion)（接口 \(edge.interface)）
                    """,
                    buildTarget: targetComponent.buildTarget,
                    interface: edge.interface,
                    sourceBuildTarget: sourceComponent.buildTarget,
                    team: targetComponent.team,
                    sourceTeam: sourceComponent.team,
                    source: edge.source,
                    target: edge.target
                ))
                continue
            }

            let isCovered: Bool
            if let covered = claimsByComponent[edge.target] {
                isCovered = covered.contains(edge.interface)
            } else {
                isCovered = false
            }

            if isCovered {
                evidenced.append(EvidencedCall(
                    edge: edge,
                    buildTarget: targetComponent.buildTarget,
                    interface: edge.interface,
                    team: targetComponent.team,
                    sourceBuildTarget: sourceComponent.buildTarget,
                    sourceTeam: sourceComponent.team
                ))
                continue
            }

            // 未覆盖：只接受精确匹配「目标组件身份 + 接口」的豁免。
            let matchingExemptions = manifest.exemptions.filter {
                $0.matches(target: edge.target, interface: edge.interface)
            }

            if let valid = matchingExemptions.first(where: { !$0.isExpired(now: now) }) {
                exempted.append(ExemptedCall(
                    edge: edge,
                    exemptionId: valid.id,
                    expiresAt: valid.expiresAt,
                    buildTarget: targetComponent.buildTarget,
                    interface: edge.interface,
                    team: targetComponent.team
                ))
                continue
            }

            if let expired = matchingExemptions.first(where: { $0.isExpired(now: now) }) {
                violations.append(Violation(
                    kind: .expiredExemption,
                    message: """
                    豁免 \(expired.id) 已于 \(Self.dateString(expired.expiresAt)) 过期，\
                    接口 \(edge.interface) 仍处于未保护状态
                    """,
                    buildTarget: targetComponent.buildTarget,
                    interface: edge.interface,
                    sourceBuildTarget: sourceComponent.buildTarget,
                    team: targetComponent.team,
                    sourceTeam: sourceComponent.team,
                    source: edge.source,
                    target: edge.target,
                    exemptionId: expired.id
                ))
                continue
            }

            let kind: ViolationKind = claimsByComponent[edge.target] == nil
                ? .missingDeclaration
                : .uncoveredEdge
            let message = kind == .missingDeclaration
                ? "组件 \(edge.target.name)@\(edge.target.version) 缺少控制流完整性保护覆盖声明（接口 \(edge.interface)）"
                : "接口 \(edge.interface) 未落入 \(edge.target.name)@\(edge.target.version) 的保护覆盖范围"
            violations.append(Violation(
                kind: kind,
                message: message,
                buildTarget: targetComponent.buildTarget,
                interface: edge.interface,
                sourceBuildTarget: sourceComponent.buildTarget,
                team: targetComponent.team,
                sourceTeam: sourceComponent.team,
                source: edge.source,
                target: edge.target
            ))
        }

        violations.append(contentsOf: detectCycles(
            edges: manifest.edges,
            componentsById: componentsById
        ))

        let summary = GateReport.Summary(
            componentCount: manifest.components.count,
            edgeCount: manifest.edges.count,
            crossComponentEdgeCount: crossComponentCount,
            evidencedCount: evidenced.count,
            exemptedCount: exempted.count,
            violationCount: violations.count
        )

        return GateReport(
            evaluatedAt: now,
            passed: violations.isEmpty,
            summary: summary,
            evidencedCalls: evidenced,
            exemptedCalls: exempted,
            violations: violations
        )
    }

    // MARK: - 依赖环检测（三色 DFS，环按最小身份旋转去重）

    private func detectCycles(
        edges: [IndirectCallEdge],
        componentsById: [ComponentID: Component]
    ) -> [Violation] {
        var adjacency: [ComponentID: Set<ComponentID>] = [:]
        var nodes = Set<ComponentID>()
        for edge in edges where edge.source != edge.target {
            // 引用未知组件的边已在调用判定中记录，这里无法构建可靠的图。
            guard componentsById[edge.source] != nil, componentsById[edge.target] != nil else { continue }
            adjacency[edge.source, default: []].insert(edge.target)
            nodes.insert(edge.source)
            nodes.insert(edge.target)
        }

        var color: [ComponentID: Int] = [:] // 0=白 1=灰 2=黑
        var stack: [ComponentID] = []
        var rawCycles: [[ComponentID]] = []

        func visit(_ node: ComponentID) {
            color[node] = 1
            stack.append(node)
            for neighbor in adjacency[node] ?? [] {
                let neighborColor = color[neighbor] ?? 0
                if neighborColor == 0 {
                    visit(neighbor)
                } else if neighborColor == 1,
                          let start = stack.firstIndex(of: neighbor) {
                    rawCycles.append(Array(stack[start...]))
                }
            }
            stack.removeLast()
            color[node] = 2
        }

        for node in nodes.sorted() where (color[node] ?? 0) == 0 {
            visit(node)
        }

        var seen = Set<[ComponentID]>()
        return rawCycles.compactMap { cycle in
            let normalized = Self.normalize(cycle)
            guard seen.insert(normalized).inserted else { return nil }
            let targets = normalized.map { componentsById[$0]?.buildTarget ?? "?" }
            return Violation(
                kind: .dependencyCycle,
                message: "依赖图含环：" + normalized.map { "\($0.name)@\($0.version)" }.joined(separator: " -> ") + " -> \(normalized[0].name)@\(normalized[0].version)",
                buildTarget: targets.first,
                team: componentsById[normalized[0]]?.team,
                target: normalized[0],
                cycle: normalized,
                cycleBuildTargets: targets
            )
        }
    }

    /// 旋转到最小身份作为起点，使同一环无论从哪个节点进入都只报一次。
    static func normalize(_ cycle: [ComponentID]) -> [ComponentID] {
        guard let minIndex = cycle.indices.min(by: { cycle[$0] < cycle[$1] }) else { return cycle }
        return Array(cycle[minIndex...] + cycle[..<minIndex])
    }

    static func dateString(_ date: Date) -> String {
        ISO8601DateFormatter().string(from: date)
    }
}
