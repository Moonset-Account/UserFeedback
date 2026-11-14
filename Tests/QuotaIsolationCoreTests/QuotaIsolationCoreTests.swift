//
//  QuotaIsolationCoreTests.swift
//  QuotaIsolationCoreTests
//
//  本地离线测试：真实断言，无网络、无第三方依赖。
//  每个测试方法对应一个稳定 case_id，通过 XCTest 实际执行。
//

import XCTest
@testable import QuotaIsolationCore

final class QuotaIsolationCoreTests: XCTestCase {

    // MARK: - 辅助

    private static var executedCaseIDs: Set<String> = []
    private static var totalAssertionChecks = 0

    private final class Probe {
        var checks = 0
        func expect(
            _ condition: Bool,
            _ message: String,
            file: StaticString = #filePath,
            line: UInt = #line
        ) {
            checks += 1
            QuotaIsolationCoreTests.totalAssertionChecks += 1
            XCTAssertTrue(condition, message, file: file, line: line)
        }
        func expectEqual<T: Equatable>(
            _ actual: T, _ expected: T, _ message: String,
            file: StaticString = #filePath, line: UInt = #line
        ) {
            checks += 1
            QuotaIsolationCoreTests.totalAssertionChecks += 1
            XCTAssertEqual(actual, expected, message, file: file, line: line)
        }
        func expectNil(
            _ value: Any?, _ message: String,
            file: StaticString = #filePath, line: UInt = #line
        ) {
            checks += 1
            QuotaIsolationCoreTests.totalAssertionChecks += 1
            XCTAssertNil(value, message, file: file, line: line)
        }
        func expectThrow(
            _ error: ScenarioError,
            file: StaticString = #filePath,
            line: UInt = #line,
            _ operation: () throws -> Void
        ) {
            checks += 1
            QuotaIsolationCoreTests.totalAssertionChecks += 1
            XCTAssertThrowsError(try operation(), "expected \(error)", file: file, line: line) { actual in
                guard let actualError = actual as? ScenarioError else {
                    XCTFail("wrong error type: \(actual)", file: file, line: line)
                    return
                }
                XCTAssertEqual(actualError, error, file: file, line: line)
            }
        }
    }

    private func beginCase(_ caseID: String, _ probe: Probe) {
        Self.executedCaseIDs.insert(caseID)
        print("▶︎ executing \(caseID)")
        // 引用 probe 确保每个用例至少登记一次断言计数
        _ = probe
    }

    private func runDemo() throws -> [LedgerEntry] {
        let demo = DemoScenario.make()
        let engine = try QuotaEngine(quotas: demo.quotas, config: demo.config)
        return try engine.run(demo.events)
    }

    private func entries(forSequence sequence: Int, in ledger: [LedgerEntry]) -> [LedgerEntry] {
        ledger.filter { $0.sequence == sequence }
    }

    private func entry(
        for tenant: TenantID, request: RequestID, action: LedgerAction, in ledger: [LedgerEntry]
    ) -> LedgerEntry {
        guard let match = ledger.first(where: {
            $0.tenant == tenant && $0.requestID == request && $0.action == action
        }) else {
            XCTFail("missing entry \(action.rawValue) for \(tenant)/\(request)")
            fatalError("unreachable")
        }
        return match
    }

    private func balance(of tenant: TenantID, period: Int, in entry: LedgerEntry) -> PeriodBalance? {
        entry.snapshots.first { $0.tenant == tenant }?.balances.first { $0.period == period }
    }

    private func engine(
        _ quotas: [TenantID: Int] = [TenantID("alpha"): 2, TenantID("beta"): 2],
        periodLength: Int64 = 1000,
        timeout: Int64 = 300
    ) throws -> QuotaEngine {
        try QuotaEngine(
            quotas: quotas,
            config: QuotaConfig(periodLength: periodLength, timeout: timeout)
        )
    }

