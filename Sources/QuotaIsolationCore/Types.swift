//
//  Types.swift
//  QuotaIsolationCore
//
//  多租户固定周期配额隔离测试台的领域类型。纯 Swift 基础类型，不依赖 Foundation。
//

/// 已验证的租户标识。配额账本按租户严格隔离，租户之间不共享任何周期额度。
public struct TenantID: Hashable, Sendable, CustomStringConvertible {
    public let rawValue: String
    public init(_ rawValue: String) { self.rawValue = rawValue }
    public var description: String { rawValue }
}

/// 请求标识。重复的确认 / 归还 / 预占事件按「租户 + 请求 ID」识别。
public struct RequestID: Hashable, Sendable, CustomStringConvertible {
    public let rawValue: String
    public init(_ rawValue: String) { self.rawValue = rawValue }
    public var description: String { rawValue }
}

/// 输入事件的种类：预占、确认占用、归还配额。超时回收由引擎按虚拟时钟自动产生。
public enum EventKind: String, Sendable {
    case reserve
    case confirm
    case release
}

/// 一条输入记录：已验证租户 ID、声明的事件序号、请求 ID、虚拟时钟读数。
public struct QuotaEvent: Sendable {
    public let tenant: TenantID
    public let sequence: Int
    public let requestID: RequestID
    public let kind: EventKind
    public let clock: Int64

    public init(
        tenant: TenantID,
        sequence: Int,
        requestID: RequestID,
        kind: EventKind,
        clock: Int64
    ) {
        self.tenant = tenant
        self.sequence = sequence
        self.requestID = requestID
        self.kind = kind
        self.clock = clock
    }
}

/// 预占记录的生命周期状态。
public enum ReservationState: String, Sendable {
    /// 已预占，持有 1 个周期额度
    case reserved = "RESERVED"
    /// 已确认占用，仍持有原周期的 1 个额度
    case confirmed = "CONFIRMED"
    /// 已归还，额度已退回原周期
    case released = "RELEASED"
    /// 超时后被引擎回收，额度已退回原周期
    case reclaimed = "RECLAIMED"
}

/// 拒绝理由枚举。拒绝事件永不改动任何预算余额。
public enum RejectReason: String, Equatable, Sendable {
    /// 该租户当前周期额度已被占满
    case quotaExhausted = "QUOTA_EXHAUSTED"
    /// 同一租户 + 请求 ID 已存在预占记录
    case duplicateReservation = "DUPLICATE_RESERVATION"
    /// 确认 / 归还了一条引擎从未见过的请求
    case unknownRequest = "UNKNOWN_REQUEST"
    /// 对已确认的请求再次确认
    case duplicateConfirm = "DUPLICATE_CONFIRM"
    /// 对已归还的请求再次归还（重复失败回调只归还一次）
    case duplicateRelease = "DUPLICATE_RELEASE"
    /// 预占已归还后才到达的确认
    case lateConfirmAfterRelease = "LATE_CONFIRM_AFTER_RELEASE"
    /// 预占已超时回收后才到达的确认（不得消耗新周期额度）
    case lateConfirmAfterReclaim = "LATE_CONFIRM_AFTER_RECLAIM"
    /// 预占已超时回收后才到达的归还
    case lateReleaseAfterReclaim = "LATE_RELEASE_AFTER_RECLAIM"
}

/// 账本动作。
public enum LedgerAction: String, Sendable {
    case reserveAccepted = "RESERVE_ACCEPTED"
    case reserveRejected = "RESERVE_REJECTED"
    case confirmAccepted = "CONFIRM_ACCEPTED"
    case confirmRejected = "CONFIRM_REJECTED"
    case releaseAccepted = "RELEASE_ACCEPTED"
    case releaseRejected = "RELEASE_REJECTED"
    /// 引擎在处理某个事件前做超时扫描时产生的回收动作
    case timeoutReclaimed = "TIMEOUT_RECLAIMED"
}

