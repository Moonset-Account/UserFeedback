import Foundation

public enum ViolationKind: String, Codable, Equatable, Sendable {
    /// 调用边引用了清单中不存在的组件身份（如遗漏某个插件版本）
    case unknownComponent = "unknown_component"
    /// 目标组件没有保护覆盖声明
    case missingDeclaration = "missing_declaration"
    /// 有声明，但被调接口未落入受保护范围
    case uncoveredEdge = "uncovered_edge"
    /// 引用的豁免已过期限
    case expiredExemption = "expired_exemption"
    /// 调用两端 ABI 版本不一致
    case abiMismatch = "abi_mismatch"
    /// 组件依赖图存在环
    case dependencyCycle = "dependency_cycle"
}

/// 一条违规。调用类违规携带每条调用要求的定位信息；环违规汇总参与组件。
public struct Violation: Codable, Equatable, Sendable {
    public var kind: ViolationKind
    public var message: String
    // 每条违规调用必须列出的定位信息
    public var buildTarget: String?
    public var interface: String?
    public var sourceBuildTarget: String?
    public var team: String?
    public var sourceTeam: String?
    public var source: ComponentID?
    public var target: ComponentID?
    public var exemptionId: String?
    /// 仅 dependency_cycle 使用：环上的组件身份（按依赖顺序）
    public var cycle: [ComponentID]?
    /// 环中各组件的构建目标
    public var cycleBuildTargets: [String]?

    public init(
        kind: ViolationKind,
        message: String,
        buildTarget: String? = nil,
        interface: String? = nil,
        sourceBuildTarget: String? = nil,
        team: String? = nil,
        sourceTeam: String? = nil,
        source: ComponentID? = nil,
        target: ComponentID? = nil,
        exemptionId: String? = nil,
        cycle: [ComponentID]? = nil,
        cycleBuildTargets: [String]? = nil
    ) {
        self.kind = kind
        self.message = message
        self.buildTarget = buildTarget
        self.interface = interface
        self.sourceBuildTarget = sourceBuildTarget
        self.team = team
        self.sourceTeam = sourceTeam
        self.source = source
        self.target = target
        self.exemptionId = exemptionId
        self.cycle = cycle
        self.cycleBuildTargets = cycleBuildTargets
    }
}

/// 有完整证据的跨组件调用：两端身份存在、ABI 一致，且接口在目标组件保护范围内。
public struct EvidencedCall: Codable, Equatable, Sendable {
    public var edge: IndirectCallEdge
    public var buildTarget: String
    public var interface: String
    public var team: String
    public var sourceBuildTarget: String
    public var sourceTeam: String

    public init(
        edge: IndirectCallEdge,
        buildTarget: String,
        interface: String,
        team: String,
        sourceBuildTarget: String,
        sourceTeam: String
    ) {
        self.edge = edge
        self.buildTarget = buildTarget
        self.interface = interface
        self.team = team
        self.sourceBuildTarget = sourceBuildTarget
        self.sourceTeam = sourceTeam
    }
}

/// 被有效豁免放行的调用（用于审计：豁免在放行什么）。
public struct ExemptedCall: Codable, Equatable, Sendable {
    public var edge: IndirectCallEdge
    public var exemptionId: String
    public var expiresAt: Date
    public var buildTarget: String
    public var interface: String
    public var team: String

    public init(
        edge: IndirectCallEdge,
        exemptionId: String,
        expiresAt: Date,
        buildTarget: String,
        interface: String,
        team: String
    ) {
        self.edge = edge
        self.exemptionId = exemptionId
        self.expiresAt = expiresAt
        self.buildTarget = buildTarget
        self.interface = interface
        self.team = team
    }
}

public struct GateReport: Codable, Equatable, Sendable {
    public var evaluatedAt: Date
    public var passed: Bool
    public var summary: Summary
    public var evidencedCalls: [EvidencedCall]
    public var exemptedCalls: [ExemptedCall]
    public var violations: [Violation]

    public struct Summary: Codable, Equatable, Sendable {
        public var componentCount: Int
        public var edgeCount: Int
        public var crossComponentEdgeCount: Int
        public var evidencedCount: Int
        public var exemptedCount: Int
        public var violationCount: Int

        public init(
            componentCount: Int,
            edgeCount: Int,
            crossComponentEdgeCount: Int,
            evidencedCount: Int,
            exemptedCount: Int,
            violationCount: Int
        ) {
            self.componentCount = componentCount
            self.edgeCount = edgeCount
            self.crossComponentEdgeCount = crossComponentEdgeCount
            self.evidencedCount = evidencedCount
            self.exemptedCount = exemptedCount
            self.violationCount = violationCount
        }
    }

    public init(
        evaluatedAt: Date,
        passed: Bool,
        summary: Summary,
        evidencedCalls: [EvidencedCall],
        exemptedCalls: [ExemptedCall],
        violations: [Violation]
    ) {
        self.evaluatedAt = evaluatedAt
        self.passed = passed
        self.summary = summary
        self.evidencedCalls = evidencedCalls
        self.exemptedCalls = exemptedCalls
        self.violations = violations
    }
}
