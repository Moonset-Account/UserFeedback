import Foundation

/// 离线 JSON 解析错误。
public enum EventFileError: Error, Equatable {
    case fileReadFailed(path: String, message: String)
    case invalidJSON(path: String, message: String)
    case unexpectedStructure(path: String)
    case decodeFailed(path: String, message: String)

    public var message: String {
        switch self {
        case let .fileReadFailed(path, message):
            return "无法读取文件 \(path): \(message)"
        case let .invalidJSON(path, message):
            return "文件 \(path) 不是合法 JSON: \(message)"
        case let .unexpectedStructure(path):
            return "文件 \(path) 结构无法识别：需要事件数组，或包含 events/authorizations/deliveries 数组的对象"
        case let .decodeFailed(path, message):
            return "文件 \(path) 事件字段不合法: \(message)"
        }
    }
}

/// 从本地 JSON 文件解析网关事件。支持三种结构：
/// 1. 顶层事件数组 `[ {event_type: "authorization"|"delivery", ...}, ... ]`
/// 2. `{ "events": [ ... ] }`
/// 3. `{ "authorizations": [ ... ], "deliveries": [ ... ] }`
/// 解析仅使用 Foundation，不发出任何网络请求。
public enum EventFileParser {
    public static func parseFile(at path: String) throws -> [GatewayEvent] {
        let data: Data
        do {
            data = try Data(contentsOf: URL(fileURLWithPath: path))
        } catch {
            throw EventFileError.fileReadFailed(path: path, message: error.localizedDescription)
        }
        return try parseData(data, sourcePath: path)
    }

    public static func parseData(_ data: Data, sourcePath: String = "<memory>") throws -> [GatewayEvent] {
        let jsonObject: Any
        do {
            jsonObject = try JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed])
        } catch {
            throw EventFileError.invalidJSON(path: sourcePath, message: error.localizedDescription)
        }

        if let array = jsonObject as? [[String: Any]] {
            return try decode(array, sourcePath: sourcePath)
        }

        if let wrapper = jsonObject as? [String: Any] {
            if let events = wrapper["events"] as? [[String: Any]] {
                return try decode(events, sourcePath: sourcePath)
            }
            let authorizations = wrapper["authorizations"] as? [[String: Any]] ?? []
            let deliveries = wrapper["deliveries"] as? [[String: Any]] ?? []
            if !authorizations.isEmpty || !deliveries.isEmpty {
                return try decode(authorizations + deliveries, sourcePath: sourcePath)
            }
        }

        throw EventFileError.unexpectedStructure(path: sourcePath)
    }

    private static func decode(_ array: [[String: Any]], sourcePath: String) throws -> [GatewayEvent] {
        let decoder = JSONDecoder()
        let wrapped: [String: [[String: Any]]] = ["events": array]
        do {
            let data = try JSONSerialization.data(withJSONObject: wrapped)
            return try decoder.decode(EventEnvelope.self, from: data).events
        } catch let error as DecodingError {
            throw EventFileError.decodeFailed(path: sourcePath, message: decodingErrorDescription(error))
        } catch {
            throw EventFileError.decodeFailed(path: sourcePath, message: error.localizedDescription)
        }
    }

    private struct EventEnvelope: Decodable {
        let events: [GatewayEvent]
    }

    private static func decodingErrorDescription(_ error: DecodingError) -> String {
        switch error {
        case let .typeMismatch(type, context):
            return "字段 \(context.codingPath.map(\.stringValue).joined(separator: ".")) 类型错误，期望 \(type): \(context.debugDescription)"
        case let .valueNotFound(type, context):
            return "字段 \(context.codingPath.map(\.stringValue).joined(separator: ".")) 缺失，期望 \(type)"
        case let .keyNotFound(key, context):
            return "缺少必需字段 \(key.stringValue)（路径 \(context.codingPath.map(\.stringValue).joined(separator: "."))）"
        case let .dataCorrupted(context):
            return "数据损坏: \(context.debugDescription)"
        @unknown default:
            return error.localizedDescription
        }
    }
}
