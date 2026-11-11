// SPDX-License-Identifier: MIT
#include "certattrib/report.hpp"

#include <sstream>

namespace certattrib {

JsonPtr checkpointToJson(const CheckpointResult& c) {
    auto o = jObj();
    o->obj["checkpoint"] = jStr(c.checkpoint);
    o->obj["passed"] = jBool(c.passed);
    o->obj["reason_code"] = jStr(c.reasonCode);
    o->obj["detail"] = jStr(c.detail);
    return o;
}

JsonPtr pathVerdictToJson(const PathVerdict& p) {
    auto o = jObj();
    o->obj["path_id"] = jStr(p.pathId);
    o->obj["accepted"] = jBool(p.accepted);
    o->obj["first_failure_checkpoint"] = jStr(p.firstFailureCheckpoint);
    o->obj["reason_code"] = jStr(p.reasonCode);
    auto cps = jArr();
    for (const auto& c : p.checkpoints) cps->arr.push_back(checkpointToJson(c));
    o->obj["checkpoints"] = cps;
    auto ev = jArr();
    for (const auto& ref : p.evidenceUsed) ev->arr.push_back(jStr(ref));
    o->obj["evidence_used"] = ev;
    return o;
}

JsonPtr channelVerdictToJson(const ChannelVerdict& v) {
    auto o = jObj();
    o->obj["channel_id"] = jStr(v.channelId);
    o->obj["leaf_id"] = jStr(v.leafId);
    o->obj["final_verdict"] = jStr(v.finalVerdict);
    o->obj["reason_code"] = jStr(v.reasonCode);
    o->obj["accepted_path_id"] = jStr(v.acceptedPathId);
    o->obj["derived_from_policy"] = jBool(v.derivedFromPolicy);
    o->obj["client_decision_explained"] = jBool(v.clientDecisionExplained);
    o->obj["client_mismatch_detail"] = jStr(v.clientMismatchDetail);
    auto ps = jArr();
    for (const auto& p : v.paths) ps->arr.push_back(pathVerdictToJson(p));
    o->obj["paths"] = ps;
    return o;
}

JsonPtr divergenceToJson(const DivergenceReport& r) {
    auto o = jObj();
    o->obj["leaf_id"] = jStr(r.leafId);
    o->obj["channel_pair"] = jStr(r.channelA + " vs " + r.channelB);
    o->obj["divergent"] = jBool(r.divergent);
    o->obj["earliest_checkpoint"] = jStr(r.earliestCheckpoint);
    o->obj["attribution"] = jStr(r.attribution);
    auto factors = jArr();
    for (const auto& f : r.attributionFactors) factors->arr.push_back(jStr(f));
    o->obj["attribution_factors"] = factors;
    auto deltas = jArr();
    for (const auto& d : r.policyDeltas) deltas->arr.push_back(jStr(d));
    o->obj["policy_deltas"] = deltas;
    o->obj["both_client_decisions_explained"] =
        jBool(r.bothClientDecisionsExplained);
    return o;
}

JsonPtr buildFullReport(const Engine& engine, const Dataset& ds) {
    auto root = jObj();
    root->obj["tool"] = jStr("cert-attrib (offline cert divergence attribution)");
    root->obj["checkpoint_order"] = [&] {
        auto a = jArr();
        for (const char* n : {cp::PATH_CONSTRUCTION, cp::SIGNATURE_CHAIN,
                              cp::ANCHOR_SELECTION, cp::VALIDITY_WINDOW,
                              cp::PURPOSE_CONSTRAINT, cp::FINAL_DECISION})
            a->arr.push_back(jStr(n));
        return a;
    }();

    auto verdicts = jArr();
    for (const auto& leaf : engine.allLeaves()) {
        for (const auto& kv : ds.policies)
            verdicts->arr.push_back(
                channelVerdictToJson(engine.evaluateChannel(kv.second, leaf)));
    }
    root->obj["channel_verdicts"] = verdicts;

    std::vector<std::string> channels;
    for (const auto& kv : ds.policies) channels.push_back(kv.first);
    auto divs = jArr();
    for (const auto& leaf : engine.allLeaves()) {
        for (size_t i = 0; i < channels.size(); ++i)
            for (size_t j = i + 1; j < channels.size(); ++j)
                divs->arr.push_back(
                    divergenceToJson(engine.compare(leaf, channels[i], channels[j])));
    }
    root->obj["divergence_reports"] = divs;
    return root;
}

std::string renderTextReport(const JsonPtr& report) {
    std::ostringstream os;
    os << "================ 证书判定分歧离线归因报告 ================\n";
    os << "检查点顺序: ";
    const auto& order = report->at("checkpoint_order")->arr;
    for (size_t i = 0; i < order.size(); ++i) {
        if (i) os << " -> ";
        os << order[i]->str;
    }
    os << "\n\n";

    os << "-------------------- 渠道判定 --------------------\n";
    for (const auto& v : report->at("channel_verdicts")->arr) {
        os << "[" << v->at("channel_id")->str << "] 叶证书 "
           << v->at("leaf_id")->str << " => "
           << (v->at("final_verdict")->str == "trusted" ? "信任" : "拒绝")
           << " (" << v->at("reason_code")->str << ")"
           << (v->at("accepted_path_id")->str.empty()
                   ? ""
                   : "  接受路径=" + v->at("accepted_path_id")->str)
           << "\n";
        for (const auto& p : v->at("paths")->arr) {
            os << "    路径 " << p->at("path_id")->str << ": "
               << (p->at("accepted")->boolean ? "通过" : "拒绝")
               << "  首个失败检查点="
               << p->at("first_failure_checkpoint")->str << "\n";
            for (const auto& c : p->at("checkpoints")->arr) {
                os << "      - " << c->at("checkpoint")->str << ": "
                   << (c->at("passed")->boolean ? "PASS" : "FAIL")
                   << " [" << c->at("reason_code")->str << "] "
                   << c->at("detail")->str << "\n";
            }
            os << "      引用证据: ";
            bool first = true;
            for (const auto& e : p->at("evidence_used")->arr) {
                if (!first) os << ", ";
                first = false;
                os << e->str;
            }
            os << "\n";
        }
        os << "    客户端判定可由策略解释: "
           << (v->at("client_decision_explained")->boolean ? "是" : "否")
           << " — " << v->at("client_mismatch_detail")->str << "\n\n";
    }

    os << "-------------------- 分歧归因 --------------------\n";
    for (const auto& d : report->at("divergence_reports")->arr) {
        os << "叶证书 " << d->at("leaf_id")->str << " | "
           << d->at("channel_pair")->str << " | "
           << (d->at("divergent")->boolean ? "终审分歧" : "终审一致")
           << " | 最早分歧检查点="
           << (d->at("earliest_checkpoint")->str.empty()
                   ? std::string("(无)")
                   : d->at("earliest_checkpoint")->str)
           << " | 归因=" << d->at("attribution")->str << "\n";
        for (const auto& f : d->at("attribution_factors")->arr)
            os << "    因素: " << f->str << "\n";
        os << "    策略差异:\n";
        for (const auto& dl : d->at("policy_deltas")->arr)
            os << "      * " << dl->str << "\n";
        os << "    两端客户端判定均可解释: "
           << (d->at("both_client_decisions_explained")->boolean ? "是" : "否")
           << "\n\n";
    }
    return os.str();
}

}  // namespace certattrib
