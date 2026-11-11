#include "loader.hpp"

#include <fstream>
#include <set>
#include <sstream>

#include "json.hpp"

namespace memtriage {

namespace {

std::string typeName(const JsonValue& v) {
    switch (v.type()) {
        case JsonValue::Type::Null:   return "null";
        case JsonValue::Type::Bool:   return "boolean";
        case JsonValue::Type::Number: return "number";
        case JsonValue::Type::String: return "string";
        case JsonValue::Type::Array:  return "array";
        case JsonValue::Type::Object: return "object";
    }
    return "unknown";
}

const JsonValue& field(const JsonValue& obj, const std::string& key,
                       const std::string& context) {
    if (!obj.isObject()) throw LoadError(context + ": expected object, got " + typeName(obj));
    if (!obj.contains(key)) throw LoadError(context + ": missing required field '" + key + "'");
    return obj.at(key);
}

std::string reqString(const JsonValue& obj, const std::string& key,
                      const std::string& context) {
    const JsonValue& v = field(obj, key, context);
    if (!v.isString()) throw LoadError(context + "." + key + ": expected string, got " + typeName(v));
    return v.asString();
}

std::int64_t reqInt(const JsonValue& obj, const std::string& key,
                    const std::string& context) {
    const JsonValue& v = field(obj, key, context);
    if (!v.isNumber()) throw LoadError(context + "." + key + ": expected integer, got " + typeName(v));
    std::int64_t n = v.asInt64();
    if (n < 0) throw LoadError(context + "." + key + ": must be non-negative, got " + std::to_string(n));
    return n;
}

bool reqBool(const JsonValue& obj, const std::string& key,
             const std::string& context) {
    const JsonValue& v = field(obj, key, context);
    if (!v.isBool()) throw LoadError(context + "." + key + ": expected boolean, got " + typeName(v));
    return v.asBool();
}

std::string optString(const JsonValue& obj, const std::string& key,
                      const std::string& fallback = "") {
    if (!obj.contains(key) || obj.at(key).isNull()) return fallback;
    const JsonValue& v = obj.at(key);
    if (!v.isString()) throw LoadError(std::string("field '") + key + "': expected string or null");
    return v.asString();
}

std::vector<DefectRecord> parseDefects(const JsonValue& root) {
    const JsonValue& arr = field(root, "defects", "defects document");
    if (!arr.isArray()) throw LoadError("defects document: 'defects' must be an array");
    std::vector<DefectRecord> out;
    std::set<std::string> seen;
    for (std::size_t i = 0; i < arr.asArray().size(); ++i) {
        const JsonValue& item = arr.asArray()[i];
        std::string ctx = "defects[" + std::to_string(i) + "]";
        DefectRecord d;
        d.defectId = reqString(item, "defect_id", ctx);
        if (!seen.insert(d.defectId).second) {
            throw LoadError(ctx + ": duplicate defect_id '" + d.defectId + "'");
        }
        d.title = optString(item, "title", "");
        d.impactLevel = parseImpactLevel(reqString(item, "impact_level", ctx));
        d.alertCount = reqInt(item, "alert_count", ctx);
        d.evidenceConfirmed = reqBool(item, "evidence_confirmed", ctx);
        d.evidenceSource = optString(item, "evidence_source", "");
        const JsonValue& rel = field(item, "affected_releases", ctx);
        if (!rel.isArray()) throw LoadError(ctx + ".affected_releases: must be an array");
        for (const auto& r : rel.asArray()) {
            if (!r.isString()) throw LoadError(ctx + ".affected_releases: entries must be strings");
            d.affectedReleases.push_back(r.asString());
        }
        if (d.affectedReleases.empty()) {
            throw LoadError(ctx + " (" + d.defectId + "): affected_releases must not be empty");
        }
        out.push_back(std::move(d));
    }
    if (out.empty()) throw LoadError("defects document: no defect records found");
    return out;
}

std::vector<DeploymentRecord> parseDeployments(const JsonValue& root) {
    const JsonValue& arr = field(root, "deployments", "deployments document");
    if (!arr.isArray()) throw LoadError("deployments document: 'deployments' must be an array");
    std::vector<DeploymentRecord> out;
    std::set<std::string> seen;
    for (std::size_t i = 0; i < arr.asArray().size(); ++i) {
        const JsonValue& item = arr.asArray()[i];
        std::string ctx = "deployments[" + std::to_string(i) + "]";
        DeploymentRecord dep;
        dep.releaseId = reqString(item, "release_id", ctx);
        if (!seen.insert(dep.releaseId).second) {
            throw LoadError(ctx + ": duplicate release_id '" + dep.releaseId + "'");
        }
        dep.lifecycle = parseLifecycle(reqString(item, "lifecycle", ctx));
        const JsonValue& cnt = field(item, "deploy_count", ctx);
        if (cnt.isNull()) {
            dep.hasDeployCount = false;
            dep.deployCount = 0;
        } else if (cnt.isNumber()) {
            dep.deployCount = cnt.asInt64();
            if (dep.deployCount < 0) {
                throw LoadError(ctx + ".deploy_count: must be non-negative");
            }
            dep.hasDeployCount = true;
        } else {
            throw LoadError(ctx + ".deploy_count: expected integer or null, got " + typeName(cnt));
        }
        out.push_back(std::move(dep));
    }
    if (out.empty()) throw LoadError("deployments document: no deployment records found");
    return out;
}

std::vector<MitigationRecord> parseMitigations(const JsonValue& root) {
    if (!root.contains("mitigations")) return {};
    const JsonValue& arr = root.at("mitigations");
    if (!arr.isArray()) throw LoadError("mitigations document: 'mitigations' must be an array");
    std::vector<MitigationRecord> out;
    std::set<std::string> seen;
    for (std::size_t i = 0; i < arr.asArray().size(); ++i) {
        const JsonValue& item = arr.asArray()[i];
        std::string ctx = "mitigations[" + std::to_string(i) + "]";
        MitigationRecord m;
        m.mitigationId = reqString(item, "mitigation_id", ctx);
        if (!seen.insert(m.mitigationId).second) {
            throw LoadError(ctx + ": duplicate mitigation_id '" + m.mitigationId + "'");
        }
        m.defectId = reqString(item, "defect_id", ctx);
        m.releaseId = reqString(item, "release_id", ctx);
        m.status = parseMitigationStatus(reqString(item, "verification_status", ctx));
        m.evidenceNote = optString(item, "evidence_note", "");
        out.push_back(std::move(m));
    }
    return out;
}

std::string readFileOrThrow(const std::string& path) {
    std::ifstream in(path, std::ios::binary);
    if (!in) throw LoadError("cannot open file: " + path);
    std::ostringstream ss;
    ss << in.rdbuf();
    return ss.str();
}

}  // namespace

Dataset parseDataset(const std::string& defectsJson,
                     const std::string& deploymentsJson,
                     const std::string& mitigationsJson) {
    Dataset ds;
    try {
        ds.defects = parseDefects(JsonParser::parse(defectsJson));
        ds.deployments = parseDeployments(JsonParser::parse(deploymentsJson));
        ds.mitigations = parseMitigations(JsonParser::parse(mitigationsJson));
    } catch (const JsonError& e) {
        throw LoadError(std::string("invalid JSON: ") + e.what());
    } catch (const std::runtime_error& e) {
        // 枚举解析错误等
        throw LoadError(e.what());
    }

    std::set<std::string> defectIds;
    for (const auto& d : ds.defects) defectIds.insert(d.defectId);
    for (const auto& m : ds.mitigations) {
        if (!defectIds.count(m.defectId)) {
            throw LoadError("mitigation '" + m.mitigationId +
                            "' references unknown defect_id '" + m.defectId + "'");
        }
    }
    return ds;
}

Dataset loadDataset(const std::string& dir) {
    std::string sep = "/";
    std::string base = dir;
    if (!base.empty() && base.back() == '/') base.pop_back();
    return parseDataset(readFileOrThrow(base + sep + "defect_impacts.json"),
                        readFileOrThrow(base + sep + "release_deployments.json"),
                        readFileOrThrow(base + sep + "mitigation_verifications.json"));
}

}  // namespace memtriage