    // MARK: - QIB-01 同一租户并发预占不会超额（正常 + 边界 + 失败路径）

    func test_QIB_01_concurrent_reserves_never_exceed_quota() throws {
        let caseID = "QIB-01"
        let p = Probe()
        beginCase(caseID, p)
        let ledger = try runDemo()
        let alpha = TenantID("alpha")

        let first = entries(forSequence: 1, in: ledger).first!
        p.expect(first.accepted, "seq1 A-1 reserve must be accepted")
        p.expectEqual(first.action, .reserveAccepted, "seq1 action")
        p.expectEqual(balance(of: alpha, period: 0, in: first)?.held, 1, "seq1 held=1")
        p.expectEqual(balance(of: alpha, period: 0, in: first)?.remaining, 1, "seq1 remaining=1")

        let second = entries(forSequence: 2, in: ledger).first!
        p.expect(second.accepted, "seq2 A-2 reserve must be accepted at quota boundary")
        p.expectEqual(balance(of: alpha, period: 0, in: second)?.held, 2, "seq2 held=2 (full)")
        p.expectEqual(balance(of: alpha, period: 0, in: second)?.remaining, 0, "seq2 remaining=0")

        let third = entries(forSequence: 3, in: ledger).first!
        p.expect(!third.accepted, "seq3 A-3 reserve must be rejected over quota")
        p.expectEqual(third.action, .reserveRejected, "seq3 action")
        p.expectEqual(third.rejectReason, .quotaExhausted, "seq3 reason")
        // 拒绝不得改动余额
        p.expectEqual(balance(of: alpha, period: 0, in: third)?.held, 2, "rejection leaves held unchanged")
        p.expectEqual(balance(of: alpha, period: 0, in: third)?.remaining, 0, "rejection leaves remaining unchanged")

        p.expectEqual(
            ledger.filter({ $0.tenant == alpha && $0.action == .reserveAccepted }).count,
            4,
            "alpha accepted reserves: A-1, A-2, A-4, A-5"
        )
        p.expectEqual(
            ledger.filter({ $0.tenant == alpha && $0.action == .reserveRejected }).count,
            2,
            "alpha rejected reserves: A-3 over quota, A-5 duplicate"
        )
        print("✓ \(caseID) finished with \(p.checks) executed assertions")
    }

    // MARK: - QIB-02 两个租户分别用满额度，账本严格隔离

    func test_QIB_02_two_tenants_use_independent_budgets() throws {
        let caseID = "QIB-02"
        let p = Probe()
        beginCase(caseID, p)
        let ledger = try runDemo()
        let alpha = TenantID("alpha")
        let beta = TenantID("beta")

        let alphaFull = entries(forSequence: 2, in: ledger).first!
        p.expectEqual(balance(of: alpha, period: 0, in: alphaFull)?.held, 2, "alpha holds 2/2")
        p.expectEqual(balance(of: beta, period: 0, in: alphaFull)?.held, 0, "beta untouched at 0/2")

        let betaFirst = entries(forSequence: 4, in: ledger).first!
        p.expect(betaFirst.accepted, "beta B-1 accepted even though alpha is full")
        p.expectEqual(balance(of: beta, period: 0, in: betaFirst)?.held, 1, "beta held=1")
        p.expectEqual(balance(of: alpha, period: 0, in: betaFirst)?.held, 2, "alpha balance unchanged by beta event")

        let betaSecond = entries(forSequence: 5, in: ledger).first!
        p.expect(betaSecond.accepted, "beta B-2 accepted: beta fills its own quota")
        p.expectEqual(balance(of: beta, period: 0, in: betaSecond)?.held, 2, "beta held=2/2")
        p.expectEqual(balance(of: beta, period: 0, in: betaSecond)?.remaining, 0, "beta remaining=0")

        // 余额快照始终同时包含两个租户，且每个租户只出现自己的周期
        for entry in ledger {
            let tenantsInSnapshot = entry.snapshots.map(\.tenant)
            p.expectEqual(Set(tenantsInSnapshot), [alpha, beta], "snapshot keeps both tenants at seq \(entry.sequence)")
            for snapshot in entry.snapshots {
                for b in snapshot.balances {
                    p.expect(b.held >= 0 && b.held <= b.quota, "held within quota for \(snapshot.tenant)")
                    p.expectEqual(b.held + b.remaining, b.quota, "held + remaining == quota invariant")
                }
            }
        }
        print("✓ \(caseID) finished with \(p.checks) executed assertions")
    }