/// 单个租户单个固定周期的预算余额快照。
public struct PeriodBalance: Equatable, Sendable {
    public let period: Int
    public let quota: Int
    public let held: Int
    public let remaining: Int

    public init(period: Int, quota: Int, held: Int, remaining: Int) {
        self.period = period
        self.quota = quota
        self.held = held
        self.remaining = remaining
    }
}

/// 某一步之后某个租户全部周期的余额。
public struct TenantSnapshot: Equatable, Sendable {
    public let tenant: TenantID
    public let balances: [PeriodBalance]

    public init(tenant: TenantID, balances: [PeriodBalance]) {
        self.tenant = tenant
        self.balances = balances
    }
}

/// 账本中的一步：每个输入事件（以及事件触发的超时回收）对应一条或多条记录。
public struct LedgerEntry: Equatable, Sendable {
    /// 触发该步的输入事件序号；超时回收复用触发扫描的事件序号
    public let sequence: Int
    public let clock: Int64
    public let tenant: TenantID
    public let requestID: RequestID
    public let action: LedgerAction
    public let accepted: Bool
    public let rejectReason: RejectReason?
    /// 事件归属的周期：始终指向原预占记录所在周期（跨周期事件不变）
    public let attributedPeriod: Int?
    /// 该步处理之后所有租户的预算余额快照
    public let snapshots: [TenantSnapshot]
    public let note: String

    public init(
        sequence: Int,
        clock: Int64,
        tenant: TenantID,
        requestID: RequestID,
        action: LedgerAction,
        accepted: Bool,
        rejectReason: RejectReason?,
        attributedPeriod: Int?,
        snapshots: [TenantSnapshot],
        note: String
    ) {
        self.sequence = sequence
        self.clock = clock
        self.tenant = tenant
        self.requestID = requestID
        self.action = action
        self.accepted = accepted
        self.rejectReason = rejectReason
        self.attributedPeriod = attributedPeriod
        self.snapshots = snapshots
        self.note = note
    }
}

/// 固定周期策略配置。虚拟时钟单位由调用方约定，周期长度与超时长度使用同一单位。
public struct QuotaConfig: Sendable, Equatable {
    public let periodLength: Int64
    public let timeout: Int64

    public init(periodLength: Int64, timeout: Int64) {
        self.periodLength = periodLength
        self.timeout = timeout
    }
}

/// 输入 / 配置层面的结构性错误。这类错误在处理任何事件前抛出，不产生账本。
public enum ScenarioError: Error, Equatable {
    case emptyTenantID(sequence: Int)
    case emptyRequestID(sequence: Int)
    case nonPositivePeriodLength
    case negativeTimeout
    case nonPositiveQuota(tenant: TenantID)
    case negativeClock(sequence: Int)
    case duplicateSequence(Int)
    case clockWentBackwards(sequence: Int, previous: Int64, current: Int64)
    case unknownTenant(TenantID)
}

extension ScenarioError: CustomStringConvertible {
    public var description: String {
        switch self {
        case let .emptyTenantID(sequence):
            return "empty tenant id at sequence \(sequence)"
        case let .emptyRequestID(sequence):
            return "empty request id at sequence \(sequence)"
        case .nonPositivePeriodLength:
            return "period length must be positive"
        case .negativeTimeout:
            return "reservation timeout must not be negative"
        case let .nonPositiveQuota(tenant):
            return "quota for tenant \(tenant) must be positive"
        case let .negativeClock(sequence):
            return "virtual clock must be non-negative at sequence \(sequence)"
        case let .duplicateSequence(sequence):
            return "duplicate event sequence \(sequence); sequence numbers must be unique"
        case let .clockWentBackwards(sequence, previous, current):
            return "virtual clock went backwards at sequence \(sequence): \(current) < \(previous)"
        case let .unknownTenant(tenant):
            return "event references unknown tenant \(tenant)"
        }
    }
}
