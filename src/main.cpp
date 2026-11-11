#include <exception>
#include <fstream>
#include <iostream>
#include <string>

#include "compare.hpp"
#include "engine.hpp"
#include "loader.hpp"
#include "report.hpp"

using namespace memtriage;

namespace {

struct CliArgs {
    std::string inputDir;
    std::string baselineDir;
    std::string outPath;
    bool json = false;
    bool help = false;
};

void printUsage(std::ostream& out, const char* prog) {
    out << "用法: " << prog << " --input <数据目录> [选项]\n"
        << "\n"
        << "必需参数:\n"
        << "  --input <目录>       包含三个输入文件的目录:\n"
        << "                         defect_impacts.json / release_deployments.json /\n"
        << "                         mitigation_verifications.json\n"
        << "\n"
        << "可选参数:\n"
        << "  --baseline <目录>    基线数据集目录，启用两期对比并解释修复顺序变化\n"
        << "  --json               输出机器可读 JSON（默认输出中文文本报告）\n"
        << "  --out <文件>         输出到文件（默认输出到标准输出）\n"
        << "  -h, --help           显示帮助\n"
        << "\n"
        << "退出码: 0 成功；1 参数错误；2 数据或评估失败。\n";
}

CliArgs parseArgs(int argc, char** argv) {
    CliArgs args;
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        auto needValue = [&](const std::string& flag) -> std::string {
            if (i + 1 >= argc) {
                throw std::runtime_error("参数 " + flag + " 需要一个值");
            }
            return argv[++i];
        };
        if (arg == "--input") {
            args.inputDir = needValue(arg);
        } else if (arg == "--baseline") {
            args.baselineDir = needValue(arg);
        } else if (arg == "--out") {
            args.outPath = needValue(arg);
        } else if (arg == "--json") {
            args.json = true;
        } else if (arg == "-h" || arg == "--help") {
            args.help = true;
        } else {
            throw std::runtime_error("未知参数: " + arg);
        }
    }
    if (!args.help && args.inputDir.empty()) {
        throw std::runtime_error("缺少必需参数 --input <数据目录>");
    }
    return args;
}

// 取路径最后一段作为数据集标签。
std::string labelOf(const std::string& path) {
    std::string p = path;
    while (!p.empty() && p.back() == '/') p.pop_back();
    auto pos = p.find_last_of('/');
    return pos == std::string::npos ? p : p.substr(pos + 1);
}

}  // namespace

int main(int argc, char** argv) {
    CliArgs args;
    try {
        args = parseArgs(argc, argv);
    } catch (const std::exception& e) {
        std::cerr << "参数错误: " << e.what() << "\n\n";
        printUsage(std::cerr, argv[0]);
        return 1;
    }
    if (args.help) {
        printUsage(std::cout, argv[0]);
        return 0;
    }

    try {
        Dataset current = loadDataset(args.inputDir);
        std::vector<Assessment> currentResults = runTriage(current);

        ComparisonReport comparison;
        bool hasBaseline = !args.baselineDir.empty();
        if (hasBaseline) {
            Dataset baseline = loadDataset(args.baselineDir);
            std::vector<Assessment> baselineResults = runTriage(baseline);
            comparison = compareAssessments(baselineResults, currentResults);
        }

        ReportOptions opts;
        opts.inputLabel = labelOf(args.inputDir);
        opts.baselineLabel = hasBaseline ? labelOf(args.baselineDir) : "";
        opts.includeComparison = hasBaseline;

        std::string output = args.json
            ? renderJsonReport(currentResults, opts, hasBaseline ? &comparison : nullptr)
            : renderTextReport(currentResults, opts, hasBaseline ? &comparison : nullptr);

        if (args.outPath.empty()) {
            std::cout << output;
        } else {
            std::ofstream out(args.outPath, std::ios::binary);
            if (!out) throw std::runtime_error("无法写入输出文件: " + args.outPath);
            out << output;
        }
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "分诊失败: " << e.what() << '\n';
        return 2;
    }
}
