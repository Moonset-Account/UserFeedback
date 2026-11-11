#ifndef MEMTRIAGE_REPORT_HPP
#define MEMTRIAGE_REPORT_HPP

#include <string>
#include <vector>

#include "compare.hpp"
#include "engine.hpp"

namespace memtriage {

struct ReportOptions {
    std::string inputLabel;   // 当前数据集标签（目录名等）
    std::string baselineLabel;
    bool includeComparison = false;
};

// 人类可读的中文分诊报告。
std::string renderTextReport(const std::vector<Assessment>& results,
                             const ReportOptions& opts,
                             const ComparisonReport* comparison = nullptr);

// 机器可读 JSON 报告（供下游系统消费）。
std::string renderJsonReport(const std::vector<Assessment>& results,
                             const ReportOptions& opts,
                             const ComparisonReport* comparison = nullptr);

}  // namespace memtriage

#endif
