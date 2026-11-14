import XCTest
@testable import AuthzValidatorCore

final class AuthzValidatorTests: XCTestCase {

    // MARK: - 测试夹具构造

    private static let baseTime = "2026-11-14T08:00:00Z"

    /// 构造一条授权记录。
    private func auth(
        caseId: String? = nil,
        requestId: String = "req-x",
        stage: Int = 1,
        target: String = "public-api",
        policy: String = "v3",
        decisionId: String = "dec-1",
        eventTime: String = "2026-11-14T08:00:00Z",
        decision: Decision = .approve,
        validUntil: String? = "2026-11-14T18:00:00Z"
    ) -> GatewayEvent {
        GatewayEvent(
            eventType: .authorization,
            requestId: requestId,
            stage: stage,
            targetClassification: target,
            policyVersion: policy,
            decisionId: decisionId,
            eventTime: eventTime,
            decision: decision,
            validUntil: validUntil,
            caseId: caseId
        )
    }

    /// 构造一条投递事件。
    private func delivery(
        caseId: String? = nil,
        requestId: String = "req-x",
        stage: Int = 1,
        target: String = "public-api",
        policy: String = "v3",
        decisionId: String = "dec-1",
        eventTime: String = "2026-11-14T09:00:00Z"
    ) -> GatewayEvent {
        GatewayEvent(
            eventType: .delivery,
            requestId: requestId,
            stage: stage,
            targetClassification: target,
            policyVersion: policy,
            decisionId: decisionId,
            eventTime: eventTime,
            decision: nil,
            validUntil: nil,
            caseId: caseId
        )
    }

    // MARK: - 题目要求的五类场景

    /// TC-01 合规投递：批准有效、阶段/分类/策略版本一致。
    func testTC01_compliantDelivery() {
        let events = [
            auth(caseId: "TC-01", decisionId: "dec-1", eventTime: "2026-11-14T08:00:00Z",
                 validUntil: "2026-11-14T18:00:00Z"),
            delivery(caseId: "TC-01", decisionId: "dec-1", eventTime: "2026-11-14T09:00:00Z")
        ]
        let report = Validator().validate(events)

        XCTAssertTrue(report.isCompliant, "TC-01 期望合规，实际 findings: \(report.findings.map(\.reasonCode))")
        XCTAssertNil(report.firstViolation)
        XCTAssertEqual(report.totalEvents, 2)
        XCTAssertEqual(report.authorizationCount, 1)
        XCTAssertEqual(report.deliveryCount, 1)
    }

    /// TC-02 批准后阶段变化：阶段序号变化但仍复用旧批准，无重新授权记录。
    func testTC02_stageChangedWithoutReauthorization() {
        let events = [
            auth(caseId: "TC-02", stage: 1, decisionId: "dec-1"),
            delivery(caseId: "TC-02", stage: 2, decisionId: "dec-1")
        ]
        let report = Validator().validate(events)

        XCTAssertEqual(report.findings.count, 1)
        let finding = try! XCTUnwrap(report.firstViolation)
        XCTAssertEqual(finding.caseId, "TC-02")
        XCTAssertEqual(finding.reasonCode, ReasonCode.stageChangedWithoutReauthorization.rawValue)
        XCTAssertEqual(finding.requestId, "req-x")
        XCTAssertEqual(finding.decisionId, "dec-1")
        XCTAssertEqual(finding.evidence.delivery?.stage, 2)
        XCTAssertEqual(finding.evidence.authorizationRecords.first?.stage, 1)
        XCTAssertTrue(finding.reason.contains("阶段"))
    }

    /// TC-03 授权到期：投递时间晚于批准有效截止时间。
    func testTC03_authorizationExpired() {
        let events = [
            auth(caseId: "TC-03", decisionId: "dec-1",
                 eventTime: "2026-11-14T08:00:00Z", validUntil: "2026-11-14T10:00:00Z"),
            delivery(caseId: "TC-03", decisionId: "dec-1", eventTime: "2026-11-14T11:00:00Z")
        ]
        let report = Validator().validate(events)

        XCTAssertEqual(report.findings.count, 1)
        let finding = try! XCTUnwrap(report.firstViolation)
        XCTAssertEqual(finding.caseId, "TC-03")
        XCTAssertEqual(finding.reasonCode, ReasonCode.authorizationExpired.rawValue)
        XCTAssertEqual(finding.eventTime, "2026-11-14T11:00:00Z")
        XCTAssertEqual(finding.evidence.authorizationRecords.first?.validUntil, "2026-11-14T10:00:00Z")
        XCTAssertTrue(finding.reason.contains("到期"))
    }

