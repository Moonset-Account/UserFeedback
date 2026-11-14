import Foundation
import AuthzValidatorCore

/// CLI 输出结构。
struct CLIOutput: Codable {
    let status: String
    let totalEvents: Int
    let authorizationCount: Int
    let deliveryCount: Int
    let findings: [ValidationFinding]
    let firstViolation: ValidationFinding?
}

enum CLI {
    static func run(arguments: [String]) -> Int32 {
        let paths = arguments.filter { !$0.hasPrefix("-") }
        let wantsPretty = arguments.contains("--pretty")

        guard !paths.isEmpty, paths.count <= 2 else {
            FileHandle.standardError.write(Data(
                """
                离线网关出站授权验证器（仅核验记录，不发出网络请求）

                用法:
                  AuthzValidator <events.json>                 单个 JSON 文件（事件数组或 events/authorizations+deliveries 包装）
                  AuthzValidator <authorizations.json> <deliveries.json>
                  AuthzValidator --pretty <events.json>        缩进输出 JSON

                退出码: 0 = 无违反; 1 = 发现授权约束问题; 2 = 用法或输入文件错误

                """.utf8))
            return 2
        }

        var events: [GatewayEvent] = []
        do {
            for path in paths {
                events.append(contentsOf: try EventFileParser.parseFile(at: path))
            }
        } catch let error as EventFileError {
            FileHandle.standardError.write(Data("输入错误: \(error.message)\n".utf8))
            return 2
        } catch {
            FileHandle.standardError.write(Data("输入错误: \(error.localizedDescription)\n".utf8))
            return 2
        }

        let report = Validator().validate(events)
        let output = CLIOutput(
            status: report.isCompliant ? "compliant" : "violation_found",
            totalEvents: report.totalEvents,
            authorizationCount: report.authorizationCount,
            deliveryCount: report.deliveryCount,
            findings: report.findings,
            firstViolation: report.firstViolation
        )

        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        encoder.outputFormatting = [.sortedKeys]
        if wantsPretty {
            encoder.outputFormatting.insert(.prettyPrinted)
        }
        do {
            let data = try encoder.encode(output)
            FileHandle.standardOutput.write(data)
            FileHandle.standardOutput.write(Data("\n".utf8))
        } catch {
            FileHandle.standardError.write(Data("输出编码失败: \(error.localizedDescription)\n".utf8))
            return 2
        }

        return report.isCompliant ? 0 : 1
    }
}

let exitCode = CLI.run(arguments: Array(CommandLine.arguments.dropFirst()))
exit(exitCode)
