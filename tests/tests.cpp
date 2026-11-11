// memtriage 单元测试：无外部依赖的轻量断言框架。
// 覆盖：退役高频告警、关键进程低频异常、部分发行版缓解、未知部署、
// 排序同分稳定次序、缓解覆盖口径、置信度与核实清单、基线对比、JSON 解析、输入校验。
#include <algorithm>
#include <cmath>
#include <functional>
#include <iostream>
#include <map>
#include <string>
#include <vector>

#include "compare.hpp"
#include "engine.hpp"
#include "json.hpp"
#include "loader.hpp"
#include "model.hpp"
#include "report.hpp"

using namespace memtriage;

namespace {

int g_checks = 0;
int g_failures = 0;
std::vector<std::string> g_failureMessages;

void check(bool cond, const std::string& name) {
    ++g_checks;
    if (!cond) {
        ++g_failures;
        g_failureMessages.push_back("FAIL: " + name);
        std::cout << "  [FAIL] " << name << '\n';
    } else {
        std::cout << "  [ok]   " << name << '\n';
    }
}

bool nearly(double a, double b, double eps = 1e-9) {
    return std::fabs(a - b) <= eps;
}

const Assessment* find(const std::vector<Assessment>& list, const std::string& id) {
    for (const auto& a : list) {
        if (a.defect.defectId == id) return &a;
    }
    return nullptr;
}

const ReleaseAssessment* findRelease(const Assessment& a, const std::string& rid) {
    for (const auto& r : a.releases) {
        if (r.releaseId == rid) return &r;
    }
    return nullptr;
}

// ---- 内联合成数据 -----------------------------------------------------------

const char* kDefects = R"JSON(
{
  "defects": [
    {"defect_id": "MEM-1001", "title": "退役高频", "impact_level": "service_degradation",
     "alert_count": 4820, "evidence_confirmed": true, "evidence_source": "fuzzer",
     "affected_releases": ["REL-EOL-9", "REL-EOL-10"]},
    {"defect_id": "MEM-1002", "title": "关键进程中止", "impact_level": "critical_process_abort",
     "alert_count": 37, "evidence_confirmed": true, "evidence_source": "crloop",
     "affected_releases": ["REL-S2", "REL-S3", "REL-S4"]},
    {"defect_id": "MEM-1003", "title": "业务中断部分缓解", "impact_level": "business_interruption",
     "alert_count": 156, "evidence_confirmed": true, "evidence_source": "sentinel",
     "affected_releases": ["REL-S1", "REL-S2", "REL-S3"]},
    {"defect_id": "MEM-1004", "title": "未知部署+证据未确认", "impact_level": "service_degradation",
     "alert_count": 12, "evidence_confirmed": false, "evidence_source": "batch",
     "affected_releases": ["REL-S4", "REL-UNMAPPED"]},
    {"defect_id": "MEM-1005", "title": "缓解无效", "impact_level": "minor_degradation",
     "alert_count": 88, "evidence_confirmed": true, "evidence_source": "leak",
     "affected_releases": ["REL-S1", "REL-S3"]}
  ]
}
)JSON";

const char* kDeployments = R"JSON(
{
  "deployments": [
    {"release_id": "REL-S1", "lifecycle": "active", "deploy_count": 1200},
    {"release_id": "REL-S2", "lifecycle": "active", "deploy_count": 1500},
    {"release_id": "REL-S3", "lifecycle": "active", "deploy_count": 1500},
    {"release_id": "REL-S4", "lifecycle": "active", "deploy_count": null},
    {"release_id": "REL-EOL-9", "lifecycle": "retired", "deploy_count": 60},
    {"release_id": "REL-EOL-10", "lifecycle": "retired", "deploy_count": 20}
  ]
}
)JSON";