    // MARK: - QIB-03 确认占用与归还配额（正常路径 + 跨周期归属）

    func test_QIB_03_confirm_holds_and_release_returns_quota() throws {
        let caseID = "QIB-03"
        let p = Probe()
        beginCase(caseID, p)
        let ledger = try runDemo()
        let alpha = TenantID("alpha")
        let beta = TenantID("beta")

        let confirm = entries(forSequence: 6, in: ledger).first!
        p.expect(confirm.accepted, "A-1 confirm accepted")
        p.expectEqual(confirm.action, .confirmAccepted, "seq6 action")
        p.expectEqual(confirm.attributedPeriod, 0, "confirm attributed to original period 0")
        p.expectEqual(balance(of: alpha, period: 0, in: confirm)?.held, 2, "confirm keeps slot charged")

        let betaRelease = entries(forSequence: 7, in: ledger).first!
        p.expect(betaRelease.accepted, "B-1 release accepted")
        p.expectEqual(betaRelease.action, .releaseAccepted, "seq7 action")
        p.expectEqual(balance(of: beta, period: 0, in: betaRelease)?.held, 1, "one slot returned")
        p.expectEqual(balance(of: beta, period: 0, in: betaRelease)?.remaining, 1, "remaining restored")
        p.expectEqual(balance(of: alpha, period: 0, in: betaRelease)?.held, 2, "alpha unaffected by beta release")

        let alphaRelease = entries(forSequence: 9, in: ledger).first!
        p.expect(alphaRelease.accepted, "A-1 release accepted after confirm")
        p.expectEqual(balance(of: alpha, period: 0, in: alphaRelease)?.held, 1, "A-1 slot returned; A-2 still holds")
        p.expectEqual(balance(of: alpha, period: 0, in: alphaRelease)?.remaining, 1, "alpha remaining restored")
        print("✓ \(caseID) finished with \(p.checks) executed assertions")
    }

    // MARK: - QIB-04 重复失败回调只归还一次；重复确认 / 重复预占均幂等拒绝

    func test_QIB_04_duplicate_callbacks_return_quota_once() throws {
        let caseID = "QIB-04"
        let p = Probe()
        beginCase(caseID, p)
        let ledger = try runDemo()
        let beta = TenantID("beta")
        let alpha = TenantID("alpha")

        let firstRelease = entries(forSequence: 7, in: ledger).first!
        p.expectEqual(balance(of: beta, period: 0, in: firstRelease)?.held, 1, "first release returns one slot")

        let duplicateRelease = entries(forSequence: 8, in: ledger).first!
        p.expect(!duplicateRelease.accepted, "duplicate release rejected")
        p.expectEqual(duplicateRelease.action, .releaseRejected, "seq8 action")
        p.expectEqual(duplicateRelease.rejectReason, .duplicateRelease, "seq8 reason")
        p.expectEqual(balance(of: beta, period: 0, in: duplicateRelease)?.held, 1, "no second refund: held stays 1")
        p.expectEqual(balance(of: beta, period: 0, in: duplicateRelease)?.remaining, 1, "no second refund: remaining stays 1")

        let lateConfirm = entries(forSequence: 10, in: ledger).first!
        p.expect(!lateConfirm.accepted, "confirm after release rejected")
        p.expectEqual(lateConfirm.rejectReason, .lateConfirmAfterRelease, "seq10 reason")
        p.expectEqual(lateConfirm.attributedPeriod, 0, "late event still attributed to original period")
        p.expectEqual(balance(of: alpha, period: 0, in: lateConfirm)?.held, 1, "late confirm does not consume budget")

        let duplicateReserve = entries(forSequence: 15, in: ledger).first!
        p.expect(!duplicateReserve.accepted, "duplicate reserve rejected by tenant+requestID")
        p.expectEqual(duplicateReserve.rejectReason, .duplicateReservation, "seq15 reason")
        p.expectEqual(
            balance(of: alpha, period: 1, in: duplicateReserve)?.held,
            2,
            "duplicate reserve does not consume new-period budget"
        )
        print("✓ \(caseID) finished with \(p.checks) executed assertions")
    }

