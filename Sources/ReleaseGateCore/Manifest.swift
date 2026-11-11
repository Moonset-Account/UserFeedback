import Foundation

/// 组件身份：同名插件的不同版本是不同的身份，声明、豁免与调用边均按完整身份匹配。
public struct ComponentID: Codable, Hashable, Comparable, Sendable {
    public var name: String
    public var version: String

    public init(name: String, version: String) {
        self.name = name
        self.version = version
    }

    public static func < (lhs: ComponentID, rhs: ComponentID) -> Bool {
        if lhs.name != rhs.name { return lhs.name < rhs.name }
        return lhs.version < rhs.version
    }
}

/// 构建清单中的一个组件（主程序或独立构建的插件）。
public struct Component: Codable, Equatable, Sendable {
    public var id: ComponentID
    /// 构建目标，例如 //plugins/auth:AuthPlugin
    public var buildTarget: String
    /// 负责该组件的团队
    public var team: String
    /// 组件编译时的 ABI 版本
    public var abiVersion: Int

    public init(id: ComponentID, buildTarget: String, team: String, abiVersion: Int) {
        self.id = id
        self.buildTarget = buildTarget
        self.team = team
        self.abiVersion = abiVersion
    }
}

/// 一条间接调用边：source 通过 interface 对 target 发起跨组件间接调用。
public struct IndirectCallEdge: Codable, Equatable, Sendable {
    public var source: ComponentID
    public var target: ComponentID
    /// 被调用的接口/符号名
    public var interface: String

    public init(source: ComponentID, target: ComponentID, interface: String) {
        self.source = source
        self.target = target
        self.interface = interface
    }
}

/// 控制流完整性保护覆盖声明：组件声明其受保护的接口集合。
public struct CoverageClaim: Codable, Equatable, Sendable {
    public var component: ComponentID
    public var coveredInterfaces: [String]

    public init(component: ComponentID, coveredInterfaces: [String]) {
        self.component = component
        self.coveredInterfaces = coveredInterfaces
    }
}

/// 带期限的豁免：只对指定组件身份 + 指定接口生效，过期即失效。
public struct Exemption: Codable, Equatable, Sendable {
    public var id: String
    public var component: ComponentID
    public var interface: String
    public var expiresAt: Date

    public init(id: String, component: ComponentID, interface: String, expiresAt: Date) {
        self.id = id
        self.component = component
        self.interface = interface
        self.expiresAt = expiresAt
    }

    public func isExpired(now: Date) -> Bool {
        expiresAt <= now
    }

    public func matches(target: ComponentID, interface: String) -> Bool {
        component == target && self.interface == interface
    }
}

/// 发布门禁的输入清单。
public struct BuildManifest: Codable, Equatable, Sendable {
    public var components: [Component]
    public var edges: [IndirectCallEdge]
    public var coverageClaims: [CoverageClaim]
    public var exemptions: [Exemption]

    public init(
        components: [Component],
        edges: [IndirectCallEdge],
        coverageClaims: [CoverageClaim],
        exemptions: [Exemption]
    ) {
        self.components = components
        self.edges = edges
        self.coverageClaims = coverageClaims
        self.exemptions = exemptions
    }
}

public enum ManifestLoader {
    /// 支持带/不带毫秒的 ISO-8601，以及仅日期 (yyyy-MM-dd，按 UTC 解释)。
    public static func makeDecoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let raw = try container.decode(String.self)

            let iso = ISO8601DateFormatter()
            if let date = iso.date(from: raw) { return date }
            iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            if let date = iso.date(from: raw) { return date }

            let dayOnly = DateFormatter()
            dayOnly.locale = Locale(identifier: "en_US_POSIX")
            dayOnly.timeZone = TimeZone(identifier: "UTC")
            dayOnly.dateFormat = "yyyy-MM-dd"
            if let date = dayOnly.date(from: raw) { return date }

            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "无法解析日期 '\(raw)'，支持 ISO-8601 或 yyyy-MM-dd"
            )
        }
        return decoder
    }

    public static func makeEncoder(pretty: Bool) -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.sortedKeys]
        if pretty { encoder.outputFormatting.insert(.prettyPrinted) }
        return encoder
    }

    public static func load(from path: String) throws -> BuildManifest {
        let url = URL(fileURLWithPath: path)
        let data = try Data(contentsOf: url)
        return try makeDecoder().decode(BuildManifest.self, from: data)
    }
}
