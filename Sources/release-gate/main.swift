import Foundation
import ReleaseGateCore

/// 用法:
///   release-gate <manifest.json> [--now yyyy-MM-dd] [--pretty] [--report-out path]
///
/// 退出码:
///   0  门禁通过（无违规）
///   1  门禁拒绝（报告中存在违规，报告同时打印到 stdout）
///   2  用法或清单解析错误
@main
struct ReleaseGateCommand {
    static func main() {
        let args = Array(CommandLine.arguments.dropFirst())

        guard let manifestPath = args.first(where: { !$0.hasPrefix("--") }) else {
            FileHandle.standardError.write(Data("错误: 缺少清单路径参数\n用法: release-gate <manifest.json> [--now yyyy-MM-dd] [--pretty] [--report-out path]\n".utf8))
            exit(2)
        }

        var now = Date()
        var pretty = false
        var reportOut: String?

        var index = 0
        while index < args.count {
            let arg = args[index]
            switch arg {
            case "--now":
                index += 1
                guard index < args.count, let parsed = parseDay(args[index]) else {
                    FileHandle.standardError.write(Data("错误: --now 需要 yyyy-MM-dd 参数\n".utf8))
                    exit(2)
                }
                now = parsed
            case "--pretty":
                pretty = true
            case "--report-out":
                index += 1
                guard index < args.count else {
                    FileHandle.standardError.write(Data("错误: --report-out 需要输出路径参数\n".utf8))
                    exit(2)
                }
                reportOut = args[index]
            default:
                break
            }
            index += 1
        }

        let manifest: BuildManifest
        do {
            manifest = try ManifestLoader.load(from: manifestPath)
        } catch {
            FileHandle.standardError.write(Data("错误: 无法加载清单 \(manifestPath): \(error)\n".utf8))
            exit(2)
        }

        let report = ReleaseGateEvaluator(now: now).evaluate(manifest)

        let encoder = ManifestLoader.makeEncoder(pretty: pretty)
        guard let reportData = try? encoder.encode(report) else {
            FileHandle.standardError.write(Data("错误: 无法编码门禁报告\n".utf8))
            exit(2)
        }

        if let reportOut {
            do {
                try reportData.write(to: URL(fileURLWithPath: reportOut))
            } catch {
                FileHandle.standardError.write(Data("错误: 无法写入报告 \(reportOut): \(error)\n".utf8))
                exit(2)
            }
        }

        // 人类可读摘要始终输出，机器可读 JSON 报告写 stdout（或同时写文件）。
        printTextSummary(report)
        FileHandle.standardOutput.write(reportData)
        FileHandle.standardOutput.write(Data("\n".utf8))

        exit(report.passed ? 0 : 1)
    }

    private static func parseDay(_ raw: String) -> Date? {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "UTC")
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.date(from: raw)
    }

    private static func printTextSummary(_ report: GateReport) {
        let s = report.summary
        var lines: [String] = []
        lines.append("===== 发布门禁 \(report.passed ? "通过 ✅" : "拒绝 ❌") =====")
        lines.append("组件: \(s.componentCount)  调用边: \(s.edgeCount)（跨组件 \(s.crossComponentEdgeCount)）  " +
                     "完整证据: \(s.evidencedCount)  豁免放行: \(s.exemptedCount)  违规: \(s.violationCount)")
        for violation in report.violations {
            lines.append("[\(violation.kind.rawValue)] \(violation.message)")
            if let target = violation.buildTarget, let interface = violation.interface, let team = violation.team {
                lines.append("    构建目标: \(target)  接口: \(interface)  负责团队: \(team)")
            }
            if let exemptionId = violation.exemptionId {
                lines.append("    豁免记录: \(exemptionId)")
            }
        }
        FileHandle.standardError.write(Data((lines.joined(separator: "\n") + "\n").utf8))
    }
}
