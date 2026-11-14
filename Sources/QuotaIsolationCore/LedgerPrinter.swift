//
//  LedgerPrinter.swift
//  QuotaIsolationCore
//

public enum LedgerPrinter {
    /// 将账本渲染为人类可读的逐步文本：预算余额、事件归属、拒绝理由。
    public static func render(
        quotas: [TenantID: Int],
        config: QuotaConfig,
        ledger: [LedgerEntry]
    ) -> String {
        var out = ""
        let tenants = quotas.keys.sorted { $0.rawValue < $1.rawValue }
        out += "== Quota Isolation Bench ==\n"
        out += "period-length=\(config.periodLength) timeout=\(config.timeout)\n"
        for tenant in tenants {
            out += "tenant \(tenant) quota=\(quotas[tenant] ?? 0)\n"
        }
        out += "steps=\(ledger.count)\n\n"

        for entry in ledger {
            let reason = entry.rejectReason?.rawValue ?? "-"
            let period = entry.attributedPeriod.map(String.init) ?? "-"
            out += "[seq=\(entry.sequence) clock=\(entry.clock)] "
                + "tenant=\(entry.tenant) request=\(entry.requestID) "
                + "action=\(entry.action.rawValue) accepted=\(entry.accepted) "
                + "attributed-period=\(period) reason=\(reason)\n"
            for snapshot in entry.snapshots {
                let cells = snapshot.balances.map { balance -> String in
                    "p\(balance.period): held=\(balance.held)/\(balance.quota) remaining=\(balance.remaining)"
                }
                out += "    balance tenant=\(snapshot.tenant) "
                    + (cells.isEmpty ? "(no period touched)" : cells.joined(separator: " | "))
                    + "\n"
            }
            out += "    note: \(entry.note)\n\n"
        }
        return out
    }
}
