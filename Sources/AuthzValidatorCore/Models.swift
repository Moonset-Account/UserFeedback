import Foundation

/// 网关事件类型：授权记录或投递事件。
public enum EventType: String, Codable, Equatable {
    case authorization
    case delivery
}

/// 授权结论：批准或拒绝。
public enum Decision: String, Codable, Equatable {
    case approve
    case deny
}

/// 一条网关出站日志事件。
///
/// 授权记录与投递事件共用同一结构：
/// - 两者都包含事件类型、匿名请求 ID、阶段序号、目标分类、策略版本、决策 ID、事件时间。
/// - 仅授权记录额外携带 `decision`（批准/拒绝结论）与 `validUntil`（批准有效截止时间）。
public struct GatewayEvent: Codable, Equatable {
    public let eventType: EventType
    public let requestId: String
    public let stage: Int
    public let targetClassification: String
    public let policyVersion: String
    public let decisionId: String
    public let eventTime: String
    public let decision: Decision?
    public let validUntil: String?
    public let caseId: String?

    public init(
        eventType: EventType,
        requestId: String,
        stage: Int,
        targetClassification: String,
        policyVersion: String,
        decisionId: String,
        eventTime: String,
        decision: Decision? = nil,
        validUntil: String? = nil,
        caseId: String? = nil
    ) {
        self.eventType = eventType
        self.requestId = requestId
        self.stage = stage
        self.targetClassification = targetClassification
        self.policyVersion = policyVersion
        self.decisionId = decisionId
        self.eventTime = eventTime
        self.decision = decision
        self.validUntil = validUntil
        self.caseId = caseId
    }

    enum CodingKeys: String, CodingKey {
        case eventType = "event_type"
        case requestId = "request_id"
        case stage
        case targetClassification = "target_classification"
        case policyVersion = "policy_version"
        case decisionId = "decision_id"
        case eventTime = "event_time"
        case decision
        case validUntil = "valid_until"
        case caseId = "case_id"
    }
}

/// 违反授权约束的稳定原因码。
public enum ReasonCode: String, Equatable {
    /// 投递引用的决策 ID 没有任何授权记录。
    case approvalRecordMissing = "approval_record_missing"
    /// 同一决策 ID 存在多条互相矛盾的授权记录。
    case conflictingAuthorizationRecords = "conflicting_authorization_records"
    /// 授权结论为拒绝，但仍出现投递事件。
    case deliveryUnderDeniedDecision = "delivery_under_denied_decision"
    /// 授权记录时间晚于投递时间，时序不一致。
    case authorizationTimestampAfterDelivery = "authorization_timestamp_after_delivery"
    /// 投递时间晚于批准有效截止时间。
    case authorizationExpired = "authorization_expired"
    /// 批准后阶段序号发生变化但未重新授权。
    case stageChangedWithoutReauthorization = "stage_changed_without_reauthorization"
    /// 目标分类发生变化但未重新授权。
    case targetClassificationChangedWithoutReauthorization = "target_classification_changed_without_reauthorization"
    /// 策略版本发生变化但未重新授权。
    case policyVersionChangedWithoutReauthorization = "policy_version_changed_without_reauthorization"
    /// 事件时间不是合法的 ISO8601 时间戳。
    case invalidTimestamp = "invalid_timestamp"
    /// 批准记录缺少有效截止时间。
    case approvalMissingValidUntil = "approval_missing_valid_until"
}

/// 单条证据问题：原因、涉及事件以及可核对的原始记录证据。
public struct ValidationFinding: Codable, Equatable {
    public let caseId: String?
    public let reasonCode: String
    public let reason: String
    public let requestId: String
    public let decisionId: String
    public let eventTime: String?
    public let evidence: Evidence

    public struct Evidence: Codable, Equatable {
        /// 触发问题的投递事件；记录级问题（如冲突）时为 nil。
        public let delivery: GatewayEvent?
        /// 相关授权记录；批准缺失时为空数组。
        public let authorizationRecords: [GatewayEvent]

        public init(delivery: GatewayEvent?, authorizationRecords: [GatewayEvent]) {
            self.delivery = delivery
            self.authorizationRecords = authorizationRecords
        }
    }

    public init(
        caseId: String?,
        reasonCode: ReasonCode,
        reason: String,
        requestId: String,
        decisionId: String,
        eventTime: String?,
        evidence: Evidence
    ) {
        self.caseId = caseId
        self.reasonCode = reasonCode.rawValue
        self.reason = reason
        self.requestId = requestId
        self.decisionId = decisionId
        self.eventTime = eventTime
        self.evidence = evidence
    }
}

/// 一次完整校验的报告。`findings` 已按事件时间升序排列。
public struct ValidationReport: Codable, Equatable {
    public let totalEvents: Int
    public let authorizationCount: Int
    public let deliveryCount: Int
    public let findings: [ValidationFinding]

    public init(
        totalEvents: Int,
        authorizationCount: Int,
        deliveryCount: Int,
        findings: [ValidationFinding]
    ) {
        self.totalEvents = totalEvents
        self.authorizationCount = authorizationCount
        self.deliveryCount = deliveryCount
        self.findings = findings
    }

    /// 第一处违反授权约束的事件；无违反时为 nil。
    public var firstViolation: ValidationFinding? { findings.first }

    public var isCompliant: Bool { findings.isEmpty }
}
