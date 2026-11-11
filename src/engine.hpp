#ifndef MEMTRIAGE_ENGINE_HPP
#define MEMTRIAGE_ENGINE_HPP

#include <map>
#include <string>
#include <vector>

#include "loader.hpp"
#include "model.hpp"

namespace memtriage {

// 评分模型常量（集中定义，报告中同步说明）。
struct ScoringConfig {
    // 置信度扣分
    double penaltyPerUnknownDeployment = 0.20; // 每个部署范围未知的在保发行版
    double penaltyPerUnknownLifecycle = 0.10;  // 每个生命周期未知的在保发行版
    double penaltyPerOpenMitigation = 0.05;    // 每个缺少已核实有效缓解的在保发行版
    double penaltyUnconfirmedEvidence = 0.25;  // 影响证据未经确认
    double confidenceHighThreshold = 0.80;
    double confidenceMediumThreshold = 0.50;
};

const ScoringConfig& defaultConfig();

// 对数据集执行完整分诊，返回按修复顺序排好的评估结果。
std::vector<Assessment> runTriage(const Dataset& ds,
                                  const ScoringConfig& cfg = defaultConfig());

// 以 defect_id 为键的索引，供对比模块使用。
std::map<std::string, const Assessment*> indexById(const std::vector<Assessment>& list);

const char* confidenceBand(double confidence, const ScoringConfig& cfg);

}  // namespace memtriage

#endif