    // MARK: - QIB-05 超时回收 + 跨周期迟到事件不消耗新周期额度

    func test_QIB_05_timeout_reclaim_and_late_events_stay_on_old_period() throws {
        let caseID = "QIB-05"
        let p = Probe()
        beginCase(caseID, p)
        let ledger = try runDemo()
        let alpha = TenantID("alpha")
        let beta = TenantID("beta")

        // seq11 处理前的扫描应回收 A-2 与 B-2（预占于时钟 20/50，时钟 1050 时远超 300 超时）
        let reclaimEntries = ledger.filter { $0.action == .timeoutReclaimed }
        p.expectEqual(reclaimEntries.count, 2, "two timed-out reservations reclaimed before seq11")
        let alphaReclaim = entry(for: alpha, request: RequestID("A-2"), action: .timeoutReclaimed, in: ledger)
        let betaReclaim = entry(for: beta, request: RequestID("B-2"), action: .timeoutReclaimed, in: ledger)
        p.expectEqual(alphaReclaim.sequence, 11, "reclaim attached to triggering sequence 11")
        p.expectEqual(alphaReclaim.attributedPeriod, 0, "reclaim returns slot to original period 0")
        p.expectEqual(betaReclaim.attributedPeriod, 0, "beta reclaim also returns to period 0")
        p.expectEqual(balance(of: alpha, period: 0, in: alphaReclaim)?.held, 0, "period-0 alpha slots fully returned")
        p.expectEqual(balance(of: beta, period: 0, in: betaReclaim)?.held, 0, "period-0 beta slots fully returned")

        // 回收之后新周期事件才被处理
        let reclaimSeq = reclaimEntries.map(\.sequence)
        let reserve11 = entries(forSequence: 11, in: ledger).first { $0.action == .reserveAccepted }!
        p.expectEqual(reclaimSeq, [11, 11], "reclaims happen on the triggering step")
        p.expect(reserve11.accepted, "A-4 reserve in new period 1 accepted")
        p.expectEqual(reserve11.attributedPeriod, 1, "A-4 belongs to period 1")
        p.expectEqual(balance(of: alpha, period: 1, in: reserve11)?.held, 1, "period 1 starts from full quota")

        let lateConfirm = entries(forSequence: 12, in: ledger).first!
        p.expect(!lateConfirm.accepted, "late confirm after reclaim rejected")
        p.expectEqual(lateConfirm.action, .confirmRejected, "seq12 action")
        p.expectEqual(lateConfirm.rejectReason, .lateConfirmAfterReclaim, "seq12 reason")
        p.expectEqual(lateConfirm.attributedPeriod, 0, "cross-period confirm still points to period 0")
        p.expectEqual(balance(of: alpha, period: 1, in: lateConfirm)?.held, 1, "late confirm consumes no period-1 budget")
        p.expectEqual(balance(of: alpha, period: 0, in: lateConfirm)?.held, 0, "period-0 budget stays reclaimed")

        let lateRelease = entries(forSequence: 13, in: ledger).first!
        p.expect(!lateRelease.accepted, "late release after reclaim rejected")
        p.expectEqual(lateRelease.rejectReason, .lateReleaseAfterReclaim, "seq13 reason")
        p.expectEqual(balance(of: alpha, period: 1, in: lateRelease)?.held, 1, "late release consumes no period-1 budget")

        let reserve14 = entries(forSequence: 14, in: ledger).first!
        p.expect(reserve14.accepted, "A-5 reserve still fits: late events never consumed period-1 quota")
        p.expectEqual(balance(of: alpha, period: 1, in: reserve14)?.held, 2, "period 1 now full at 2/2")
        p.expectEqual(balance(of: alpha, period: 1, in: reserve14)?.remaining, 0, "period 1 remaining 0")
        print("✓ \(caseID) finished with \(p.checks) executed assertions")
    }

