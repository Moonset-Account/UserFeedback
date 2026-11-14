//
//  QuotaEngine.swift
//  QuotaIsolationCore
//
//  固定配额周期 + 虚拟时钟的多租户配额隔离引擎。
//
//  规则：
//  1. 每个租户拥有完全独立的账本；一个租户用满额度不会影响另一个租户。
//  2. 事件按「声明的事件序号」处理，与输入记录的存储顺序无关。
//  3. 请求先预占 1 个额度，之后按结果确认占用、归还配额；超时未确认的预占被回收。
//  4. 重复的确认 / 归还按「租户 + 请求 ID」识别，重复失败回调只归还一次。
//  5. 跨周期到达的事件仍关联到原预占记录，任何后续动作都不消耗新周期额度。
//

private struct Reservation {
    var state: ReservationState
    /// 原预占所在周期；跨周期事件的归属始终指向它
    var period: Int
    /// 原预占时的虚拟时钟，用于判定超时
    var reservedAt: Int64
}

private struct PeriodAccount {
    var quota: Int
    var held: Int = 0
}

public final class QuotaEngine: @unchecked Sendable {
    private let config: QuotaConfig
    private let quotas: [TenantID: Int]
    private var accounts: [TenantID: [Int: PeriodAccount]] = [:]
    private var reservations: [TenantID: [RequestID: Reservation]] = [:]
    /// 已观察到的最大周期号；快照据此为每个租户输出完整周期区间（含零持有周期）
    private var maxObservedPeriod: Int = -1

    public init(quotas: [TenantID: Int], config: QuotaConfig) throws {
        if config.periodLength <= 0 { throw ScenarioError.nonPositivePeriodLength }
        if config.timeout < 0 { throw ScenarioError.negativeTimeout }
        for (tenant, quota) in quotas {
            if tenant.rawValue.isEmpty { throw ScenarioError.emptyTenantID(sequence: -1) }
            if quota <= 0 { throw ScenarioError.nonPositiveQuota(tenant: tenant) }
        }
        self.config = config
        self.quotas = quotas
        for tenant in quotas.keys {
            accounts[tenant] = [:]
            reservations[tenant] = [:]
        }
    }

    /// 按声明的事件序号处理全部事件并输出逐步账本。
    /// - Parameter events: 任意存储顺序的输入记录
    /// - Returns: 与事件序号顺序一致的账本（含超时回收产生的附加条目）
    public func run(_ events: [QuotaEvent]) throws -> [LedgerEntry] {
        try validate(events)
        let ordered = events.sorted { $0.sequence < $1.sequence }

        var ledger: [LedgerEntry] = []
        var lastClock: Int64?
        for event in ordered {
            if let previous = lastClock, event.clock < previous {
                throw ScenarioError.clockWentBackwards(
                    sequence: event.sequence, previous: previous, current: event.clock
                )
            }
            lastClock = event.clock
            maxObservedPeriod = max(maxObservedPeriod, periodIndex(for: event.clock))

            // 处理任何事件前先做超时扫描：回收所有租户中已超时且仍处于预占态的记录。
            ledger.append(contentsOf: sweepTimeout(trigger: event))

            ledger.append(handle(event))
        }
        return ledger
    }

    // MARK: - 校验

    private func validate(_ events: [QuotaEvent]) throws {
        var seenSequences = Set<Int>()
        for event in events {
            if event.tenant.rawValue.isEmpty {
                throw ScenarioError.emptyTenantID(sequence: event.sequence)
            }
            if event.requestID.rawValue.isEmpty {
                throw ScenarioError.emptyRequestID(sequence: event.sequence)
            }
            if event.clock < 0 {
                throw ScenarioError.negativeClock(sequence: event.sequence)
            }
            if quotas[event.tenant] == nil {
                throw ScenarioError.unknownTenant(event.tenant)
            }
            if !seenSequences.insert(event.sequence).inserted {
                throw ScenarioError.duplicateSequence(event.sequence)
            }
        }
    }

    // MARK: - 超时回收

