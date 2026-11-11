// SPDX-License-Identifier: MIT
// cert-attrib 命令行入口。
//
// 用法:
//   cert-attrib --data <dataset.json> [--json-out <file>] [--text] [--leaf <id>]
//
// 默认同时输出人类可读文本报告；--json-out 保存结构化 JSON 报告；
// --leaf 仅聚焦指定叶证书的两两渠道比对（默认覆盖数据集中全部叶证书）。
#include <cstring>
#include <fstream>
#include <iostream>
#include <string>

#include "certattrib/engine.hpp"
#include "certattrib/loader.hpp"
#include "certattrib/report.hpp"

using namespace certattrib;

namespace {

void usage() {
    std::cerr << R"(usage: cert-attrib --data <dataset.json>
                       [--json-out <report.json>] [--text] [--leaf <leaf-id>]
  --data       结构化输入记录（证书/锚/证据/候选路径/策略/客户端日志）
  --json-out   结构化归因报告输出路径（不指定则打印到 stdout 的 JSON 块）
  --text       同时在 stderr 打印人类可读文本视图
  --leaf       仅处理指定叶证书（可重复传入）
  --help       显示本帮助
)";
}

}  // namespace

int main(int argc, char** argv) {
    std::string dataPath, jsonOut;
    std::vector<std::string> leafFilter;
    bool wantText = false;

    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        auto need = [&](const std::string& flag) {
            if (i + 1 >= argc)
                throw std::runtime_error(flag + " requires an argument");
            return std::string(argv[++i]);
        };
        if (arg == "--data") dataPath = need("--data");
        else if (arg == "--json-out") jsonOut = need("--json-out");
        else if (arg == "--leaf") leafFilter.push_back(need("--leaf"));
        else if (arg == "--text") wantText = true;
        else if (arg == "--help" || arg == "-h") { usage(); return 0; }
        else throw std::runtime_error("unknown argument: " + arg);
    }
    if (dataPath.empty()) {
        usage();
        return 2;
    }

    try {
        Dataset ds = loadDatasetFile(dataPath);
        Engine engine(ds);

        std::vector<std::string> leaves = leafFilter.empty()
                                              ? engine.allLeaves()
                                              : leafFilter;

        auto root = jObj();
        root->obj["tool"] =
            jStr("cert-attrib (offline cert divergence attribution)");
        root->obj["dataset"] = jStr(dataPath);
        root->obj["checkpoint_order"] = [&] {
            auto a = jArr();
            for (const char* n : {cp::PATH_CONSTRUCTION, cp::SIGNATURE_CHAIN,
                                  cp::ANCHOR_SELECTION, cp::VALIDITY_WINDOW,
                                  cp::PURPOSE_CONSTRAINT, cp::FINAL_DECISION})
                a->arr.push_back(jStr(n));
            return a;
        }();

        auto verdicts = jArr();
        for (const auto& leaf : leaves) {
            // 叶证书可能只出现在客户端日志而无任何候选路径：引擎判 no_candidate_path。
            for (const auto& kv : ds.policies)
                verdicts->arr.push_back(
                    channelVerdictToJson(engine.evaluateChannel(kv.second, leaf)));
        }
        root->obj["channel_verdicts"] = verdicts;

        std::vector<std::string> channels;
        for (const auto& kv : ds.policies) channels.push_back(kv.first);
        auto divs = jArr();
        for (const auto& leaf : leaves)
            for (size_t i = 0; i < channels.size(); ++i)
                for (size_t j = i + 1; j < channels.size(); ++j)
                    divs->arr.push_back(
                        divergenceToJson(engine.compare(leaf, channels[i], channels[j])));
        root->obj["divergence_reports"] = divs;

        std::string json = dumpJson(root, true);
        if (jsonOut.empty()) {
            std::cout << json << "\n";
        } else {
            std::ofstream out(jsonOut);
            if (!out) throw std::runtime_error("cannot write: " + jsonOut);
            out << json << "\n";
            std::cerr << "JSON report written to " << jsonOut << "\n";
        }
        if (wantText) std::cerr << renderTextReport(root);
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "cert-attrib: " << e.what() << "\n";
        return 1;
    }
}
