// SPDX-License-Identifier: MIT
// 离线归因引擎：按固定检查点顺序评估候选路径，汇总渠道判定，比对两端分歧。
// 引擎不做任何密码学运算——签名结论完全来自输入中的 signature_evidence 记录。
#pragma once

#include <map>
#include <string>
#include <vector>

#include "certattrib/loader.hpp"
#include "certattrib/model.hpp"

namespace certattrib {

class Engine {
public:
    explicit Engine(const Dataset& ds) : ds_(ds) {}

    // 单个渠道对单张叶证书的完整判定（含全部候选路径的逐检查点结论）。
    ChannelVerdict evaluateChannel(const PolicyFields& policy,
                                   const std::string& leafId) const;

    // 比对两个渠道对同一叶证书的判定，定位最早分歧检查点并归因。
    DivergenceReport compare(const std::string& leafId,
                             const std::string& channelA,
                             const std::string& channelB) const;

    // 数据集中出现过的全部叶证书 id（来自候选路径与客户端判定）。
    std::vector<std::string> allLeaves() const;

    // 检查点序号（用于分歧定位的全局排序）。
    static int checkpointRank(const std::string& checkpoint);

private:
    const Dataset& ds_;

    PathVerdict evaluatePath(const PolicyFields& policy,
                             const CandidatePath& path) const;

    // 在信任锚快照中选择顶端证书对应的锚；返回空串表示不可信，原因写入 reason/detail。
    std::string selectAnchor(const PolicyFields& policy,
                             const CertRecord& top,
                             std::string& reason,
                             std::string& detail) const;

    const SigEvidence* findEvidence(const std::string& childCertId,
                                    const std::string& issuerKeyId) const;
};

}  // namespace certattrib
