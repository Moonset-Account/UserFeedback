import Foundation

/// ISO8601 时间戳解析：同时兼容带小数秒与不带小数秒的形式（如 `2026-11-14T08:00:00Z`
/// 与 `2026-11-14T08:00:00.123Z`）。纯离线解析，不访问网络。
public enum Timestamp {
    private static let fractionalFormatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    private static let plainFormatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()

    public static func parse(_ text: String) -> Date? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return fractionalFormatter.date(from: trimmed) ?? plainFormatter.date(from: trimmed)
    }
}
