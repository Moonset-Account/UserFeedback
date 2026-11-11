// SPDX-License-Identifier: MIT
// 输入加载：把结构化 JSON 记录转换为模型对象，并做基本完整性校验。
#pragma once

#include <map>
#include <string>
#include <vector>

#include "certattrib/json.hpp"
#include "certattrib/model.hpp"

namespace certattrib {

struct Dataset {
    std::map<std::string, CertRecord> certs;                       // id -> cert
    std::vector<AnchorRecord> anchors;
    std::map<std::string, std::vector<SigEvidence>> evidence;      // childCertId -> 证据列表
    std::vector<CandidatePath> paths;
    std::map<std::string, PolicyFields> policies;                  // channelId -> 策略
    std::map<std::string, std::vector<ClientDecision>> decisions;  // leafId -> 各渠道判定

    const CertRecord& cert(const std::string& id) const;
    const CandidatePath* findPath(const std::string& pathId) const;
    std::vector<CandidatePath> pathsOfLeaf(const std::string& leafId) const;
};

// 解析整份数据集 JSON；格式错误抛 JsonError / std::runtime_error。
Dataset loadDataset(const JsonPtr& root);
Dataset loadDatasetFile(const std::string& path);

}  // namespace certattrib