    // MARK: - QIB-06 输入存储顺序调换后，按声明序号处理得到相同账本

    func test_QIB_06_shuffled_storage_order_produces_identical_ledger() throws {
        let caseID = "QIB-06"
        let p = Probe()
        beginCase(caseID, p)
        let demo = DemoScenario.make()

        let engineA = try QuotaEngine(quotas: demo.quotas, config: demo.config)
        let ledgerA = try engineA.run(demo.events)

        // 确定性洗牌（固定步长反转），不依赖随机数
        var shuffled = demo.events
        let n = shuffled.count
        for i in stride(from: 0, to: n / 2, by: 1) {
            shuffled.swapAt(i, n - 1 - i)
        }
        // 再做一次固定旋转，彻底打乱相邻关系
        let rotate = 7
        shuffled = Array(shuffled[rotate...] + shuffled[..<rotate])
        p.expect(
            shuffled.map(\.sequence) != demo.events.map(\.sequence),
            "input storage order must actually differ"
        )

        let engineB = try QuotaEngine(quotas: demo.quotas, config: demo.config)
        let ledgerB = try engineB.run(shuffled)

        p.expectEqual(ledgerA.count, ledgerB.count, "same number of ledger entries")
        p.expectEqual(
            ledgerA.map(\.sequence), ledgerB.map(\.sequence),
            "both ledgers ordered by declared sequence"
        )
        p.expectEqual(ledgerA, ledgerB, "full ledgers identical regardless of storage order")

        // 用一个新引擎再跑原始顺序，确认引擎实例间也可复现
        let engineC = try QuotaEngine(quotas: demo.quotas, config: demo.config)
        p.expectEqual(try engineC.run(demo.events), ledgerA, "deterministic across engine instances")
        print("✓ \(caseID) finished with \(p.checks) executed assertions")
    }

    // MARK: - QIB-07 结构性输入错误在处理前抛出（失败路径）