    private func sweepTimeout(trigger: QuotaEvent) -> [LedgerEntry] {
        var entries: [LedgerEntry] = []
        for tenant in reservations.keys.sorted(by: { $0.rawValue < $1.rawValue }) {
            let requestIDs = reservations[tenant, default: [:]].keys
                .sorted(by: { $0.rawValue < $1.rawValue })
            for requestID in requestIDs {
                guard var reservation = reservations[tenant]?[requestID],
                      reservation.state == .reserved,
                      trigger.clock - reservation.reservedAt >= config.timeout
                else { continue }

                var account = account(tenant: tenant, period: reservation.period)
                account.held -= 1
                precondition(account.held >= 0, "held quota went negative during reclaim")
                save(account: account, tenant: tenant, period: reservation.period)

                reservation.state = .reclaimed
                reservations[tenant]?[requestID] = reservation

                entries.append(
                    LedgerEntry(
                        sequence: trigger.sequence,
                        clock: trigger.clock,
                        tenant: tenant,
                        requestID: requestID,
                        action: .timeoutReclaimed,
                        accepted: true,
                        rejectReason: nil,
                        attributedPeriod: reservation.period,
                        snapshots: makeSnapshots(),
                        note: "reservation timed out after \(config.timeout) clock units; "
                            + "1 unit returned to period \(reservation.period)"
                    )
                )
            }
        }
        return entries
    }

    // MARK: - 事件处理

    private func handle(_ event: QuotaEvent) -> LedgerEntry {
        switch event.kind {
        case .reserve: return handleReserve(event)
        case .confirm: return handleConfirm(event)
        case .release: return handleRelease(event)
        }
    }

    private func handleReserve(_ event: QuotaEvent) -> LedgerEntry {
        let period = periodIndex(for: event.clock)
        if let existing = reservations[event.tenant]?[event.requestID] {
            return reject(
                event,
                action: .reserveRejected,
                reason: .duplicateReservation,
                period: existing.period,
                note: "request \(event.requestID) already has a reservation in period \(existing.period)"
            )
        }

        var account = account(tenant: event.tenant, period: period)
        if account.held >= account.quota {
            return reject(
                event,
                action: .reserveRejected,
                reason: .quotaExhausted,
                period: period,
                note: "tenant \(event.tenant) period \(period) exhausted: "
                    + "\(account.held)/\(account.quota) slots held"
            )
        }

        account.held += 1
        save(account: account, tenant: event.tenant, period: period)
        reservations[event.tenant]?[event.requestID] = Reservation(
            state: .reserved, period: period, reservedAt: event.clock
        )

        return accept(
            event,
            action: .reserveAccepted,
            period: period,
            note: "1 slot reserved in tenant \(event.tenant) period \(period); "
                + "remaining \(account.quota - account.held)/\(account.quota)"
        )
    }

    private func handleConfirm(_ event: QuotaEvent) -> LedgerEntry {
        guard var reservation = reservations[event.tenant]?[event.requestID] else {
            return reject(
                event,
                action: .confirmRejected,
                reason: .unknownRequest,
                period: nil,
                note: "no reservation found for request \(event.requestID)"
            )
        }

        switch reservation.state {
        case .reserved:
            reservation.state = .confirmed
            reservations[event.tenant]?[event.requestID] = reservation
            return accept(
                event,
                action: .confirmAccepted,
                period: reservation.period,
                note: "reservation confirmed; slot remains charged to period \(reservation.period)"
            )
        case .confirmed:
            return reject(
                event,
                action: .confirmRejected,
                reason: .duplicateConfirm,
                period: reservation.period,
                note: "request \(event.requestID) already confirmed"
            )
        case .released:
            return reject(
                event,
                action: .confirmRejected,
                reason: .lateConfirmAfterRelease,
                period: reservation.period,
                note: "request \(event.requestID) was already released; late confirm changes nothing"
            )
        case .reclaimed:
            // 回收后的迟到确认不会消耗新周期额度：拒绝且不触碰任何周期账户。
            return reject(
                event,
                action: .confirmRejected,
                reason: .lateConfirmAfterReclaim,
                period: reservation.period,
                note: "request \(event.requestID) was reclaimed at period \(reservation.period); "
                    + "late confirm consumes no new-period budget"
            )
        }
    }