    /// TC-04 策略拒绝仍出现投递记录。
    func testTC04_deliveryUnderDeniedDecision() {
        let events = [
            auth(caseId: "TC-04", decision: .deny, validUntil: nil),
            delivery(caseId: "TC-04")
        ]
        let report = Validator().validate(events)

        XCTAssertEqual(report.findings.count, 1)
        let finding = try! XCTUnwrap(report.firstViolation)
        XCTAssertEqual(finding.caseId, "TC-04")
        XCTAssertEqual(finding.reasonCode, ReasonCode.deliveryUnderDeniedDecision.rawValue)
        XCTAssertEqual(finding.evidence.delivery?.decisionId, "dec-1")
        XCTAssertEqual(finding.evidence.authorizationRecords.first?.decision, .deny)
        XCTAssertTrue(finding.reason.contains("拒绝"))
    }

    /// TC-05 批准记录缺失：投递引用不存在的决策 ID。
    func testTC05_approvalRecordMissing() {
        let events = [
            delivery(caseId: "TC-05", decisionId: "ghost-decision")
        ]
        let report = Validator().validate(events)

        XCTAssertEqual(report.findings.count, 1)
        let finding = try! XCTUnwrap(report.firstViolation)
        XCTAssertEqual(finding.caseId, "TC-05")
        XCTAssertEqual(finding.reasonCode, ReasonCode.approvalRecordMissing.rawValue)
        XCTAssertEqual(finding.decisionId, "ghost-decision")
        XCTAssertTrue(finding.evidence.authorizationRecords.isEmpty)
        XCTAssertNotNil(finding.evidence.delivery)
        XCTAssertTrue(finding.reason.contains("不存在"))
    }

    // MARK: - 重新授权场景

    /// TC-06 阶段变化后存在重新授权记录：新决策 ID 覆盖新阶段，投递合规。
    func testTC06_reauthorizationAfterStageChangeIsCompliant() {
        let events = [
            auth(caseId: "TC-06", stage: 1, decisionId: "dec-1"),
            auth(caseId: "TC-06", stage: 2, decisionId: "dec-2", eventTime: "2026-11-14T08:30:00Z"),
            delivery(caseId: "TC-06", stage: 2, decisionId: "dec-2", eventTime: "2026-11-14T09:00:00Z")
        ]
        let report = Validator().validate(events)

        XCTAssertTrue(report.isCompliant, "TC-06 期望合规，实际: \(report.findings.map(\.reasonCode))")
        XCTAssertEqual(report.authorizationCount, 2)
    }

    /// TC-07 目标分类变化但未重新授权。
    func testTC07_targetClassificationChangedWithoutReauthorization() {
        let events = [
            auth(caseId: "TC-07", target: "public-api"),
            delivery(caseId: "TC-07", target: "internal-db")
        ]
        let report = Validator().validate(events)

        XCTAssertEqual(report.findings.count, 1)
        let finding = try! XCTUnwrap(report.firstViolation)
        XCTAssertEqual(finding.caseId, "TC-07")
        XCTAssertEqual(finding.reasonCode,
                       ReasonCode.targetClassificationChangedWithoutReauthorization.rawValue)
        XCTAssertEqual(finding.evidence.delivery?.targetClassification, "internal-db")
        XCTAssertEqual(finding.evidence.authorizationRecords.first?.targetClassification, "public-api")
    }

