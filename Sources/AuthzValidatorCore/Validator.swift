import Foundation

/// 离线授权验证器：只核验输入记录，不发出任何网络请求。
///
/// 核对规则（每次投递）：
/// 1. 决策 ID 必须存在对应授权记录，否则报告批准记录缺失。
/// 2. 授权结论必须为 approve；deny 下出现投递即报告策略拒绝仍投递。
/// 3. 授权记录时间不得晚于投递时间（时序一致性）。
/// 4. 投递时间必须早于或等于批准有效截止时间，否则报告授权到期。
/// 5. 批准的阶段序号、目标分类、策略版本必须与投递一致；
///    任一维度变化都必须存在对应新决策 ID 的重新授权记录，否则报告未重新授权。
/// 另在记录级预检：
/// - 同一决策 ID 的多条授权记录互相冲突；
/// - 批准记录缺少有效截止时间；
/// - 事件时间无法解析。
public struct Validator {
    public init() {}

    public func validate(_ events: [GatewayEvent]) -> ValidationReport {
        var findings: [ValidationFinding] = []
        let authorizations = events.filter { $0.eventType == .authorization }
        let deliveries = events.filter { $0.eventType == .delivery }

        // decisionId -> 同决策 ID 的全部授权记录
        var authByDecision: [String: [GatewayEvent]] = [:]
        for auth in authorizations {
            authByDecision[auth.decisionId, default: []].append(auth)
        }

        preScanInvalidTimestamps(events: events, findings: &findings)
        preScanApprovalMissingValidUntil(authorizations: authorizations, findings: &findings)
        preScanConflictingDecisions(authByDecision: authByDecision, findings: &findings)

        let sortedDeliveries = deliveries.sorted { lhs, rhs in
            let lhsDate = Timestamp.parse(lhs.eventTime) ?? .distantPast
            let rhsDate = Timestamp.parse(rhs.eventTime) ?? .distantPast
            if lhsDate == rhsDate {
                return lhs.stage < rhs.stage
            }
            return lhsDate < rhsDate
        }

        for delivery in sortedDeliveries {
            validate(delivery: delivery, authByDecision: authByDecision, findings: &findings)
        }

        findings.sort { lhs, rhs in
            let lhsDate = Timestamp.parse(lhs.eventTime ?? "") ?? .distantPast
            let rhsDate = Timestamp.parse(rhs.eventTime ?? "") ?? .distantPast
            if lhsDate == rhsDate {
                return lhs.reasonCode < rhs.reasonCode
            }
            return lhsDate < rhsDate
        }

        return ValidationReport(
            totalEvents: events.count,
            authorizationCount: authorizations.count,
            deliveryCount: deliveries.count,
            findings: findings
        )
    }

    // MARK: - 记录级预检

    private func preScanInvalidTimestamps(events: [GatewayEvent], findings: inout [ValidationFinding]) {
        for event in events where Timestamp.parse(event.eventTime) == nil {
            findings.append(FindingBuilder.make(
                for: nil,
                code: .invalidTimestamp,
                reason: "事件 \(event.eventType.rawValue) 的时间戳 \"\(event.eventTime)\" 不是合法的 ISO8601 时间，无法核对时序",
                eventTime: event.eventTime,
                authRecords: event.eventType == .authorization ? [event] : []
            ))
        }
    }

    private func preScanApprovalMissingValidUntil(
        authorizations: [GatewayEvent],
        findings: inout [ValidationFinding]
    ) {
        for auth in authorizations where auth.decision == .approve && auth.validUntil == nil {
            findings.append(FindingBuilder.make(
                for: nil,
                code: .approvalMissingValidUntil,
                reason: "批准决策 \(auth.decisionId) 缺少有效截止时间（valid_until），无法判断批准是否仍有效",
                eventTime: auth.eventTime,
                authRecords: [auth]
            ))
        }
    }

    private func preScanConflictingDecisions(
        authByDecision: [String: [GatewayEvent]],
        findings: inout [ValidationFinding]
    ) {
        for (decisionId, records) in authByDecision where records.count > 1 {
            let keyPairs = Set(records.map {
                "\(String(describing: $0.decision))|\($0.stage)|\($0.targetClassification)|\($0.policyVersion)|\($0.validUntil ?? "nil")"
            })
            guard keyPairs.count > 1 else { continue }
            let latest = latestAuthorization(records)
            findings.append(FindingBuilder.make(
                for: nil,
                code: .conflictingAuthorizationRecords,
                reason: "决策 \(decisionId) 存在 \(records.count) 条互相冲突的授权记录（结论/阶段/目标分类/策略版本/截止时间不一致），无法确定唯一批准决定",
                eventTime: latest.eventTime,
                authRecords: records
            ))
        }
    }

    // MARK: - 单次投递核对