const char* kMitigations = R"JSON(
{
  "mitigations": [
    {"mitigation_id": "MIT-2001", "defect_id": "MEM-1002", "release_id": "REL-S2",
     "verification_status": "verified_effective", "evidence_note": "复测通过"},
    {"mitigation_id": "MIT-2002", "defect_id": "MEM-1002", "release_id": "REL-S3",
     "verification_status": "pending", "evidence_note": "待排期"},
    {"mitigation_id": "MIT-2003", "defect_id": "MEM-1003", "release_id": "REL-S1",
     "verification_status": "verified_effective", "evidence_note": "灰度有效"},
    {"mitigation_id": "MIT-2004", "defect_id": "MEM-1003", "release_id": "REL-S2",
     "verification_status": "verified_effective", "evidence_note": "争议闭环，复测有效"},
    {"mitigation_id": "MIT-2005", "defect_id": "MEM-1005", "release_id": "REL-S1",
     "verification_status": "inconclusive", "evidence_note": "样本不足"},
    {"mitigation_id": "MIT-2006", "defect_id": "MEM-1005", "release_id": "REL-S3",
     "verification_status": "ineffective", "evidence_note": "未消除泄漏"}
  ]
}
)JSON";

Dataset currentDataset() {
    return parseDataset(kDefects, kDeployments, kMitigations);
}

// 基线：REL-S2=300、REL-S3=400，MEM-1003 的两条缓解均未达到已核实有效。
// 基线风险分 1003=3*3100=9300 居首；当前期新增部署后 1002 反超，形成排名反转。
const char* kBaselineDeployments = R"JSON(
{
  "deployments": [
    {"release_id": "REL-S1", "lifecycle": "active", "deploy_count": 1200},
    {"release_id": "REL-S2", "lifecycle": "active", "deploy_count": 300},
    {"release_id": "REL-S3", "lifecycle": "active", "deploy_count": 400},
    {"release_id": "REL-S4", "lifecycle": "active", "deploy_count": null},
    {"release_id": "REL-EOL-9", "lifecycle": "retired", "deploy_count": 60},
    {"release_id": "REL-EOL-10", "lifecycle": "retired", "deploy_count": 20}
  ]
}
)JSON";

const char* kBaselineMitigations = R"JSON(
{
  "mitigations": [
    {"mitigation_id": "MIT-2002", "defect_id": "MEM-1002", "release_id": "REL-S3",
     "verification_status": "pending", "evidence_note": "待排期"},
    {"mitigation_id": "MIT-2003", "defect_id": "MEM-1003", "release_id": "REL-S1",
     "verification_status": "pending", "evidence_note": "等待灰度"},
    {"mitigation_id": "MIT-2004", "defect_id": "MEM-1003", "release_id": "REL-S2",
     "verification_status": "disputed", "evidence_note": "结论分歧"},
    {"mitigation_id": "MIT-2005", "defect_id": "MEM-1005", "release_id": "REL-S1",
     "verification_status": "inconclusive", "evidence_note": "样本不足"},
    {"mitigation_id": "MIT-2006", "defect_id": "MEM-1005", "release_id": "REL-S3",
     "verification_status": "ineffective", "evidence_note": "未消除泄漏"}
  ]
}
)JSON";

void group(const std::string& name) {
    std::cout << "\n=== " << name << " ===\n";
}

}  // namespace

