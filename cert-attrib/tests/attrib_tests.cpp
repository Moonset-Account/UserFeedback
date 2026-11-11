// SPDX-License-Identifier: MIT
// 验收测试：覆盖四类必备场景及边界语义，断言引擎结论与客户端判定解释性。
#include <cstdio>
#include <functional>
#include <iostream>
#include <string>
#include <vector>

#include "certattrib/engine.hpp"
#include "certattrib/loader.hpp"
#include "certattrib/report.hpp"

using namespace certattrib;

namespace {

int g_failures = 0;
int g_checks = 0;

void check(bool cond, const std::string& msg) {
    ++g_checks;
    if (!cond) {
        ++g_failures;
        std::cerr << "  FAIL: " << msg << "\n";
    }
}

void checkEq(const std::string& got, const std::string& want,
             const std::string& msg) {
    ++g_checks;
    if (got != want) {
        ++g_failures;
        std::cerr << "  FAIL: " << msg << " — got='" << got << "' want='"
                  << want << "'\n";
    }
}

const PathVerdict& findPath(const ChannelVerdict& cv,
                            const std::string& pathId) {
    for (const auto& p : cv.paths)
        if (p.pathId == pathId) return p;
    throw std::runtime_error("missing path verdict: " + pathId);
}

const CheckpointResult& findCp(const PathVerdict& pv,
                               const std::string& name) {
    for (const auto& c : pv.checkpoints)
        if (c.checkpoint == name) return c;
    throw std::runtime_error("missing checkpoint: " + name);
}

void testJsonParser() {
    std::cout << "[test] JSON 解析/转义/序列化往返\n";
    auto v = parseJson(R"({"a":"x\ny","b":[1,2.5,true,false,null],"c":{"k":"v"}})");
    checkEq(v->at("a")->str, "x\ny", "转义 \\n");
    check(v->at("b")->arr.size() == 5, "数组 5 元素");
    checkEq(v->at("c")->at("k")->str, "v", "嵌套对象");
    auto round = parseJson(dumpJson(v, false));
    checkEq(round->at("a")->str, "x\ny", "序列化往返保持字符串");
    bool threw = false;
    try { parseJson("{bad"); } catch (const JsonError&) { threw = true; }
    check(threw, "非法 JSON 抛 JsonError");
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2) {
        std::cerr << "usage: attrib_tests <acceptance.json>\n";
        return 2;
    }
    testJsonParser();

    Dataset ds = loadDatasetFile(argv[1]);
    Engine eng(ds);

    auto verdict = [&](const std::string& ch, const std::string& leaf) {
        return eng.evaluateChannel(ds.policies.at(ch), leaf);
    };

    // ---- 场景 1：交叉签发，旧路径失效、另一条仍有效 ----
    std::cout << "[test] 场景1 交叉签发：旧锚路径失效，新锚路径有效\n";
    {
        auto legacy = verdict("ch-legacy", "leaf-1");
        auto modern = verdict("ch-modern", "leaf-1");
        checkEq(legacy.finalVerdict, "trusted", "leaf-1 legacy 终审信任");
        checkEq(modern.finalVerdict, "trusted", "leaf-1 modern 终审信任");
        checkEq(findPath(legacy, "p1-old").reasonCode, reason::OK,
                "legacy 下旧路径有效");
        checkEq(findPath(legacy, "p1-new").reasonCode, reason::ANCHOR_VERSION_ABSENT,
                "旧快照没有 G2 锚版本");
        checkEq(findPath(modern, "p1-old").reasonCode, reason::ANCHOR_VERSION_ABSENT,
                "新快照移除 G1 锚，旧路径失效");
        checkEq(findPath(modern, "p1-new").reasonCode, reason::OK,
                "新快照下新路径仍有效");
        checkEq(modern.acceptedPathId, "p1-new", "终审接受新路径");
        // 证据引用
        bool hasEv = false;
        for (const auto& ref : findPath(modern, "p1-new").evidenceUsed)
            if (ref == "leaf-1@kinx" || ref == "inter-x-v2@kr2") hasEv = true;
        check(hasEv, "新路径实际引用了签名证据");

        auto rep = eng.compare("leaf-1", "ch-legacy", "ch-modern");
        check(!rep.divergent, "终审一致，不构成终审分歧");
        checkEq(rep.earliestCheckpoint, cp::ANCHOR_SELECTION,
                "最早分歧检查点为锚选择");
        checkEq(rep.attribution, divergence::TRUST_ANCHOR_CHANGE,
                "路径级差异归因为信任锚变化");
        check(rep.bothClientDecisionsExplained, "两端客户端判定均可解释");
    }