    private func validate(
        delivery: GatewayEvent,
        authByDecision: [String: [GatewayEvent]],
        findings: inout [ValidationFinding]
    ) {
        let matchingRecords = authByDecision[delivery.decisionId] ?? []

        guard let auth = matchingRecords.isEmpty ? nil : latestAuthorization(matchingRecords) else {
            findings.append(FindingBuilder.make(
                for: delivery,
                code: .approvalRecordMissing,
                reason: "投递 \(delivery.requestId) 引用决策 \(delivery.decisionId)，但不存在对应的授权记录，批准决定无法核验",
                eventTime: delivery.eventTime,
                authRecords: []
            ))
            return
        }

        guard auth.decision == .approve else {
            findings.append(FindingBuilder.make(
                for: delivery,
                code: .deliveryUnderDeniedDecision,
                reason: "决策 \(delivery.decisionId) 的授权结论为拒绝（deny），但请求 \(delivery.requestId) 仍出现投递记录",
                eventTime: delivery.eventTime,
                authRecords: matchingRecords
            ))
            return
        }

        if let authTime = Timestamp.parse(auth.eventTime),
           let deliveryTime = Timestamp.parse(delivery.eventTime),
           authTime > deliveryTime {
            findings.append(FindingBuilder.make(
                for: delivery,
                code: .authorizationTimestampAfterDelivery,
                reason: "决策 \(delivery.decisionId) 的授权时间 \(auth.eventTime) 晚于投递时间 \(delivery.eventTime)，时序不一致",
                eventTime: delivery.eventTime,
                authRecords: matchingRecords
            ))
            return
        }

        if let validUntil = auth.validUntil,
           let validUntilDate = Timestamp.parse(validUntil),
           let deliveryTime = Timestamp.parse(delivery.eventTime),
           deliveryTime > validUntilDate {
            findings.append(FindingBuilder.make(
                for: delivery,
                code: .authorizationExpired,
                reason: "决策 \(delivery.decisionId) 的批准已于 \(validUntil) 到期，投递时间 \(delivery.eventTime) 超出有效截止时间",
                eventTime: delivery.eventTime,
                authRecords: matchingRecords
            ))
            return
        }

        if auth.stage != delivery.stage {
            findings.append(FindingBuilder.make(
                for: delivery,
                code: .stageChangedWithoutReauthorization,
                reason: "批准对应阶段 \(auth.stage)，投递发生在阶段 \(delivery.stage)，阶段变化后未找到重新授权记录（仍复用决策 \(delivery.decisionId)）",
                eventTime: delivery.eventTime,
                authRecords: matchingRecords
            ))
            return
        }

        if auth.targetClassification != delivery.targetClassification {
            findings.append(FindingBuilder.make(
                for: delivery,
                code: .targetClassificationChangedWithoutReauthorization,
                reason: "批准对应目标分类 \(auth.targetClassification)，投递目标分类为 \(delivery.targetClassification)，分类变化后未找到重新授权记录（仍复用决策 \(delivery.decisionId)）",
                eventTime: delivery.eventTime,
                authRecords: matchingRecords
            ))
            return
        }

        if auth.policyVersion != delivery.policyVersion {
            findings.append(FindingBuilder.make(
                for: delivery,
                code: .policyVersionChangedWithoutReauthorization,
                reason: "批准对应策略版本 \(auth.policyVersion)，投递策略版本为 \(delivery.policyVersion)，策略版本变化后未找到重新授权记录（仍复用决策 \(delivery.decisionId)）",
                eventTime: delivery.eventTime,
                authRecords: matchingRecords
            ))
            return
        }
    }

    /// 同一决策 ID 有多条记录时，以事件时间最晚者作为有效决定（重复记录场景）。
    private func latestAuthorization(_ records: [GatewayEvent]) -> GatewayEvent {
        records.max { lhs, rhs in
            let lhsDate = Timestamp.parse(lhs.eventTime) ?? .distantPast
            let rhsDate = Timestamp.parse(rhs.eventTime) ?? .distantPast
            return lhsDate < rhsDate
        }!
    }
}

enum FindingBuilder {
    static func make(
        for delivery: GatewayEvent?,
        code: ReasonCode,
        reason: String,
        eventTime: String?,
        authRecords: [GatewayEvent]
    ) -> ValidationFinding {
        let requestId = delivery?.requestId ?? authRecords.first?.requestId ?? "unknown"
        let decisionId = delivery?.decisionId ?? authRecords.first?.decisionId ?? "unknown"
        return ValidationFinding(
            caseId: delivery?.caseId ?? authRecords.first(where: { $0.caseId != nil })?.caseId,
            reasonCode: code,
            reason: reason,
            requestId: requestId,
            decisionId: decisionId,
            eventTime: eventTime,
            evidence: .init(delivery: delivery, authorizationRecords: authRecords)
        )
    }
}
