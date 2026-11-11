// SPDX-License-Identifier: MIT
#include "certattrib/loader.hpp"

#include <fstream>
#include <sstream>

namespace certattrib {

namespace {

long long asInt(const JsonPtr& v, const std::string& ctx) {
    return static_cast<long long>(v->asNumber(ctx));
}

std::vector<std::string> asStringList(const JsonPtr& v, const std::string& ctx) {
    std::vector<std::string> out;
    for (const auto& e : v->asArray(ctx))
        out.push_back(e->asString(ctx + "[]"));
    return out;
}

}  // namespace

const CertRecord& Dataset::cert(const std::string& id) const {
    auto it = certs.find(id);
    if (it == certs.end())
        throw std::runtime_error("dangling certificate reference: " + id);
    return it->second;
}

const CandidatePath* Dataset::findPath(const std::string& pathId) const {
    for (const auto& p : paths)
        if (p.pathId == pathId) return &p;
    return nullptr;
}

std::vector<CandidatePath> Dataset::pathsOfLeaf(const std::string& leafId) const {
    std::vector<CandidatePath> out;
    for (const auto& p : paths)
        if (p.leafId == leafId) out.push_back(p);
    return out;
}

Dataset loadDataset(const JsonPtr& root) {
    root->asObject("root");
    Dataset ds;

    // ---- certificates ----
    for (const auto& jc : root->at("certificates")->asArray("certificates")) {
        CertRecord c;
        c.id = jc->at("id")->asString("certificate.id");
        c.subject = jc->at("subject")->asString("certificate.subject");
        c.issuer = jc->at("issuer")->asString("certificate.issuer");
        c.keyId = jc->at("key_id")->asString("certificate.key_id");
        c.issuerKeyId = jc->at("issuer_key_id")->asString("certificate.issuer_key_id");
        c.notBefore = asInt(jc->at("not_before"), "certificate.not_before");
        c.notAfter = asInt(jc->at("not_after"), "certificate.not_after");
        if (c.notAfter < c.notBefore)
            throw JsonError("certificate " + c.id + ": not_after < not_before");
        c.isCa = jc->getOr("is_ca", jBool(false))->asBool("certificate.is_ca");
        c.pathLen = static_cast<int>(
            jc->getOr("path_len", jNum(-1))->asNumber("certificate.path_len"));
        if (jc->has("eku"))
            c.eku = asStringList(jc->at("eku"), "certificate.eku");
        if (!ds.certs.emplace(c.id, c).second)
            throw JsonError("duplicate certificate id: " + c.id);
    }

    // ---- trust_anchors ----
    for (const auto& ja : root->at("trust_anchors")->asArray("trust_anchors")) {
        AnchorRecord a;
        a.subject = ja->at("subject")->asString("anchor.subject");
        a.keyId = ja->at("key_id")->asString("anchor.key_id");
        a.anchorId = ja->at("anchor_id")->asString("anchor.anchor_id");
        a.version = asInt(ja->at("version"), "anchor.version");
        a.addedAt = asInt(ja->getOr("added_at", jNum(0)), "anchor.added_at");
        a.removedAt = asInt(ja->getOr("removed_at", jNum(0)), "anchor.removed_at");
        ds.anchors.push_back(a);
    }

    // ---- signature_evidence ----
    for (const auto& je :
         root->at("signature_evidence")->asArray("signature_evidence")) {
        SigEvidence e;
        e.childCertId = je->at("child_cert_id")->asString("evidence.child_cert_id");
        e.issuerKeyId = je->at("issuer_key_id")->asString("evidence.issuer_key_id");
        e.result = je->at("result")->asString("evidence.result");
        if (e.result != "valid" && e.result != "invalid")
            throw JsonError("evidence for " + e.childCertId +
                            ": result must be 'valid' or 'invalid'");
        e.checkedAt = asInt(je->getOr("checked_at", jNum(0)), "evidence.checked_at");
        ds.evidence[e.childCertId].push_back(e);
    }

    // ---- candidate_paths ----
    for (const auto& jp :
         root->at("candidate_paths")->asArray("candidate_paths")) {
        CandidatePath p;
        p.pathId = jp->at("path_id")->asString("path.path_id");
        p.leafId = jp->at("leaf_id")->asString("path.leaf_id");
        p.chain = asStringList(jp->at("chain"), "path.chain");
        if (p.chain.empty() || p.chain.front() != p.leafId)
            throw JsonError("path " + p.pathId +
                            ": chain must start with leaf_id");
        for (const auto& id : p.chain)
            if (!ds.certs.count(id))
                throw JsonError("path " + p.pathId + ": unknown cert " + id);
        ds.paths.push_back(p);
    }

    // ---- channels / policies ----
    for (const auto& jch : root->at("channels")->asArray("channels")) {
        PolicyFields pol;
        pol.channelId = jch->at("channel_id")->asString("channel.channel_id");
        pol.anchorVersion =
            asInt(jch->at("anchor_version"), "channel.anchor_version");
        pol.verificationTime =
            asInt(jch->at("verification_time"), "channel.verification_time");
        pol.requiredEku =
            jch->getOr("required_eku", jStr(""))->str;
        pol.enforceEku =
            jch->getOr("enforce_eku", jBool(true))->asBool("channel.enforce_eku");
        pol.enforceBasicConstraints =
            jch->getOr("enforce_basic_constraints", jBool(true))
                ->asBool("channel.enforce_basic_constraints");
        pol.validityEndpointInclusive =
            jch->getOr("validity_endpoint_inclusive", jBool(true))
                ->asBool("channel.validity_endpoint_inclusive");
        pol.preferredAnchorKeyId =
            jch->getOr("preferred_anchor_key_id", jStr(""))->str;
        pol.rejectOnMissingEvidence =
            jch->getOr("reject_on_missing_evidence", jBool(true))
                ->asBool("channel.reject_on_missing_evidence");
        if (!ds.policies.emplace(pol.channelId, pol).second)
            throw JsonError("duplicate channel id: " + pol.channelId);
    }

    // ---- client_decisions（可选块）----
    if (root->has("client_decisions")) {
        for (const auto& jd :
             root->at("client_decisions")->asArray("client_decisions")) {
            ClientDecision d;
            d.channelId = jd->at("channel_id")->asString("decision.channel_id");
            d.leafId = jd->at("leaf_id")->asString("decision.leaf_id");
            d.verdict = jd->at("verdict")->asString("decision.verdict");
            if (d.verdict != "trusted" && d.verdict != "rejected")
                throw JsonError("decision: verdict must be trusted/rejected");
            d.reasonCode = jd->getOr("reason_code", jStr(""))->str;
            d.lastCheckpoint = jd->getOr("last_checkpoint", jStr(""))->str;
            if (!ds.policies.count(d.channelId))
                throw JsonError("decision references unknown channel: " +
                                d.channelId);
            ds.decisions[d.leafId].push_back(d);
        }
    }

    return ds;
}

Dataset loadDatasetFile(const std::string& path) {
    std::ifstream in(path);
    if (!in) throw std::runtime_error("cannot open dataset: " + path);
    std::ostringstream ss;
    ss << in.rdbuf();
    return loadDataset(parseJson(ss.str()));
}

}  // namespace certattrib
