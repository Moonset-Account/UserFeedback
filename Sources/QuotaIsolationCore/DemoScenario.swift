//
//  DemoScenario.swift
//  QuotaIsolationCore
//
//  内置合成场景：两个合法租户 alpha / beta，各 2 个固定周期额度。
//  覆盖：并发预占上限、租户隔离、重复归还只退一次、跨周期超时回收、
//  回收后迟到确认不消耗新周期额度。
//

public struct DemoScenario {
    public static let tenantAlpha = TenantID("alpha")
    public static let tenantBeta = TenantID("beta")

    public static func make() -> (quotas: [TenantID: Int], config: QuotaConfig, events: [QuotaEvent]) {
        let quotas: [TenantID: Int] = [tenantAlpha: 2, tenantBeta: 2]
        // 周期长度 1000 个虚拟时钟单位；预占 300 个单位内未确认即超时
        let config = QuotaConfig(periodLength: 1000, timeout: 300)

        let t = TenantID("alpha")
        let b = TenantID("beta")
        func r(_ id: String) -> RequestID { RequestID(id) }

        let events: [QuotaEvent] = [
            // —— 周期 0（时钟 0..<1000）：同一租户并发预占不得超额 ——
            QuotaEvent(tenant: t, sequence: 1, requestID: r("A-1"), kind: .reserve, clock: 10),
            QuotaEvent(tenant: t, sequence: 2, requestID: r("A-2"), kind: .reserve, clock: 20),
            QuotaEvent(tenant: t, sequence: 3, requestID: r("A-3"), kind: .reserve, clock: 30), // 超额拒绝
            // beta 独立使用自己的额度，不受 alpha 用满影响
            QuotaEvent(tenant: b, sequence: 4, requestID: r("B-1"), kind: .reserve, clock: 40),
            QuotaEvent(tenant: b, sequence: 5, requestID: r("B-2"), kind: .reserve, clock: 50),
            // 确认与归还
            QuotaEvent(tenant: t, sequence: 6, requestID: r("A-1"), kind: .confirm, clock: 60),
            QuotaEvent(tenant: b, sequence: 7, requestID: r("B-1"), kind: .release, clock: 70),
            QuotaEvent(tenant: b, sequence: 8, requestID: r("B-1"), kind: .release, clock: 80), // 重复归还，拒绝
            QuotaEvent(tenant: t, sequence: 9, requestID: r("A-1"), kind: .release, clock: 90),
            QuotaEvent(tenant: t, sequence: 10, requestID: r("A-1"), kind: .confirm, clock: 100), // 归还后的迟到确认
            // —— 时钟跨入周期 1：扫描先回收周期 0 中超时的 A-2、B-2 ——
            QuotaEvent(tenant: t, sequence: 11, requestID: r("A-4"), kind: .reserve, clock: 1050), // 周期 1 的新额度
            // 跨周期到达的确认 / 归还仍关联原预占（周期 0），且记录已被回收
            QuotaEvent(tenant: t, sequence: 12, requestID: r("A-2"), kind: .confirm, clock: 1100), // 回收后的迟到确认
            QuotaEvent(tenant: t, sequence: 13, requestID: r("A-2"), kind: .release, clock: 1110), // 回收后的迟到归还
            // 新周期额度未被迟到确认消耗：A-5 可正常预占，用满周期 1
            QuotaEvent(tenant: t, sequence: 14, requestID: r("A-5"), kind: .reserve, clock: 1120),
            // 重复预占按 租户+请求ID 识别
            QuotaEvent(tenant: t, sequence: 15, requestID: r("A-5"), kind: .reserve, clock: 1130)
        ]
        return (quotas, config, events)
    }
}