    private func handleRelease(_ event: QuotaEvent) -> LedgerEntry {
        guard var reservation = reservations[event.tenant]?[event.requestID] else {
            return reject(
                event,
                action: .releaseRejected,
                reason: .unknownRequest,
                period: nil,
                note: "no reservation found for request \(event.requestID)"
            )
        }

        switch reservation.state {
        case .reserved, .confirmed:
            var account = account(tenant: event.tenant, period: reservation.period)
            account.held -= 1
            precondition(account.held >= 0, "held quota went negative during release")
            save(account: account, tenant: event.tenant, period: reservation.period)

            reservation.state = .released
            reservations[event.tenant]?[event.requestID] = reservation
            return accept(
                event,
                action: .releaseAccepted,
                period: reservation.period,
                note: "1 slot returned to tenant \(event.tenant) period \(reservation.period)"
            )
        case .released:
            // 重复失败回调只归还一次。
            return reject(
                event,
                action: .releaseRejected,
                reason: .duplicateRelease,
                period: reservation.period,
                note: "request \(event.requestID) already released; no quota returned again"
            )
        case .reclaimed:
            return reject(
                event,
                action: .releaseRejected,
                reason: .lateReleaseAfterReclaim,
                period: reservation.period,
                note: "request \(event.requestID) was already reclaimed; late release changes nothing"
            )
        }
    }

    // MARK: - 账本辅助

    private func accept(
        _ event: QuotaEvent,
        action: LedgerAction,
        period: Int?,
        note: String
    ) -> LedgerEntry {
        LedgerEntry(
            sequence: event.sequence,
            clock: event.clock,
            tenant: event.tenant,
            requestID: event.requestID,
            action: action,
            accepted: true,
            rejectReason: nil,
            attributedPeriod: period,
            snapshots: makeSnapshots(),
            note: note
        )
    }

    private func reject(
        _ event: QuotaEvent,
        action: LedgerAction,
        reason: RejectReason,
        period: Int?,
        note: String
    ) -> LedgerEntry {
        LedgerEntry(
            sequence: event.sequence,
            clock: event.clock,
            tenant: event.tenant,
            requestID: event.requestID,
            action: action,
            accepted: false,
            rejectReason: reason,
            attributedPeriod: period,
            snapshots: makeSnapshots(),
            note: note
        )
    }

    private func periodIndex(for clock: Int64) -> Int {
        Int(clock / config.periodLength)
    }

    private func account(tenant: TenantID, period: Int) -> PeriodAccount {
        let existing = accounts[tenant, default: [:]][period]
        return PeriodAccount(quota: quotas[tenant, default: 0], held: existing?.held ?? 0)
    }

    private func save(account: PeriodAccount, tenant: TenantID, period: Int) {
        accounts[tenant, default: [:]][period] = account
    }

    private func makeSnapshots() -> [TenantSnapshot] {
        quotas.keys
            .sorted(by: { $0.rawValue < $1.rawValue })
            .map { tenant in
                // 对每个租户输出 0...最大已观察周期 的完整区间，
                // 尚未触达的周期以 held=0 显式列出，便于跨租户余额对比。
                let periods: [Int]
                if maxObservedPeriod >= 0 {
                    periods = Array(0...maxObservedPeriod)
                } else {
                    periods = []
                }
                let balances: [PeriodBalance] = periods.map { period in
                    let account = accounts[tenant, default: [:]][period]
                    let held = account?.held ?? 0
                    let quota = quotas[tenant, default: 0]
                    return PeriodBalance(
                        period: period,
                        quota: quota,
                        held: held,
                        remaining: quota - held
                    )
                }
                return TenantSnapshot(tenant: tenant, balances: balances)
            }
    }
}