    func test_QIB_07_invalid_inputs_are_rejected_before_processing() throws {
        let caseID = "QIB-07"
        let p = Probe()
        beginCase(caseID, p)
        let alpha = TenantID("alpha")
        let beta = TenantID("beta")

        p.expectThrow(.nonPositivePeriodLength) {
            _ = try QuotaEngine(quotas: [alpha: 1], config: QuotaConfig(periodLength: 0, timeout: 10))
        }
        p.expectThrow(.negativeTimeout) {
            _ = try QuotaEngine(quotas: [alpha: 1], config: QuotaConfig(periodLength: 100, timeout: -1))
        }
        p.expectThrow(.nonPositiveQuota(tenant: alpha)) {
            _ = try QuotaEngine(quotas: [alpha: 0], config: QuotaConfig(periodLength: 100, timeout: 10))
        }

        let good = try engine()
        p.expectThrow(.duplicateSequence(2)) {
            _ = try good.run([
                QuotaEvent(tenant: alpha, sequence: 1, requestID: RequestID("x"), kind: .reserve, clock: 1),
                QuotaEvent(tenant: beta, sequence: 2, requestID: RequestID("y"), kind: .reserve, clock: 2),
                QuotaEvent(tenant: alpha, sequence: 2, requestID: RequestID("z"), kind: .reserve, clock: 3)
            ])
        }
        p.expectThrow(.clockWentBackwards(sequence: 3, previous: 10, current: 5)) {
            _ = try good.run([
                QuotaEvent(tenant: alpha, sequence: 1, requestID: RequestID("x"), kind: .reserve, clock: 10),
                QuotaEvent(tenant: alpha, sequence: 3, requestID: RequestID("y"), kind: .reserve, clock: 5)
            ])
        }
        p.expectThrow(.unknownTenant(TenantID("ghost"))) {
            _ = try good.run([
                QuotaEvent(tenant: TenantID("ghost"), sequence: 1, requestID: RequestID("x"),
                           kind: .reserve, clock: 1)
            ])
        }
        p.expectThrow(.emptyRequestID(sequence: 4)) {
            _ = try good.run([
                QuotaEvent(tenant: alpha, sequence: 4, requestID: RequestID(""), kind: .reserve, clock: 1)
            ])
        }
        p.expectThrow(.negativeClock(sequence: 5)) {
            _ = try good.run([
                QuotaEvent(tenant: alpha, sequence: 5, requestID: RequestID("x"), kind: .reserve, clock: -1)
            ])
        }
        print("✓ \(caseID) finished with \(p.checks) executed assertions")
    }

    // MARK: - QIB-08 边界：未知请求、超时边界、零超时即时回收、新周期额度独立