    /// TC-08 策略版本变化但未重新授权。
    func testTC08_policyVersionChangedWithoutReauthorization() {
        let events = [
            auth(caseId: "TC-08", policy: "v3"),
            delivery(caseId: "TC-08", policy: "v4")
        ]
        let report = Validator().validate(events)

        XCTAssertEqual(report.findings.count, 1)
        let finding = try! XCTUnwrap(report.firstViolation)
        XCTAssertEqual(finding.caseId, "TC-08")
        XCTAssertEqual(finding.reasonCode,
                       ReasonCode.policyVersionChangedWithoutReauthorization.rawValue)
        XCTAssertEqual(finding.evidence.delivery?.policyVersion, "v4")
        XCTAssertEqual(finding.evidence.authorizationRecords.first?.policyVersion, "v3")
        XCTAssertTrue(finding.reason.contains("策略版本"))
    }

    // MARK: - 时序与冲突

    /// TC-09 时序不一致：授权记录时间晚于投递时间。
    func testTC09_authorizationTimestampAfterDelivery() {
        let events = [
            auth(caseId: "TC-09", eventTime: "2026-11-14T12:00:00Z"),
            delivery(caseId: "TC-09", eventTime: "2026-11-14T09:00:00Z")
        ]
        let report = Validator().validate(events)

        XCTAssertEqual(report.findings.count, 1)
        let finding = try! XCTUnwrap(report.firstViolation)
        XCTAssertEqual(finding.caseId, "TC-09")
        XCTAssertEqual(finding.reasonCode,
                       ReasonCode.authorizationTimestampAfterDelivery.rawValue)
        XCTAssertTrue(finding.reason.contains("时序"))
    }

    /// TC-10 同一决策 ID 存在互相冲突的授权记录。
    func testTC10_conflictingAuthorizationRecords() {
        let events = [
            auth(caseId: "TC-10", stage: 1, decisionId: "dec-1"),
            auth(caseId: "TC-10", stage: 2, decisionId: "dec-1", eventTime: "2026-11-14T08:30:00Z"),
            delivery(caseId: "TC-10", stage: 2, decisionId: "dec-1")
        ]
        let report = Validator().validate(events)

        let conflict = report.findings.first {
            $0.reasonCode == ReasonCode.conflictingAuthorizationRecords.rawValue
        }
        XCTAssertNotNil(conflict, "TC-10 期望报告授权记录冲突")
        XCTAssertEqual(conflict?.decisionId, "dec-1")
        XCTAssertEqual(conflict?.evidence.authorizationRecords.count, 2)
        XCTAssertTrue(conflict!.reason.contains("冲突"))
    }

    /// TC-11 批准记录缺少有效截止时间。
    func testTC11_approvalMissingValidUntil() {
        let events = [
            auth(caseId: "TC-11", validUntil: nil),
            delivery(caseId: "TC-11")
        ]
        let report = Validator().validate(events)

        let finding = report.findings.first {
            $0.reasonCode == ReasonCode.approvalMissingValidUntil.rawValue
        }
        XCTAssertNotNil(finding, "TC-11 期望报告批准缺少截止时间")
        XCTAssertEqual(finding?.decisionId, "dec-1")
        XCTAssertTrue(finding!.reason.contains("有效截止时间"))
    }

    /// TC-12 事件时间无法解析。
    func testTC12_invalidTimestamp() {
        let events = [
            auth(caseId: "TC-12", eventTime: "not-a-timestamp"),
            delivery(caseId: "TC-12", eventTime: "2026-11-14T09:00:00Z")
        ]
        let report = Validator().validate(events)

        let finding = report.findings.first {
            $0.reasonCode == ReasonCode.invalidTimestamp.rawValue
        }
        XCTAssertNotNil(finding, "TC-12 期望报告非法时间戳")
        XCTAssertEqual(finding?.eventTime, "not-a-timestamp")
    }

    // MARK: - 边界场景

    /// TC-13 边界：投递时间恰好等于批准截止时间，仍然有效。
    func testTC13_deliveryExactlyAtValidUntilIsCompliant() {
        let events = [
            auth(caseId: "TC-13", validUntil: "2026-11-14T10:00:00Z"),
            delivery(caseId: "TC-13", eventTime: "2026-11-14T10:00:00Z")
        ]
        let report = Validator().validate(events)

        XCTAssertTrue(report.isCompliant, "TC-13 截止时刻投递应视为有效，实际: \(report.findings.map(\.reasonCode))")
    }