    // ---- 场景 2：证书到期边界 + 端点语义 ----
    std::cout << "[test] 场景2 到期边界：t == not_after，inclusive/exclusive 差异\n";
    {
        auto legacy = verdict("ch-legacy", "leaf-2");
        auto modern = verdict("ch-modern", "leaf-2");
        auto edge = verdict("ch-edge-exclusive", "leaf-2");
        auto lenient = verdict("ch-lenient", "leaf-2");
        checkEq(legacy.finalVerdict, "trusted", "t<not_after 且含端点：有效");
        checkEq(modern.finalVerdict, "rejected", "t>not_after：过期");
        checkEq(findPath(modern, "p2").reasonCode, reason::EXPIRED,
                "拒绝原因 expired");
        checkEq(edge.finalVerdict, "rejected", "t==not_after 且端点排除：过期");
        checkEq(findPath(edge, "p2").reasonCode, reason::EXPIRED,
                "端点排除时边界时刻拒绝");
        checkEq(lenient.finalVerdict, "trusted",
                "同一时刻端点纳入：仍然有效");

        auto r1 = eng.compare("leaf-2", "ch-legacy", "ch-modern");
        check(r1.divergent, "legacy/modern 终审判定分歧");
        checkEq(r1.earliestCheckpoint, cp::VALIDITY_WINDOW,
                "最早分歧在时间窗检查点");
        checkEq(r1.attribution, divergence::TIME_WINDOW, "归因为时间窗口");

        auto r2 = eng.compare("leaf-2", "ch-edge-exclusive", "ch-lenient");
        check(r2.divergent, "同一边界时刻，端点策略不同导致分歧");
        checkEq(r2.attribution, divergence::TIME_WINDOW,
                "端点语义差异归入时间窗口");
        bool deltaSeen = false;
        for (const auto& d : r2.policyDeltas)
            if (d.find("validity_endpoint_inclusive") != std::string::npos)
                deltaSeen = true;
        check(deltaSeen, "策略差异清单包含端点语义字段");
    }

    // ---- 场景 3：同名异钥锚选择差异 ----
    std::cout << "[test] 场景3 同名锚：同 subject 两把 key，策略偏好决定选择\n";
    {
        auto legacy = verdict("ch-legacy", "leaf-3");
        auto modern = verdict("ch-modern", "leaf-3");
        auto edge = verdict("ch-edge-exclusive", "leaf-3");
        auto lenient = verdict("ch-lenient", "leaf-3");
        checkEq(legacy.finalVerdict, "trusted", "旧快照只有 ks1 一个同名锚，无冲突");
        checkEq(modern.finalVerdict, "rejected", "新快照双锚且偏好 ks2，路径止于 ks1");
        checkEq(findPath(modern, "p3").reasonCode, reason::ANCHOR_NAME_COLLISION,
                "拒绝原因 anchor_name_collision");
        checkEq(edge.finalVerdict, "rejected", "无偏好策略下同名双钥无法裁决");
        checkEq(lenient.finalVerdict, "trusted", "偏好 ks1 与路径顶端一致，通过");

        auto rep = eng.compare("leaf-3", "ch-legacy", "ch-modern");
        check(rep.divergent, "同名锚选择导致终审分歧");
        checkEq(rep.earliestCheckpoint, cp::ANCHOR_SELECTION,
                "最早分歧在锚选择检查点");
        checkEq(rep.attribution, divergence::TRUST_ANCHOR_CHANGE,
                "归因为信任锚变化");
    }

    // ---- 场景 4：签名结论缺失（含同名异钥证据）----
    std::cout << "[test] 场景4 签名证据缺失：严格拒绝/宽松放行\n";
    {
        auto legacy = verdict("ch-legacy", "leaf-4");
        auto lenient = verdict("ch-lenient", "leaf-4");
        checkEq(legacy.finalVerdict, "rejected", "严格渠道拒绝证据缺失路径");
        checkEq(findPath(legacy, "p4").reasonCode, reason::SIG_KEY_MISMATCH,
                "存在异钥证据 → signature_key_mismatch");
        checkEq(findPath(legacy, "p4").firstFailureCheckpoint,
                cp::SIGNATURE_CHAIN, "首个失败检查点为签名链");
        checkEq(lenient.finalVerdict, "trusted",
                "宽松渠道 reject_on_missing_evidence=false 放行");
        const auto& sigCp = findCp(findPath(lenient, "p4"), cp::SIGNATURE_CHAIN);
        check(sigCp.passed && sigCp.detail.find("reject_on_missing_evidence=false")
                                  != std::string::npos,
              "放行同时保留证据缺失标注");

        auto rep = eng.compare("leaf-4", "ch-legacy", "ch-lenient");
        check(rep.divergent, "证据处理策略导致终审分歧");
        checkEq(rep.earliestCheckpoint, cp::SIGNATURE_CHAIN,
                "最早分歧在签名链检查点");
        checkEq(rep.attribution, divergence::INCOMPLETE_CANDIDATE,
                "归因为候选路径不完整（证据缺失）");
    }

