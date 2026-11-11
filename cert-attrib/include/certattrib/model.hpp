// SPDX-License-Identifier: MIT
// 归因程序的数据模型：输入记录结构、检查点与拒绝原因常量。
// 所有标识均为合成字符串标识（如 "cert-leaf-1"），不处理真实 X.509 编码；
// 密码学签名验证结果由维护组以结构化记录提供，本程序只消费这些结论作为证据。
#pragma once

#include <map>
#include <string>
#include <vector>

namespace certattrib {

// ---- 检查点（按引擎实际执行顺序；分歧定位即取两端首个结论不同的检查点）----
namespace cp {
constexpr const char* PATH_CONSTRUCTION = "path_construction";  // 候选链能否拼出
constexpr const char* SIGNATURE_CHAIN  = "signature_chain";    // 链上签名证据
constexpr const char* ANCHOR_SELECTION = "anchor_selection";   // 锚版本与同名锚选择
constexpr const char* VALIDITY_WINDOW  = "validity_window";    // 有效期时间窗
constexpr const char* PURPOSE_CONSTRAINT = "purpose_constraint"; // EKU/CA/路径长度
constexpr const char* FINAL_DECISION   = "final_decision";     // 跨路径汇总判定
}  // namespace cp

// ---- 拒绝原因码 ----
namespace reason {
constexpr const char* OK                     = "ok";
constexpr const char* BROKEN_CHAIN           = "broken_chain";            // 链中存在无法解析的签发关系
constexpr const char* SIG_EVIDENCE_MISSING   = "signature_evidence_missing"; // 该签发关系没有任何证据记录
constexpr const char* SIG_INVALID            = "signature_invalid";       // 证据结论为 invalid
constexpr const char* SIG_KEY_MISMATCH       = "signature_key_mismatch";  // 证据所用公钥与签发者当前公钥不符（同名异钥/重签）
constexpr const char* ANCHOR_NOT_TRUSTED     = "anchor_not_trusted";      // 顶端证书不在该渠道信任锚集合中
constexpr const char* ANCHOR_VERSION_ABSENT  = "anchor_version_absent";   // 锚存在但所需版本未包含在快照中
constexpr const char* ANCHOR_NAME_COLLISION  = "anchor_name_collision";   // 同名多钥且策略未指定/无法选出锚
constexpr const char* NOT_YET_VALID          = "not_yet_valid";           // 验证时刻早于 notBefore
constexpr const char* EXPIRED                = "expired";                 // 验证时刻晚于 notAfter
constexpr const char* PURPOSE_EKU            = "purpose_eku_denied";      // EKU 不含策略要求的用途
constexpr const char* PURPOSE_NOT_CA         = "purpose_not_ca";          // 中间证书缺少 CA 基本约束
constexpr const char* PURPOSE_PATH_LENGTH    = "purpose_path_length";     // 违反 pathLenConstraint
constexpr const char* NO_CANDIDATE_PATH      = "no_candidate_path";       // 叶证书没有任何候选路径
}  // namespace reason

// ---- 分歧归因类别 ----
namespace divergence {
constexpr const char* NONE                 = "none";
constexpr const char* INCOMPLETE_CANDIDATE = "incomplete_candidate_path"; // 候选路径不完整（链断裂/证据缺失致路径不可用）
constexpr const char* TRUST_ANCHOR_CHANGE  = "trust_anchor_change";       // 信任锚版本或同名锚选择差异
constexpr const char* TIME_WINDOW          = "time_window";               // 验证时刻或有效期边界差异
constexpr const char* PURPOSE_CONSTRAINT   = "purpose_constraint";        // 用途约束差异
constexpr const char* MIXED                = "mixed";                    // 多个独立因素共同作用
constexpr const char* UNEXPLAINED          = "unexplained";              // 两端策略推不出各自的客户端判定
}  // namespace divergence

// 证书记录（叶 / 中间 / 根均可）。
struct CertRecord {
    std::string id;
    std::string subject;            // 主体名（合成名，可能重名 → 同名异钥）
    std::string issuer;             // 签发者主体名；自签名根等于 subject
    std::string keyId;              // 本证书公钥标识
    std::string issuerKeyId;        // 签发所用公钥标识（用于与父证书 keyId / 锚 keyId 比对）
    long long notBefore = 0;        // epoch 秒
    long long notAfter = 0;         // epoch 秒
    bool isCa = false;
    int pathLen = -1;              // pathLenConstraint，-1 表示无约束
    std::vector<std::string> eku;  // 增强型密钥用途，空表示不含 EKU 扩展
};

// 信任锚版本记录。
struct AnchorRecord {
    std::string subject;
    std::string keyId;
    std::string anchorId;          // 锚实例唯一标识
    long long version = 0;         // 单调递增版本号
    long long addedAt = 0;         // 进入该版本的时刻
    long long removedAt = 0;       // 0 表示仍在最新快照中
};

// 签名验证证据：维护组对 (childCert, issuerKeyId) 这一签发关系给出的结论。
struct SigEvidence {
    std::string childCertId;       // 被签发证书
    std::string issuerKeyId;       // 验证时使用的签发者公钥标识
    std::string result;            // "valid" | "invalid"；记录整体缺失即证据缺失
    long long checkedAt = 0;       // 证据产生时刻
};

// 候选路径：叶证书 id 与自叶向根的证书 id 序列（含叶，不含锚实例本身）。
struct CandidatePath {
    std::string pathId;
    std::string leafId;
    std::vector<std::string> chain; // chain.front() == leafId
};

// 单个渠道支持的策略字段（见 README 策略字段表）。
struct PolicyFields {
    std::string channelId;
    long long anchorVersion = 0;                       // 信任锚快照版本
    long long verificationTime = 0;                    // 验证时刻（epoch 秒）
    std::string requiredEku;                           // 要求的 EKU，空表示不检查
    bool enforceEku = true;                            // 是否强制 EKU 检查
    bool enforceBasicConstraints = true;               // 是否校验 CA / pathLen
    bool validityEndpointInclusive = true;             // 有效期端点是否纳入有效区间
    std::string preferredAnchorKeyId;                  // 同名异钥时优先选择的锚 keyId；空=无偏好
    bool rejectOnMissingEvidence = true;               // 签名证据缺失时是否拒绝（false=保守放行但标注）
};

// 客户端判定日志（作为被解释对象，不参与引擎推导）。
struct ClientDecision {
    std::string channelId;
    std::string leafId;
    std::string verdict;            // "trusted" | "rejected"
    std::string reasonCode;         // 客户端自报原因，可能为空或与引擎不一致
    std::string lastCheckpoint;     // 客户端日志中的最后检查点
};

// 引擎对单条路径在单个渠道下的逐检查点结论。
struct CheckpointResult {
    std::string checkpoint;
    bool passed = false;
    std::string reasonCode;         // 未通过时的原因码
    std::string detail;             // 人类可读细节
};

struct PathVerdict {
    std::string pathId;
    bool accepted = false;
    std::string firstFailureCheckpoint;
    std::string reasonCode;
    std::vector<CheckpointResult> checkpoints;
    std::vector<std::string> evidenceUsed;  // 实际引用的证据 (childCertId@issuerKeyId)
};

struct ChannelVerdict {
    std::string channelId;
    std::string leafId;
    std::string finalVerdict;       // "trusted" | "rejected"
    std::string reasonCode;
    std::string acceptedPathId;     // 接受时为路径 id，否则空
    std::vector<PathVerdict> paths;
    // 引擎结论能否由该渠道策略 + 记录证据推出（恒为 true；该字段描述的是推导完整性）。
    bool derivedFromPolicy = true;
    // 客户端判定是否与引擎按策略推出的结论一致。
    bool clientDecisionExplained = false;
    std::string clientMismatchDetail;
};

struct DivergenceReport {
    std::string leafId;
    std::string channelA;
    std::string channelB;
    bool divergent = false;
    std::string earliestCheckpoint;         // 最早出现分歧的检查点
    std::string attribution;                // divergence::*
    std::vector<std::string> attributionFactors;
    std::vector<std::string> policyDeltas;  // 两端实际不同的策略字段
    bool bothClientDecisionsExplained = false;
};

}  // namespace certattrib