    /// TC-14 边界：带小数秒的 ISO8601 时间戳可正确解析，到期判定正确。
    func testTC14_fractionalSecondsTimestamp() {
        let events = [
            auth(caseId: "TC-14",
                 eventTime: "2026-11-14T08:00:00.000Z",
                 validUntil: "2026-11-14T10:00:00.500Z"),
            delivery(caseId: "TC-14", eventTime: "2026-11-14T10:00:00.501Z")
        ]
        let report = Validator().validate(events)

        XCTAssertEqual(report.findings.first?.reasonCode,
                       ReasonCode.authorizationExpired.rawValue)
    }

    /// TC-15 边界：同一决策 ID 的完全重复授权记录不算冲突，按最新记录核对。
    func testTC15_duplicateIdenticalAuthorizationsAreNotConflict() {
        let events = [
            auth(caseId: "TC-15", eventTime: "2026-11-14T08:00:00Z"),
            auth(caseId: "TC-15", eventTime: "2026-11-14T08:05:00Z"),
            delivery(caseId: "TC-15", eventTime: "2026-11-14T09:00:00Z")
        ]
        let report = Validator().validate(events)

        XCTAssertTrue(report.isCompliant, "TC-15 重复记录不应判冲突，实际: \(report.findings.map(\.reasonCode))")
    }

    // MARK: - 第一处违反的排序语义

    /// TC-16 多事件混合：报告按事件时间升序排列，firstViolation 指向最早的违反。
    func testTC16_firstViolationIsChronologicallyEarliest() {
        let events = [
            auth(caseId: "TC-16-late", decisionId: "dec-late",
                 eventTime: "2026-11-14T08:00:00Z", validUntil: "2026-11-14T10:00:00Z"),
            delivery(caseId: "TC-16-late", decisionId: "dec-late",
                     eventTime: "2026-11-14T15:00:00Z"),
            delivery(caseId: "TC-16-early", decisionId: "missing-dec",
                     eventTime: "2026-11-14T09:00:00Z")
        ]
        let report = Validator().validate(events)

        XCTAssertEqual(report.findings.count, 2)
        let first = try! XCTUnwrap(report.firstViolation)
        XCTAssertEqual(first.caseId, "TC-16-early")
        XCTAssertEqual(first.reasonCode, ReasonCode.approvalRecordMissing.rawValue)
        XCTAssertEqual(report.findings.map(\.caseId), ["TC-16-early", "TC-16-late"])
    }

    /// TC-17 多个请求互不干扰：一个合规、一个违规，只报告违规请求。
    func testTC17_multipleRequestsIsolation() {
        let events = [
            auth(caseId: "TC-17", requestId: "req-ok", decisionId: "dec-ok"),
            delivery(caseId: "TC-17", requestId: "req-ok", decisionId: "dec-ok"),
            auth(caseId: "TC-17", requestId: "req-bad", decisionId: "dec-bad",
                 decision: .deny, validUntil: nil),
            delivery(caseId: "TC-17", requestId: "req-bad", decisionId: "dec-bad")
        ]
        let report = Validator().validate(events)

        XCTAssertEqual(report.findings.count, 1)
        XCTAssertEqual(report.findings.first?.requestId, "req-bad")
        XCTAssertEqual(report.findings.first?.reasonCode,
                       ReasonCode.deliveryUnderDeniedDecision.rawValue)
    }

    // MARK: - 解析器：失败路径

    /// TC-18 解析顶层数组结构。
    func testTC18_parserAcceptsTopLevelArray() throws {
        let json = """
        [
          {"event_type":"authorization","request_id":"r1","stage":1,"target_classification":"t",
           "policy_version":"v1","decision_id":"d1","event_time":"2026-11-14T08:00:00Z",
           "decision":"approve","valid_until":"2026-11-14T18:00:00Z"},
          {"event_type":"delivery","request_id":"r1","stage":1,"target_classification":"t",
           "policy_version":"v1","decision_id":"d1","event_time":"2026-11-14T09:00:00Z"}
        ]
        """.data(using: .utf8)!

        let events = try EventFileParser.parseData(json)
        XCTAssertEqual(events.count, 2)
        XCTAssertEqual(events.first?.decision, .approve)
        XCTAssertEqual(events.last?.eventType, .delivery)
        XCTAssertTrue(Validator().validate(events).isCompliant)
    }

