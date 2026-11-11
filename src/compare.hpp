#ifndef MEMTRIAGE_COMPARE_HPP
#define MEMTRIAGE_COMPARE_HPP

#include <string>
#include <vector>

#include "engine.hpp"

namespace memtriage {

struct RankFlip {
    std::string defectIdA;
    std::string defectIdB;
    int baselineOrder;  // -1: A 在 B 前；1: B 在 A 前
    int currentOrder;
};

struct ComparisonEntry {
    std::string defectId;
    int baselineRank = 0;   // 从 1 开始；0 表示不存在
    int currentRank = 0;
    std::int64_t baselineUnmitigated = 0;
    std::int64_t currentUnmitigated = 0;
    double baselineRisk = 0.0;
    double currentRisk = 0.0;
    double baselineConfidence = 0.0;
    double currentConfidence = 0.0;
    bool isNew = false;
    bool isRemoved = false;
    std::vector<std::string> reasons; // 机器可审计的变化归因
};

struct ComparisonReport {
    std::vector<ComparisonEntry> entries; // 按当前排名排序
    std::vector<RankFlip> rankFlips;
};

ComparisonReport compareAssessments(const std::vector<Assessment>& baseline,
                                    const std::vector<Assessment>& current);

}  // namespace memtriage

#endif