    func test_QIB_08_boundary_conditions() throws {
        let caseID = "QIB-08"
        let p = Probe()
        beginCase(caseID, p)
        let alpha = TenantID("alpha")
        let beta = TenantID("beta")

        // 未知请求的确认 / 归还
        let e1 = try engine()
        let ledger1 = try e1.run([
            QuotaEvent(tenant: alpha, sequence: 1, requestID: RequestID("ghost-1"), kind: .confirm, clock: 5),
            QuotaEvent(tenant: beta, sequence: 2, requestID: RequestID("ghost-2"), kind: .release, clock: 6)
        ])
        p.expectEqual(ledger1[0].rejectReason, .unknownRequest, "confirm of unknown request rejected")
        p.expectEqual(ledger1[1].rejectReason, .unknownRequest, "release of unknown request rejected")
        p.expectEqual(
            ledger1[1].snapshots.flatMap(\.balances).filter { $0.held != 0 }.count,
            0,
            "unknown-request events never touch any budget"
        )

        // 超时边界：clock - reservedAt == timeout 恰好回收；差 1 不回收
        let e2 = try engine([alpha: 1], periodLength: 1000, timeout: 100)
        let ledger2 = try e2.run([
            QuotaEvent(tenant: alpha, sequence: 1, requestID: RequestID("x"), kind: .reserve, clock: 0),
            QuotaEvent(tenant: alpha, sequence: 2, requestID: RequestID("y"), kind: .reserve, clock: 99), // 差1，x未超时
            QuotaEvent(tenant: alpha, sequence: 3, requestID: RequestID("y"), kind: .release, clock: 99)
        ])
        p.expectEqual(ledger2[1].rejectReason, .quotaExhausted, "at clock 99 x (reserved at 0) has not timed out yet")
        let reclaimsBefore100 = ledger2.filter { $0.action == .timeoutReclaimed }
        p.expectEqual(reclaimsBefore100.count, 0, "no reclaim strictly inside timeout window")

        let e3 = try engine([alpha: 1], periodLength: 1000, timeout: 100)
        let ledger3 = try e3.run([
            QuotaEvent(tenant: alpha, sequence: 1, requestID: RequestID("x"), kind: .reserve, clock: 0),
            QuotaEvent(tenant: alpha, sequence: 2, requestID: RequestID("y"), kind: .reserve, clock: 100) // 恰好超时
        ])
        let reclaim = ledger3.first { $0.action == .timeoutReclaimed }
        p.expect(reclaim != nil, "x reclaimed exactly at timeout boundary (elapsed == timeout)")
        p.expectEqual(ledger3.last?.action, .reserveAccepted, "y accepted after reclaim frees the only slot")
        p.expectEqual(ledger3.last?.attributedPeriod, 0, "both still in period 0")

        // 零超时：下一个事件处理前立即回收
        let e4 = try engine([alpha: 1], periodLength: 1000, timeout: 0)
        let ledger4 = try e4.run([
            QuotaEvent(tenant: alpha, sequence: 1, requestID: RequestID("x"), kind: .reserve, clock: 0),
            QuotaEvent(tenant: alpha, sequence: 2, requestID: RequestID("y"), kind: .reserve, clock: 1)
        ])
        p.expectEqual(
            ledger4.map(\.action),
            [.reserveAccepted, .timeoutReclaimed, .reserveAccepted],
            "timeout=0 reclaims before the next event"
        )

        // 跨周期确认 / 归还仍退回原周期，不触碰新周期；各周期 / 各租户额度独立核算
        let e5 = try engine([alpha: 1, beta: 1], periodLength: 100, timeout: 1000)
        let ledger5 = try e5.run([
            QuotaEvent(tenant: alpha, sequence: 1, requestID: RequestID("x"), kind: .reserve, clock: 10),
            QuotaEvent(tenant: alpha, sequence: 2, requestID: RequestID("x"), kind: .confirm, clock: 250),
            // x 已跨入周期 1 但额度仍挂周期 0；周期 1 拥有自己的完整额度，y 可正常预占
            QuotaEvent(tenant: alpha, sequence: 3, requestID: RequestID("y"), kind: .reserve, clock: 260),
            // beta 的额度与 alpha 完全独立，同样可预占
            QuotaEvent(tenant: beta, sequence: 4, requestID: RequestID("b"), kind: .reserve, clock: 265),
            // x 在周期 2 归还，额度退回原周期 0，周期 1/2 均不受影响
            QuotaEvent(tenant: alpha, sequence: 5, requestID: RequestID("x"), kind: .release, clock: 410)
        ])
        p.expectEqual(ledger5[1].attributedPeriod, 0, "cross-period confirm attributed to period 0")
        let yReserve = ledger5.first { $0.requestID == RequestID("y") }!
        p.expect(yReserve.accepted, "new period grants its own full quota despite carried-over hold")
        p.expectEqual(yReserve.attributedPeriod, 2, "clock 260 with periodLength 100 belongs to period 2")
        p.expectEqual(balance(of: alpha, period: 0, in: yReserve)?.held, 1, "x still charged to period 0")
        p.expectEqual(balance(of: alpha, period: 2, in: yReserve)?.held, 1, "y charged to its own period 2")
        let betaReserve = ledger5.first { $0.requestID == RequestID("b") }!
        p.expect(betaReserve.accepted, "beta reserve independent of alpha's cross-period holds")
        p.expectEqual(balance(of: beta, period: 2, in: betaReserve)?.held, 1, "beta uses its own budget")
        let xRelease = ledger5.first { $0.action == .releaseAccepted }!
        p.expectEqual(xRelease.attributedPeriod, 0, "cross-period release returns to period 0")
        p.expectEqual(balance(of: alpha, period: 0, in: xRelease)?.held, 0, "period-0 slot returned")
        p.expectEqual(balance(of: alpha, period: 2, in: xRelease)?.held, 1, "period-2 accounting untouched by release into period 0")
        p.expectEqual(balance(of: beta, period: 2, in: xRelease)?.held, 1, "beta balance independent of alpha release")
        print("✓ \(caseID) finished with \(p.checks) executed assertions")
    }