    /// TC-19 解析 events 包装与 authorizations/deliveries 分离包装。
    func testTC19_parserAcceptsWrapperFormats() throws {
        let wrapperJSON = """
        {"events":[
          {"event_type":"authorization","request_id":"r1","stage":1,"target_classification":"t",
           "policy_version":"v1","decision_id":"d1","event_time":"2026-11-14T08:00:00Z",
           "decision":"approve","valid_until":"2026-11-14T18:00:00Z"}
        ]}
        """.data(using: .utf8)!
        let splitJSON = """
        {
          "authorizations":[
            {"event_type":"authorization","request_id":"r1","stage":1,"target_classification":"t",
             "policy_version":"v1","decision_id":"d1","event_time":"2026-11-14T08:00:00Z",
             "decision":"approve","valid_until":"2026-11-14T18:00:00Z"}
          ],
          "deliveries":[
            {"event_type":"delivery","request_id":"r1","stage":1,"target_classification":"t",
             "policy_version":"v1","decision_id":"d1","event_time":"2026-11-14T09:00:00Z"}
          ]
        }
        """.data(using: .utf8)!

        let fromWrapper = try EventFileParser.parseData(wrapperJSON)
        let fromSplit = try EventFileParser.parseData(splitJSON)
        XCTAssertEqual(fromWrapper.count, 1)
        XCTAssertEqual(fromSplit.count, 2)
        XCTAssertTrue(Validator().validate(fromSplit).isCompliant)
    }

    /// TC-20 解析失败路径：非法 JSON、无法识别的结构、缺少必需字段。
    func testTC20_parserFailurePaths() {
        XCTAssertThrowsError(try EventFileParser.parseData(Data("not json".utf8))) { error in
            guard case EventFileError.invalidJSON = error else {
                return XCTFail("TC-20 期望 invalidJSON，实际 \(error)")
            }
        }

        XCTAssertThrowsError(try EventFileParser.parseData(Data("{\"foo\":1}".utf8))) { error in
            guard case EventFileError.unexpectedStructure = error else {
                return XCTFail("TC-20 期望 unexpectedStructure，实际 \(error)")
            }
        }

        let missingField = Data("""
        [{"event_type":"delivery","request_id":"r1"}]
        """.utf8)
        XCTAssertThrowsError(try EventFileParser.parseData(missingField)) { error in
            guard case EventFileError.decodeFailed = error else {
                return XCTFail("TC-20 期望 decodeFailed，实际 \(error)")
            }
        }

        XCTAssertThrowsError(try EventFileParser.parseFile(at: "/nonexistent/path/no-such.json")) { error in
            guard case EventFileError.fileReadFailed = error else {
                return XCTFail("TC-20 期望 fileReadFailed，实际 \(error)")
            }
        }
    }

    /// TC-21 报告 JSON 可编码且包含第一处违反的证据字段。
    func testTC21_reportIsJSONEncodableWithEvidence() throws {
        let events = [
            auth(caseId: "TC-21", stage: 1),
            delivery(caseId: "TC-21", stage: 2)
        ]
        let report = Validator().validate(events)
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        encoder.outputFormatting = [.sortedKeys]
        let data = try encoder.encode(report)
        let text = String(data: data, encoding: .utf8)!

        XCTAssertTrue(text.contains("stage_changed_without_reauthorization"))
        XCTAssertFalse(text.contains("first_violation"),
                       "ValidationReport 只通过属性暴露 firstViolation，编码结果不应包含该字段")
        XCTAssertTrue(text.contains("authorization_records"))
        XCTAssertTrue(text.contains("\"delivery\""))
    }

    /// TC-22 空输入：没有任何事件时报告合规且计数为零。
    func testTC22_emptyInputIsCompliant() {
        let report = Validator().validate([])
        XCTAssertTrue(report.isCompliant)
        XCTAssertEqual(report.totalEvents, 0)
        XCTAssertNil(report.firstViolation)
    }
}