    // ---- 场景 5：客户端判定无法由策略解释 ----
    std::cout << "[test] 场景5 引擎一致但客户端日志分歧 → unexplained\n";
    {
        auto modern = verdict("ch-modern", "leaf-5");
        checkEq(modern.finalVerdict, "trusted", "策略+证据推导出信任");
        check(!modern.clientDecisionExplained,
              "客户端本地黑名单拒绝无法由策略解释");
        check(modern.clientMismatchDetail.find("local_blacklist") !=
                  std::string::npos,
              "差异细节保留客户端自报原因");
        auto rep = eng.compare("leaf-5", "ch-legacy", "ch-modern");
        check(!rep.divergent, "引擎终审一致");
        checkEq(rep.attribution, divergence::UNEXPLAINED,
                "客户端分歧归因为 unexplained");
        check(!rep.bothClientDecisionsExplained, "并非两端客户端判定都可解释");
    }

    // ---- 场景 6：断裂候选路径不影响另一条完整路径 ----
    std::cout << "[test] 场景6 一条候选链断裂，另一条完整\n";
    {
        auto v = verdict("ch-legacy", "leaf-6");
        checkEq(v.finalVerdict, "trusted", "完整路径使叶证书仍被信任");
        const auto& bad = findPath(v, "p6-bad");
        checkEq(bad.reasonCode, reason::BROKEN_CHAIN, "断裂路径原因 broken_chain");
        checkEq(bad.firstFailureCheckpoint, cp::PATH_CONSTRUCTION,
                "断裂在路径构造检查点暴露");
        const auto& later = findCp(bad, cp::VALIDITY_WINDOW);
        checkEq(later.reasonCode, "skipped_after_failure",
                "失败后后续检查点标记跳过，序列仍可对齐");
        checkEq(findPath(v, "p6-good").reasonCode, reason::OK, "完整路径通过");
    }

    // ---- 场景 7：无候选路径（叶仅见于客户端日志）与 not_before 端点语义 ----
    std::cout << "[test] 场景7 无候选路径 + not_before 边界两侧语义\n";
    {
        Dataset mini;
        CertRecord root, leaf;
        root.id = "r"; root.subject = "R"; root.issuer = "R";
        root.keyId = "kr"; root.issuerKeyId = "kr";
        root.notBefore = 100; root.notAfter = 1000; root.isCa = true;
        leaf.id = "l"; leaf.subject = "L"; leaf.issuer = "R";
        leaf.keyId = "kl"; leaf.issuerKeyId = "kr";
        leaf.notBefore = 500; leaf.notAfter = 900; leaf.eku = {"serverAuth"};
        mini.certs = {{"r", root}, {"l", leaf}};
        AnchorRecord a;
        a.subject = "R"; a.keyId = "kr"; a.anchorId = "ar"; a.version = 1;
        mini.anchors.push_back(a);
        SigEvidence e;
        e.childCertId = "l"; e.issuerKeyId = "kr"; e.result = "valid";
        mini.evidence["l"] = {e};
        // 不添加任何 candidate_paths → 无候选路径。
        PolicyFields p;
        p.channelId = "c"; p.anchorVersion = 1; p.verificationTime = 600;
        mini.policies["c"] = p;
        Engine miniEng(mini);
        auto noPath = miniEng.evaluateChannel(p, "l");
        checkEq(noPath.finalVerdict, "rejected", "无候选路径 → 拒绝");
        checkEq(noPath.reasonCode, reason::NO_CANDIDATE_PATH,
                "原因码 no_candidate_path");

        // 补上路径后检查 not_before 边界：t == not_before。
        CandidatePath cp;
        cp.pathId = "pl"; cp.leafId = "l"; cp.chain = {"l", "r"};
        mini.paths.push_back(cp);
        Engine eng2(mini);
        PolicyFields incl = p, excl = p;
        incl.validityEndpointInclusive = true;
        excl.validityEndpointInclusive = false;
        incl.verificationTime = excl.verificationTime = 500;  // == not_before
        checkEq(eng2.evaluateChannel(incl, "l").finalVerdict, "trusted",
                "含端点时 t==not_before 有效");
        auto ex = eng2.evaluateChannel(excl, "l");
        checkEq(ex.finalVerdict, "rejected", "排端点时 t==not_before 拒绝");
        checkEq(ex.paths.front().reasonCode, reason::NOT_YET_VALID,
                "拒绝原因 not_yet_valid");
    }

    // ---- 报告可生成且包含全部叶证书 ----
    std::cout << "[test] 结构化报告生成\n";    {
        auto report = buildFullReport(eng, ds);
        check(report->at("channel_verdicts")->arr.size() == 24,
              "6 叶证书 × 4 渠道 = 24 条判定");
        auto round = parseJson(dumpJson(report, true));
        check(round->at("divergence_reports")->arr.size() == 36,
              "6 叶 × C(4,2)=6 对 = 36 份分歧报告");
    }

    std::cout << "\n" << g_checks << " checks, " << g_failures << " failures\n";
    return g_failures ? 1 : 0;
}