int main() {
    // ------------------------------------------------------------------
    group("JSON 解析器");
    {
        JsonValue v = JsonParser::parse(R"({"a":[1,2.5,true,false,null,"x"],"b":{"c":-3}})");
        check(v.isObject(), "顶层为对象");
        check(v.at("a").asArray().size() == 6, "数组长度正确");
        check(v.at("a").asArray()[0].asInt64() == 1, "整数解析");
        check(nearly(v.at("a").asArray()[1].asDouble(), 2.5), "浮点解析");
        check(v.at("a").asArray()[2].asBool(), "布尔 true 解析");
        check(!v.at("a").asArray()[3].asBool(), "布尔 false 解析");
        check(v.at("a").asArray()[4].isNull(), "null 解析");
        check(v.at("b").at("c").asInt64() == -3, "负数与嵌套对象解析");

        JsonValue s = JsonParser::parse(R"("中文Aé\ntab\tx")");
        check(s.asString() == "中文Aé\ntab\tx", "Unicode 与转义解析");

        bool threw = false;
        try { JsonParser::parse("{bad}"); } catch (const JsonError&) { threw = true; }
        check(threw, "非法 JSON 抛出带位置的错误");

        bool threw2 = false;
        try { JsonParser::parse("[1,2]  tail"); } catch (const JsonError&) { threw2 = true; }
        check(threw2, "尾随字符被拒绝");
    }

    // ------------------------------------------------------------------
    group("数据装载与校验");
    {
        bool threw = false;
        try {
            parseDataset(R"({"defects":[]})", kDeployments, kMitigations);
        } catch (const LoadError&) { threw = true; }
        check(threw, "空缺陷列表被拒绝");

        threw = false;
        try {
            const char* bad = R"({"defects":[{"defect_id":"X","impact_level":"weird",
                "alert_count":1,"evidence_confirmed":true,"affected_releases":["R"]}]})";
            parseDataset(bad, kDeployments, kMitigations);
        } catch (const LoadError&) { threw = true; }
        check(threw, "未知 impact_level 枚举被拒绝");

        threw = false;
        try {
            const char* bad = R"({"mitigations":[{"mitigation_id":"M","defect_id":"NOPE",
                "release_id":"REL-S1","verification_status":"pending"}]})";
            parseDataset(kDefects, kDeployments, bad);
        } catch (const LoadError&) { threw = true; }
        check(threw, "缓解记录引用未知缺陷被拒绝");

        bool parsedNull = false;
        {
            Dataset ds = currentDataset();
            for (const auto& d : ds.deployments) {
                if (d.releaseId == "REL-S4") parsedNull = !d.hasDeployCount;
            }
        }
        check(parsedNull, "deploy_count=null 解析为未知而非 0");
    }

    // ------------------------------------------------------------------
    group("受影响范围：退役版本与未知部署");
    Dataset ds = currentDataset();
    std::vector<Assessment> r = runTriage(ds);
    {
        const Assessment* a1001 = find(r, "MEM-1001");
        check(a1001 != nullptr, "MEM-1001 存在");
        check(a1001->retiredReleaseCount == 2, "MEM-1001 两个发行版均退役");
        check(a1001->exposedReleaseCount == 0, "退役发行版不纳入暴露面");
        check(a1001->exposedDeployments == 0, "MEM-1001 暴露部署 E=0");
        check(a1001->unmitigatedDeployments == 0, "MEM-1001 未缓解部署 U=0");
        check(!a1001->hasCoverage, "E=0 时覆盖率为空而非除零");
        check(nearly(a1001->riskScore, 0.0), "告警 4820 的退役缺陷风险分为 0");
        check(a1001->checklist.empty(), "退役缺陷不产生核实清单");
        check(nearly(a1001->confidence, 1.0), "退役缺陷置信度为 1.0（high）");

        const Assessment* a1004 = find(r, "MEM-1004");
        check(a1004->unknownDeploymentReleaseCount == 2, "MEM-1004 两个在保发行版部署均未知");
        check(a1004->exposedDeployments == 0, "未知部署不计入 E，禁止按 0 以外数值猜测");
        const auto* unmapped = findRelease(*a1004, "REL-UNMAPPED");
        check(unmapped != nullptr && unmapped->lifecycle == Lifecycle::Unknown,
              "部署表缺失的发行版标记为生命周期未知");
        check(unmapped != nullptr && !unmapped->deploymentKnown, "缺失发行版部署数未知");
    }

    // ------------------------------------------------------------------
    group("缓解覆盖：只有 verified_effective 计入");
    {
        const Assessment* a1002 = find(r, "MEM-1002");
        // REL-S2 1500 已核实有效；REL-S3 1500 pending；REL-S4 未知
        check(a1002->coveredDeployments == 1500, "已核实有效缓解覆盖 REL-S2 的 1500 部署");
        check(a1002->exposedDeployments == 3000, "E=1500+1500，未知 REL-S4 不计入");
        check(a1002->unmitigatedDeployments == 1500, "U=3000-1500=1500");
        check(a1002->coveredReleaseCount == 1, "仅 1 个发行版计入覆盖");
        check(nearly(a1002->mitigationCoverage, 0.5), "覆盖率=1500/3000=0.50");
        const auto* s3 = findRelease(*a1002, "REL-S3");
        check(s3 != nullptr && !s3->coveredByEffectiveMitigation &&
                  s3->bestStatus == MitigationStatus::Pending,
              "pending 缓解不计入覆盖");

        const Assessment* a1005 = find(r, "MEM-1005");
        check(a1005->coveredDeployments == 0, "inconclusive/ineffective 均不计入覆盖");
        check(a1005->unmitigatedDeployments == 2700, "MEM-1005 U=1200+1500");
        check(nearly(a1005->riskScore, 2700.0), "MEM-1005 R=1*2700");
    }

    // ------------------------------------------------------------------
    group("风险分与排序：低频关键进程优先于高频退役缺陷");
    {
        std::vector<std::string> order;
        for (const auto& a : r) order.push_back(a.defect.defectId);
        // MEM-1002 R=4*1500=6000 居首；MEM-1003 R=3*1500=4500 次之；MEM-1005 R=1*2700=2700；
        // 零分两项：MEM-1004 置信度 0.15 低于 MEM-1001 的 1.0，故 1004 在 1001 之前。
        const std::vector<std::string> expected = {
            "MEM-1002", "MEM-1003", "MEM-1005", "MEM-1004", "MEM-1001"};
        check(order == expected, "排序序列: 1002 > 1003 > 1005 > 1004 > 1001");

        const Assessment* a1002 = find(r, "MEM-1002");
        check(nearly(a1002->impactWeight, 4.0), "关键进程中止权重 W=4");
        check(nearly(a1002->riskScore, 6000.0), "MEM-1002 R=4*1500=6000，虽仅 37 次告警仍居首");
        check(r.front().defect.defectId == "MEM-1002",
              "中止关键进程的低频异常（37 次告警）排在告警 4820 的退役缺陷之前");
        const auto rankOf = [&](const std::string& id) {
            for (size_t i = 0; i < r.size(); ++i)
                if (r[i].defect.defectId == id) return static_cast<int>(i);
            return -1;
        };
        check(rankOf("MEM-1002") < rankOf("MEM-1001"),
              "关键进程中止缺陷排名显著领先于退役高频缺陷");
    }

    // ------------------------------------------------------------------
    group("同分稳定次序（确定性全序）");
    {
        // 构造三个完全同分的缺陷，仅缺陷编号不同；再打乱输入顺序验证输出确定。
        const char* defects = R"JSON(
        {"defects":[
          {"defect_id":"MEM-ZZZ","impact_level":"service_degradation","alert_count":5,
           "evidence_confirmed":true,"affected_releases":["REL-S1"]},
          {"defect_id":"MEM-AAA","impact_level":"service_degradation","alert_count":5,
           "evidence_confirmed":true,"affected_releases":["REL-S1"]},
          {"defect_id":"MEM-MMM","impact_level":"service_degradation","alert_count":5,
           "evidence_confirmed":true,"affected_releases":["REL-S1"]}
        ]})JSON";
        const char* deps = R"JSON(
        {"deployments":[{"release_id":"REL-S1","lifecycle":"active","deploy_count":10}]})JSON";
        Dataset tie = parseDataset(defects, deps, R"({"mitigations":[]})");
        std::vector<Assessment> t1 = runTriage(tie);
        check(t1[0].defect.defectId == "MEM-AAA" && t1[1].defect.defectId == "MEM-MMM" &&
                  t1[2].defect.defectId == "MEM-ZZZ",
              "全同分按缺陷编号字典序稳定排列");

        // 再次运行确认可复现（纯函数，无随机/时间依赖）。
        std::vector<Assessment> t2 = runTriage(tie);
        bool same = t1.size() == t2.size();
        for (size_t i = 0; same && i < t1.size(); ++i)
            same = t1[i].defect.defectId == t2[i].defect.defectId;
        check(same, "重复运行输出次序完全一致");

        // 同分但告警次数不同：告警次数降序优先于编号。
        const char* defects2 = R"JSON(
        {"defects":[
          {"defect_id":"MEM-A","impact_level":"service_degradation","alert_count":1,
           "evidence_confirmed":true,"affected_releases":["REL-S1"]},
          {"defect_id":"MEM-B","impact_level":"service_degradation","alert_count":9,
           "evidence_confirmed":true,"affected_releases":["REL-S1"]}
        ]})JSON";
        std::vector<Assessment> t3 = runTriage(parseDataset(defects2, deps, R"({"mitigations":[]})"));
        check(t3[0].defect.defectId == "MEM-B", "同分时告警次数高者优先（排序键 5）");
    }

    // ------------------------------------------------------------------
    group("置信度分档与核实清单");
    {
        const Assessment* a1004 = find(r, "MEM-1004");
        // 未知部署 2*0.20 + 未知生命周期 1*0.10（REL-UNMAPPED）+ 开放缓解 2*0.05 + 证据未确认 0.25
        // = 0.40 + 0.10 + 0.10 + 0.25 = 0.85 -> 置信度 0.15（low）
        check(nearly(a1004->confidence, 0.15), "MEM-1004 置信度=0.15");
        check(a1004->confidenceBand == "low", "MEM-1004 分档 low");
        std::map<std::string, int> cats;
        for (const auto& item : a1004->checklist) ++cats[item.category];
        check(cats["unknown_deployment"] == 2, "生成 2 条部署范围核实项");
        check(cats["lifecycle_unknown"] == 1, "生成 1 条生命周期核实项");
        check(cats["mitigation_unverified"] == 2, "生成 2 条缓解核实项");
        check(cats["impact_evidence"] == 1, "生成 1 条影响证据核实项");

        const Assessment* a1001 = find(r, "MEM-1001");
        check(a1001->confidenceBand == "high", "证据充分的退役缺陷为 high");

        const Assessment* a1003 = find(r, "MEM-1003");
        // S1、S2 均已核实有效，仅 S3 无记录 -> 开放缓解 1 个，-0.05 -> 0.95 high
        check(nearly(a1003->confidence, 0.95), "MEM-1003 置信度=0.95（high）");
        bool mentionsMissingS3 = false;
        for (const auto& item : a1003->checklist)
            if (item.category == "mitigation_unverified" &&
                item.action.find("REL-S3") != std::string::npos)
                mentionsMissingS3 = true;
        check(mentionsMissingS3, "无缓解记录的 REL-S3 进入核实清单");
    }

    // ------------------------------------------------------------------
    group("基线对比：新增部署与缓解证据更正改变顺序");
    {
        Dataset base = parseDataset(kDefects, kBaselineDeployments, kBaselineMitigations);
        std::vector<Assessment> br = runTriage(base);
        ComparisonReport cmp = compareAssessments(br, r);

        std::vector<std::string> baseOrder;
        for (const auto& a : br) baseOrder.push_back(a.defect.defectId);
        // 基线：1003 R=3*3100=9300 居首；1002 R=4*700=2800 次之
        check(baseOrder.front() == "MEM-1003", "基线第一名是 MEM-1003");
        check(baseOrder[1] == "MEM-1002", "基线第二名是 MEM-1002");

        bool flipFound = false;
        for (const auto& f : cmp.rankFlips) {
            // 基线中 MEM-1003 领先 MEM-1002，当前反转为 MEM-1002 领先。
            if (f.defectIdA == "MEM-1003" && f.defectIdB == "MEM-1002")
                flipFound = true;
        }
        check(flipFound, "检测到 MEM-1003 与 MEM-1002 的排名反转");

        const ComparisonEntry* e1003 = nullptr;
        const ComparisonEntry* e1002 = nullptr;
        for (const auto& e : cmp.entries) {
            if (e.defectId == "MEM-1003") e1003 = &e;
            if (e.defectId == "MEM-1002") e1002 = &e;
        }
        check(e1003 != nullptr && e1003->baselineRank == 1 && e1003->currentRank == 2,
              "MEM-1003 排名 1 -> 2");
        check(e1002 != nullptr && e1002->baselineRank == 2 && e1002->currentRank == 1,
              "MEM-1002 排名 2 -> 1");

        auto hasReasonContaining = [](const ComparisonEntry& e, const std::string& needle) {
            for (const auto& s : e.reasons)
                if (s.find(needle) != std::string::npos) return true;
            return false;
        };
        check(hasReasonContaining(*e1002, "1500") && hasReasonContaining(*e1002, "300"),
              "MEM-1002 归因包含 REL-S2 新增部署 300->1500");
        check(hasReasonContaining(*e1003, "已核实有效"),
              "MEM-1003 归因包含缓解证据更正为已核实有效");
        check(hasReasonContaining(*e1003, "M: 0 -> 2700"),
              "MEM-1003 覆盖部署 M 由 0 增至 2700");
        check(hasReasonContaining(*e1002, "风险分"), "归因解释风险分变化");
    }

    // ------------------------------------------------------------------
    group("新增/移除缺陷的对比条目");
    {
        Dataset base = currentDataset();
        check(base.defects.size() == 5, "基线构造含 5 个缺陷");
        // 当前期：移除 MEM-1001，并新增 MEM-9001；其余缺陷保持不变。
        Dataset cur = currentDataset();
        cur.defects.erase(
            std::remove_if(cur.defects.begin(), cur.defects.end(),
                           [](const DefectRecord& d) { return d.defectId == "MEM-1001"; }),
            cur.defects.end());
        check(cur.defects.size() == 4, "当前期移除 MEM-1001 后含 4 个旧缺陷");
        cur.defects.push_back(DefectRecord{
            "MEM-9001", "新增关键进程缺陷", ImpactLevel::CriticalProcessAbort,
            1, true, "test-source", {"REL-S2"}});
        ComparisonReport cmp = compareAssessments(runTriage(base), runTriage(cur));
        bool hasNew = false, hasRemoved = false;
        for (const auto& e : cmp.entries) {
            if (e.defectId == "MEM-9001" && e.isNew) hasNew = true;
            if (e.defectId == "MEM-1001" && e.isRemoved) hasRemoved = true;
        }
        check(hasNew, "新增缺陷条目标记为 is_new");
        check(hasRemoved, "移除缺陷条目标记为 is_removed");
    }

    // ------------------------------------------------------------------
    group("报告渲染与 JSON 可回读");
    {
        Dataset base = parseDataset(kDefects, kBaselineDeployments, kBaselineMitigations);
        ComparisonReport cmp = compareAssessments(runTriage(base), r);
        ReportOptions opts;
        opts.inputLabel = "current";
        opts.baselineLabel = "baseline";
        opts.includeComparison = true;

        std::string text = renderTextReport(r, opts, &cmp);
        check(text.find("内存缺陷修复分诊报告") != std::string::npos, "文本报告包含标题");
        check(text.find("排序键") != std::string::npos, "文本报告说明排序规则");
        check(text.find("MEM-1002") != std::string::npos, "文本报告包含缺陷明细");
        check(text.find("核实清单") != std::string::npos, "文本报告包含核实清单");
        check(text.find("排名反转") != std::string::npos, "文本报告包含对比章节");

        std::string js = renderJsonReport(r, opts, &cmp);
        JsonValue parsed = JsonParser::parse(js);  // 自生成 JSON 必须合法且可回读
        check(parsed.isObject() && parsed.contains("assessments"), "JSON 报告结构合法");
        check(parsed.at("assessments").asArray().size() == r.size(), "JSON 报告条目数一致");
        check(parsed.contains("comparison"), "JSON 报告包含对比段");
        const JsonValue& first = parsed.at("assessments").asArray().front();
        check(first.at("defect_id").asString() == "MEM-1002", "JSON 报告排名第一为 MEM-1002");
        check(nearly(first.at("risk_score").asDouble(), 6000.0), "JSON 风险分正确");
    }

    // ------------------------------------------------------------------
    group("同一发行版多条缓解记录：已核实有效优先");
    {
        const char* mit = R"JSON(
        {"mitigations":[
          {"mitigation_id":"MIT-OLD","defect_id":"MEM-1005","release_id":"REL-S1",
           "verification_status":"ineffective","evidence_note":"旧措施无效"},
          {"mitigation_id":"MIT-NEW","defect_id":"MEM-1005","release_id":"REL-S1",
           "verification_status":"verified_effective","evidence_note":"新补丁验证通过"}
        ]})JSON";
        Dataset ds2 = parseDataset(kDefects, kDeployments, mit);
        std::vector<Assessment> r2 = runTriage(ds2);
        const Assessment* a = find(r2, "MEM-1005");
        const auto* s1 = findRelease(*a, "REL-S1");
        check(s1->coveredByEffectiveMitigation, "存在已核实有效记录时该发行版计入覆盖");
        check(s1->bestMitigationId == "MIT-NEW", "最佳缓解取已核实有效的 MIT-NEW");
        check(a->coveredDeployments == 1200, "覆盖部署计入 REL-S1 的 1200");
    }

    std::cout << "\n========================================\n";
    std::cout << "检查项: " << g_checks << "，失败: " << g_failures << '\n';
    if (g_failures == 0) {
        std::cout << "全部测试通过\n";
        return 0;
    }
    for (const auto& m : g_failureMessages) std::cout << m << '\n';
    return 1;
}