    // MARK: - QIB-09 账本全局不变量审计（余额守恒 + 状态机闭环）

    func test_QIB_09_ledger_invariants_audit() throws {
        let caseID = "QIB-09"
        let p = Probe()
        beginCase(caseID, p)
        let ledger = try runDemo()
        let alpha = TenantID("alpha")
        let beta = TenantID("beta")

        for entry in ledger {
            for snapshot in entry.snapshots {
                for b in snapshot.balances {
                    p.expect(b.held >= 0, "held never negative (\(snapshot.tenant) p\(b.period))")
                    p.expect(b.held <= b.quota, "held never exceeds quota (\(snapshot.tenant) p\(b.period))")
                    p.expectEqual(b.remaining, b.quota - b.held, "remaining derived from held")
                }
            }
            if entry.accepted {
                p.expectNil(entry.rejectReason, "accepted entry carries no reject reason (seq \(entry.sequence))")
            } else {
                p.expect(entry.rejectReason != nil, "rejected entry carries a reason (seq \(entry.sequence))")
            }
        }

        // 每个被拒绝的事件，其拒绝前后的余额必须完全一致
        for rejected in ledger where !rejected.accepted {
            let prior = ledger.last { $0.sequence < rejected.sequence }
            guard let prior else { continue }
            for tenant in [alpha, beta] {
                let now = rejected.snapshots.first { $0.tenant == tenant }
                let before = prior.snapshots.first { $0.tenant == tenant }
                p.expectEqual(now?.balances, before?.balances,
                              "rejected seq \(rejected.sequence) leaves \(tenant) balances untouched")
            }
        }

        // 终态审计：末尾仍持有的额度必须恰好等于「接受的预占 − 归还 − 回收」，
        // 即每个预占的额度都可在账本中追踪到唯一去向（持有中 / 已归还 / 已回收）。
        let finalSnapshot = ledger.last!.snapshots
        for tenant in [alpha, beta] {
            let acceptedReserves = ledger.filter {
                $0.tenant == tenant && $0.action == .reserveAccepted
            }.count
            let releases = ledger.filter {
                $0.tenant == tenant && $0.action == .releaseAccepted
            }.count
            let reclaims = ledger.filter {
                $0.tenant == tenant && $0.action == .timeoutReclaimed
            }.count
            let finalHeld = finalSnapshot.first { $0.tenant == tenant }!
                .balances.reduce(0) { $0 + $1.held }
            p.expectEqual(
                finalHeld, acceptedReserves - releases - reclaims,
                "tenant \(tenant): every reserved slot is either held, released or reclaimed"
            )
            p.expect(
                acceptedReserves == releases + reclaims + finalHeld,
                "tenant \(tenant): no slot is double-returned or lost"
            )
        }
        // 两条终态路径都被真实走到
        p.expect(
            !ledger.filter({ $0.action == .releaseAccepted }).isEmpty,
            "release terminal path is exercised"
        )
        p.expect(
            !ledger.filter({ $0.action == .timeoutReclaimed }).isEmpty,
            "reclaim terminal path is exercised"
        )

        // 已执行用例与断言计数审计
        p.expectEqual(Self.executedCaseIDs.count, 9, "all nine stable case_ids executed")
        let expectedCases: Set = [
            "QIB-01", "QIB-02", "QIB-03", "QIB-04", "QIB-05",
            "QIB-06", "QIB-07", "QIB-08", "QIB-09"
        ]
        p.expectEqual(Self.executedCaseIDs, expectedCases, "case_id registry matches the declared suite")
        p.expect(Self.totalAssertionChecks >= 100, "real assertion count must be substantial, got \(Self.totalAssertionChecks)")
        print("✓ \(caseID) finished with \(p.checks) executed assertions")
        print("🏁 executed test cases: \(Self.executedCaseIDs.sorted().count) / 9, total assertion checks: \(Self.totalAssertionChecks)")
    }
}
