// SPDX-License-Identifier: MIT
#include "certattrib/engine.hpp"

#include <algorithm>
#include <set>
#include <sstream>

namespace certattrib {

namespace {
constexpr const char* SKIPPED = "skipped_after_failure";

std::string evRef(const SigEvidence& e) {
    return e.childCertId + "@" + e.issuerKeyId;
}

CheckpointResult mkCp(const std::string& name, bool passed,
                      const std::string& reason, const std::string& detail) {
    CheckpointResult r;
    r.checkpoint = name;
    r.passed = passed;
    r.reasonCode = reason;
    r.detail = detail;
    return r;
}
}  // namespace

const SigEvidence* Engine::findEvidence(const std::string& childCertId,
                                        const std::string& issuerKeyId) const {
    auto it = ds_.evidence.find(childCertId);
    if (it == ds_.evidence.end()) return nullptr;
    for (const auto& e : it->second)
        if (e.issuerKeyId == issuerKeyId) return &e;
    return nullptr;
}

std::string Engine::selectAnchor(const PolicyFields& pol,
                                 const CertRecord& top,
                                 std::string& reason,
                                 std::string& detail) const {
    // 快照中与顶端证书同 subject 的全部锚记录。
    std::vector<const AnchorRecord*> sameSubject;
    for (const auto& a : ds_.anchors)
        if (a.subject == top.subject) sameSubject.push_back(&a);

    if (sameSubject.empty()) {
        reason = reason::ANCHOR_NOT_TRUSTED;
        detail = "no anchor with subject '" + top.subject +
                 "' exists in any trust anchor version";
        return "";
    }

    // 在该渠道锚版本下生效（已引入且未移除）的记录。
    auto applicable = [&](const AnchorRecord* a) {
        return a->version <= pol.anchorVersion &&
               (a->removedAt == 0 || a->removedAt > pol.anchorVersion);
    };

    // 1) subject + key 完全匹配的锚。
    std::vector<const AnchorRecord*> exact;
    for (const auto* a : sameSubject)
        if (a->keyId == top.keyId && applicable(a)) exact.push_back(a);

    if (!exact.empty()) {
        // 同名多钥：快照里同 subject 下生效的不同 keyId 数量。
        std::set<std::string> liveKeys;
        for (const auto* a : sameSubject)
            if (applicable(a)) liveKeys.insert(a->keyId);

        if (liveKeys.size() > 1) {
            // 同名异钥必须由策略的 preferred_anchor_key_id 确定性裁决。
            if (pol.preferredAnchorKeyId.empty()) {
                reason = reason::ANCHOR_NAME_COLLISION;
                std::ostringstream os;
                os << "multiple live anchors share subject '" << top.subject
                   << "' and policy sets no preferred_anchor_key_id";
                detail = os.str();
                return "";
            }
            if (pol.preferredAnchorKeyId != top.keyId) {
                reason = reason::ANCHOR_NAME_COLLISION;
                detail = "policy prefers anchor key '" +
                         pol.preferredAnchorKeyId + "', path terminates at key '" +
                         top.keyId + "'";
                return "";
            }
        }
        // 取最高版本的生效记录。
        const AnchorRecord* best = exact.front();
        for (const auto* a : exact)
            if (a->version > best->version) best = a;
        detail = "selected anchor " + best->anchorId + " (version " +
                 std::to_string(best->version) + ")";
        return best->anchorId;
    }

    // 2) subject+key 的锚存在，但在该渠道快照版本下均不生效 → 版本缺失。
    bool exactKeyExists = std::any_of(
        sameSubject.begin(), sameSubject.end(),
        [&](const AnchorRecord* a) { return a->keyId == top.keyId; });
    if (exactKeyExists) {
        reason = reason::ANCHOR_VERSION_ABSENT;
        std::set<long long> versions;
        for (const auto* a : sameSubject)
            if (a->keyId == top.keyId) versions.insert(a->version);
        std::ostringstream os;
        os << "anchor with key '" << top.keyId << "' exists only in version(s)";
        for (long long v : versions) os << " " << v;
        os << "; channel snapshot is version " << pol.anchorVersion;
        detail = os.str();
        return "";
    }

    // 3) 同 subject 但 key 全部不同。
    std::set<std::string> liveKeys;
    for (const auto* a : sameSubject)
        if (applicable(a)) liveKeys.insert(a->keyId);
    if (liveKeys.size() > 1) {
        reason = reason::ANCHOR_NAME_COLLISION;
        std::ostringstream os;
        os << "path key '" << top.keyId
           << "' matches none of the live same-subject anchor keys:";
        for (const auto& k : liveKeys) os << " " << k;
        detail = os.str();
        return "";
    }
    reason = reason::ANCHOR_NOT_TRUSTED;
    detail = "subject '" + top.subject + "' is trusted under a different key ('" +
             (liveKeys.empty() ? std::string("none live") : *liveKeys.begin()) +
             "'), path key is '" + top.keyId + "'";
    return "";
}

PathVerdict Engine::evaluatePath(const PolicyFields& pol,
                                 const CandidatePath& path) const {
    PathVerdict v;
    v.pathId = path.pathId;
    v.accepted = false;

    auto fail = [&](const std::string& checkpoint, const std::string& r,
                    const std::string& detail) {
        v.checkpoints.push_back(mkCp(checkpoint, false, r, detail));
        v.firstFailureCheckpoint = checkpoint;
        v.reasonCode = r;
        // 后续检查点标记为跳过，保证两端检查点序列始终可对齐比较。
        static const std::vector<std::string> all = {
            cp::PATH_CONSTRUCTION, cp::SIGNATURE_CHAIN, cp::ANCHOR_SELECTION,
            cp::VALIDITY_WINDOW, cp::PURPOSE_CONSTRAINT};
        bool seen = false;
        for (const auto& name : all) {
            if (seen)
                v.checkpoints.push_back(
                    mkCp(name, false, SKIPPED, "not evaluated: earlier check failed"));
            if (name == checkpoint) seen = true;
        }
    };

    // ---- 检查点 1：路径构造（签发关系能否逐跳拼接）----
    for (size_t i = 0; i + 1 < path.chain.size(); ++i) {
        const CertRecord& child = ds_.cert(path.chain[i]);
        const CertRecord& parent = ds_.cert(path.chain[i + 1]);
        if (child.issuer != parent.subject || child.issuerKeyId != parent.keyId) {
            fail(cp::PATH_CONSTRUCTION, reason::BROKEN_CHAIN,
                 child.id + " expects issuer '" + child.issuer + "'/key '" +
                     child.issuerKeyId + "' but next chain element is " +
                     parent.id + " ('" + parent.subject + "'/key '" +
                     parent.keyId + "')");
            return v;
        }
    }
    v.checkpoints.push_back(
        mkCp(cp::PATH_CONSTRUCTION, true, reason::OK,
             std::to_string(path.chain.size()) + " certificate(s) linked"));

    // ---- 检查点 2：签名链（证据驱动；顶端锚证书自身签名由锚信任覆盖）----
    for (size_t i = 0; i + 1 < path.chain.size(); ++i) {
        const CertRecord& child = ds_.cert(path.chain[i]);
        const SigEvidence* e = findEvidence(child.id, child.issuerKeyId);
        if (!e) {
            // 是否存在针对同一证书但另一把签发公钥的证据（同名异钥/重签特征）。
            auto alt = ds_.evidence.find(child.id);
            bool hasOtherKey =
                alt != ds_.evidence.end() &&
                std::any_of(alt->second.begin(), alt->second.end(),
                            [&](const SigEvidence& x) {
                                return x.issuerKeyId != child.issuerKeyId;
                            });
            std::string detail = "no signature evidence for " + child.id +
                                 " under issuer key '" + child.issuerKeyId + "'";
            if (hasOtherKey)
                detail += " (evidence exists under a different issuer key: "
                          "same-name/different-key reissue)";
            if (pol.rejectOnMissingEvidence) {
                fail(cp::SIGNATURE_CHAIN,
                     hasOtherKey ? reason::SIG_KEY_MISMATCH
                                 : reason::SIG_EVIDENCE_MISSING,
                     detail);
                return v;
            }
            v.checkpoints.push_back(mkCp(
                cp::SIGNATURE_CHAIN, true, reason::OK,
                detail + "; accepted because reject_on_missing_evidence=false"));
            continue;
        }
        v.evidenceUsed.push_back(evRef(*e));
        if (e->result == "invalid") {
            fail(cp::SIGNATURE_CHAIN, reason::SIG_INVALID,
                 "evidence " + evRef(*e) + " concludes invalid (checked_at=" +
                     std::to_string(e->checkedAt) + ")");
            return v;
        }
    }
    v.checkpoints.push_back(
        mkCp(cp::SIGNATURE_CHAIN, true, reason::OK,
             std::to_string(v.evidenceUsed.size()) + " valid signature evidence record(s)"));

    // ---- 检查点 3：锚选择（版本快照 + 同名异钥裁决）----
    const CertRecord& top = ds_.cert(path.chain.back());
    {
        std::string ar, ad;
        std::string anchorId = selectAnchor(pol, top, ar, ad);
        if (anchorId.empty()) {
            fail(cp::ANCHOR_SELECTION, ar, ad);
            return v;
        }
        v.checkpoints.push_back(mkCp(cp::ANCHOR_SELECTION, true, reason::OK, ad));
    }

    // ---- 检查点 4：有效期时间窗（端点语义由策略字段决定）----
    for (const auto& cid : path.chain) {
        const CertRecord& c = ds_.cert(cid);
        long long t = pol.verificationTime;
        const bool inclusive = pol.validityEndpointInclusive;
        if (inclusive ? (t < c.notBefore) : (t <= c.notBefore)) {
            fail(cp::VALIDITY_WINDOW, reason::NOT_YET_VALID,
                 c.id + " not_before=" + std::to_string(c.notBefore) +
                     " verification_time=" + std::to_string(t) +
                     (inclusive ? " (endpoints inclusive)" : " (endpoints exclusive)"));
            return v;
        }
        if (inclusive ? (t > c.notAfter) : (t >= c.notAfter)) {
            fail(cp::VALIDITY_WINDOW, reason::EXPIRED,
                 c.id + " not_after=" + std::to_string(c.notAfter) +
                     " verification_time=" + std::to_string(t) +
                     (inclusive ? " (endpoints inclusive)" : " (endpoints exclusive)"));
            return v;
        }
    }
    v.checkpoints.push_back(mkCp(
        cp::VALIDITY_WINDOW, true, reason::OK,
        "all certificates within window at t=" +
            std::to_string(pol.verificationTime) +
            (pol.validityEndpointInclusive ? "; endpoints inclusive [nb,na]"
                                           : "; endpoints exclusive (nb,na)")));

    // ---- 检查点 5：用途约束（CA 基本约束 / pathLen / EKU）----
    if (pol.enforceBasicConstraints) {
        for (size_t i = 1; i < path.chain.size(); ++i) {
            const CertRecord& c = ds_.cert(path.chain[i]);
            if (!c.isCa) {
                fail(cp::PURPOSE_CONSTRAINT, reason::PURPOSE_NOT_CA,
                     c.id + " signs another certificate but is_ca=false");
                return v;
            }
            // 位于索引 i 的 CA，其下方的中间 CA 证书数量为 i-1（叶在索引 0）。
            if (c.pathLen >= 0 && (static_cast<int>(i) - 1) > c.pathLen) {
                fail(cp::PURPOSE_CONSTRAINT, reason::PURPOSE_PATH_LENGTH,
                     c.id + " path_len=" + std::to_string(c.pathLen) +
                         " but " + std::to_string(i - 1) +
                         " intermediate CA(s) appear below it");
                return v;
            }
        }
    }
    if (pol.enforceEku && !pol.requiredEku.empty()) {
        const CertRecord& leaf = ds_.cert(path.chain.front());
        bool found = std::find(leaf.eku.begin(), leaf.eku.end(),
                               pol.requiredEku) != leaf.eku.end();
        if (!found) {
            fail(cp::PURPOSE_CONSTRAINT, reason::PURPOSE_EKU,
                 leaf.id + " EKU does not contain required purpose '" +
                     pol.requiredEku + "'");
            return v;
        }
    }
    v.checkpoints.push_back(
        mkCp(cp::PURPOSE_CONSTRAINT, true, reason::OK, "purpose constraints satisfied"));

    v.accepted = true;
    v.reasonCode = reason::OK;
    return v;
}

ChannelVerdict Engine::evaluateChannel(const PolicyFields& pol,
                                       const std::string& leafId) const {
    ChannelVerdict out;
    out.channelId = pol.channelId;
    out.leafId = leafId;

    auto candidates = ds_.pathsOfLeaf(leafId);
    if (candidates.empty()) {
        out.finalVerdict = "rejected";
        out.reasonCode = reason::NO_CANDIDATE_PATH;
    } else {
        for (const auto& p : candidates)
            out.paths.push_back(evaluatePath(pol, p));
        for (const auto& pv : out.paths) {
            if (pv.accepted) {
                out.finalVerdict = "trusted";
                out.reasonCode = reason::OK;
                out.acceptedPathId = pv.pathId;
                break;
            }
        }
        if (out.finalVerdict.empty()) {
            out.finalVerdict = "rejected";
            // 汇总原因取最早失败检查点排名最靠前的路径，保证报告稳定可解释。
            const PathVerdict* best = &out.paths.front();
            for (const auto& pv : out.paths) {
                if (checkpointRank(pv.firstFailureCheckpoint) <
                    checkpointRank(best->firstFailureCheckpoint))
                    best = &pv;
            }
            out.reasonCode = best->reasonCode;
        }
    }

    // 与客户端判定日志对账：客户端判定能否由该渠道策略 + 证据推出。
    auto dit = ds_.decisions.find(leafId);
    if (dit == ds_.decisions.end()) {
        out.clientDecisionExplained = false;
        out.clientMismatchDetail = "no client decision record for this leaf";
    } else {
        const ClientDecision* cd = nullptr;
        for (const auto& d : dit->second)
            if (d.channelId == pol.channelId) cd = &d;
        if (!cd) {
            out.clientDecisionExplained = false;
            out.clientMismatchDetail =
                "no client decision record from channel " + pol.channelId;
        } else if (cd->verdict == out.finalVerdict) {
            out.clientDecisionExplained = true;
            out.clientMismatchDetail =
                "client verdict '" + cd->verdict + "' matches policy-derived verdict";
        } else {
            out.clientDecisionExplained = false;
            out.clientMismatchDetail =
                "client says '" + cd->verdict + "' (" +
                (cd->reasonCode.empty() ? std::string("no reason") : cd->reasonCode) +
                ") but policy + evidence derive '" + out.finalVerdict + "' (" +
                out.reasonCode + ")";
        }
    }
    return out;
}

int Engine::checkpointRank(const std::string& cpName) {
    if (cpName == cp::PATH_CONSTRUCTION) return 0;
    if (cpName == cp::SIGNATURE_CHAIN) return 1;
    if (cpName == cp::ANCHOR_SELECTION) return 2;
    if (cpName == cp::VALIDITY_WINDOW) return 3;
    if (cpName == cp::PURPOSE_CONSTRAINT) return 4;
    if (cpName == cp::FINAL_DECISION) return 5;
    return 6;
}

std::vector<std::string> Engine::allLeaves() const {
    std::set<std::string> leaves;
    for (const auto& p : ds_.paths) leaves.insert(p.leafId);
    for (const auto& kv : ds_.decisions) leaves.insert(kv.first);
    return {leaves.begin(), leaves.end()};
}

namespace {

// 检查点 → 归因类别。
std::string categoryOf(const std::string& checkpoint, const std::string& r) {
    if (checkpoint == cp::PATH_CONSTRUCTION)
        return divergence::INCOMPLETE_CANDIDATE;
    if (checkpoint == cp::SIGNATURE_CHAIN) {
        // 签名证据缺失/异钥属于候选路径证据不完整；硬签名失效对两端必然一致。
        if (r == reason::SIG_EVIDENCE_MISSING || r == reason::SIG_KEY_MISMATCH)
            return divergence::INCOMPLETE_CANDIDATE;
        return divergence::INCOMPLETE_CANDIDATE;
    }
    if (checkpoint == cp::ANCHOR_SELECTION) return divergence::TRUST_ANCHOR_CHANGE;
    if (checkpoint == cp::VALIDITY_WINDOW) return divergence::TIME_WINDOW;
    if (checkpoint == cp::PURPOSE_CONSTRAINT) return divergence::PURPOSE_CONSTRAINT;
    return divergence::UNEXPLAINED;
}

std::string fieldDelta(const std::string& name, const std::string& va,
                       const std::string& vb) {
    return name + ": " + va + " -> " + vb;
}

}  // namespace

DivergenceReport Engine::compare(const std::string& leafId,
                                 const std::string& channelA,
                                 const std::string& channelB) const {
    const PolicyFields& pa = ds_.policies.at(channelA);
    const PolicyFields& pb = ds_.policies.at(channelB);
    ChannelVerdict va = evaluateChannel(pa, leafId);
    ChannelVerdict vb = evaluateChannel(pb, leafId);

    DivergenceReport rep;
    rep.leafId = leafId;
    rep.channelA = channelA;
    rep.channelB = channelB;
    rep.divergent = va.finalVerdict != vb.finalVerdict;
    rep.bothClientDecisionsExplained =
        va.clientDecisionExplained && vb.clientDecisionExplained;

    // 逐路径、逐检查点对齐，取排名最靠前的差异检查点。
    std::map<std::string, const PathVerdict*> ma, mb;
    for (const auto& pv : va.paths) ma[pv.pathId] = &pv;
    for (const auto& pv : vb.paths) mb[pv.pathId] = &pv;

    std::set<std::string> categories;
    int earliest = 99;
    for (const auto& kv : ma) {
        auto itb = mb.find(kv.first);
        if (itb == mb.end()) continue;  // 候选路径集合来自同一数据集，不会发生
        const auto& ca = kv.second->checkpoints;
        const auto& cb = itb->second->checkpoints;
        size_t n = std::min(ca.size(), cb.size());
        for (size_t i = 0; i < n; ++i) {
            // 失败后的跳过占位不构成独立分歧：真实分歧已在更早的失败检查点记录，
            // 否则一端的跳过行会把后续全部检查点错误地纳入归因，导致 mixed 误判。
            if (ca[i].reasonCode == SKIPPED || cb[i].reasonCode == SKIPPED)
                continue;
            if (ca[i].passed != cb[i].passed ||
                ca[i].reasonCode != cb[i].reasonCode) {
                int rank = checkpointRank(ca[i].checkpoint);
                if (rank < earliest) {
                    earliest = rank;
                    rep.earliestCheckpoint = ca[i].checkpoint;
                }
                categories.insert(
                    categoryOf(ca[i].checkpoint,
                               ca[i].passed ? cb[i].reasonCode : ca[i].reasonCode));
                rep.attributionFactors.push_back(
                    kv.first + " @ " + ca[i].checkpoint + ": " +
                    channelA + "=" + ca[i].reasonCode +
                    (ca[i].passed ? "" : (" (" + ca[i].detail + ")")) + " | " +
                    channelB + "=" + cb[i].reasonCode +
                    (cb[i].passed ? "" : (" (" + cb[i].detail + ")")));
            }
        }
    }

    if (rep.earliestCheckpoint.empty() && rep.divergent) {
        // 所有路径级检查点一致却终审不同（理论上仅当汇总规则差异导致）。
        rep.earliestCheckpoint = cp::FINAL_DECISION;
        categories.insert(divergence::UNEXPLAINED);
    }

    // 策略字段差异清单（只列实际不同的字段）。
    auto addDelta = [&](const std::string& name, const std::string& x,
                        const std::string& y) {
        if (x != y) rep.policyDeltas.push_back(fieldDelta(name, x, y));
    };
    addDelta("anchor_version", std::to_string(pa.anchorVersion),
             std::to_string(pb.anchorVersion));
    addDelta("verification_time", std::to_string(pa.verificationTime),
             std::to_string(pb.verificationTime));
    addDelta("required_eku", pa.requiredEku, pb.requiredEku);
    addDelta("enforce_eku", pa.enforceEku ? "true" : "false",
             pb.enforceEku ? "true" : "false");
    addDelta("enforce_basic_constraints",
             pa.enforceBasicConstraints ? "true" : "false",
             pb.enforceBasicConstraints ? "true" : "false");
    addDelta("validity_endpoint_inclusive",
             pa.validityEndpointInclusive ? "true" : "false",
             pb.validityEndpointInclusive ? "true" : "false");
    addDelta("preferred_anchor_key_id", pa.preferredAnchorKeyId,
             pb.preferredAnchorKeyId);
    addDelta("reject_on_missing_evidence",
             pa.rejectOnMissingEvidence ? "true" : "false",
             pb.rejectOnMissingEvidence ? "true" : "false");

    // 归因裁决。
    if (!rep.divergent) {
        // 引擎判定一致：检查客户端日志是否仍然分歧（即策略无法解释的现场差异）。
        auto dit = ds_.decisions.find(leafId);
        bool clientDisagrees = false;
        if (dit != ds_.decisions.end()) {
            const ClientDecision *da = nullptr, *db = nullptr;
            for (const auto& d : dit->second) {
                if (d.channelId == channelA) da = &d;
                if (d.channelId == channelB) db = &d;
            }
            if (da && db) clientDisagrees = da->verdict != db->verdict;
        }
        if (clientDisagrees) {
            rep.attribution = divergence::UNEXPLAINED;
            rep.earliestCheckpoint = cp::FINAL_DECISION;
            rep.attributionFactors.push_back(
                "policy-derived verdicts agree ('" + va.finalVerdict +
                "') but client logs disagree; at least one client decision is not derivable from its policy");
        } else if (categories.empty()) {
            rep.attribution = divergence::NONE;
        } else if (categories.size() > 1) {
            rep.attribution = divergence::MIXED;
        } else {
            rep.attribution = *categories.begin();  // 终审一致但路径级处理已有差异
        }
    } else {
        rep.attribution = categories.size() > 1 ? divergence::MIXED
                                                : *categories.begin();
    }

    return rep;
}

}  // namespace certattrib
