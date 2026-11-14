//
//  ScenarioLoader.swift
//  quota-bench
//
//  本地 JSON 输入适配器。仅使用 Foundation，不访问任何网络或外部系统。
//

import Foundation
import QuotaIsolationCore

struct ScenarioFile: Decodable {
    let periodLength: Int64
    let timeout: Int64
    let tenants: [TenantSpec]
    let events: [EventSpec]
}

struct TenantSpec: Decodable {
    let tenantID: String
    let quota: Int
}

struct EventSpec: Decodable {
    let tenantID: String
    let sequence: Int
    let requestID: String
    let kind: String
    let clock: Int64
}

enum ScenarioLoader {
    enum LoadError: Error, CustomStringConvertible {
        case fileNotFound(String)
        case invalidKind(String, sequence: Int)
        case emptyTenants

        var description: String {
            switch self {
            case let .fileNotFound(path): return "input file not found: \(path)"
            case let .invalidKind(kind, sequence):
                return "unknown event kind '\(kind)' at sequence \(sequence) (expected reserve|confirm|release)"
            case .emptyTenants: return "scenario declares no tenants"
            }
        }
    }

    static func load(contentsOf path: String) throws -> (
        quotas: [TenantID: Int], config: QuotaConfig, events: [QuotaEvent]
    ) {
        guard FileManager.default.fileExists(atPath: path) else {
            throw LoadError.fileNotFound(path)
        }
        let data = try Data(contentsOf: URL(fileURLWithPath: path))
        let file = try JSONDecoder().decode(ScenarioFile.self, from: data)
        guard !file.tenants.isEmpty else { throw LoadError.emptyTenants }

        var quotas: [TenantID: Int] = [:]
        for spec in file.tenants {
            quotas[TenantID(spec.tenantID)] = spec.quota
        }

        let events: [QuotaEvent] = try file.events.map { spec in
            guard let kind = EventKind(rawValue: spec.kind) else {
                throw LoadError.invalidKind(spec.kind, sequence: spec.sequence)
            }
            return QuotaEvent(
                tenant: TenantID(spec.tenantID),
                sequence: spec.sequence,
                requestID: RequestID(spec.requestID),
                kind: kind,
                clock: spec.clock
            )
        }

        return (quotas, QuotaConfig(periodLength: file.periodLength, timeout: file.timeout), events)
    }
}
