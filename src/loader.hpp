#ifndef MEMTRIAGE_LOADER_HPP
#define MEMTRIAGE_LOADER_HPP

#include <string>
#include <vector>

#include "model.hpp"

namespace memtriage {

struct Dataset {
    std::vector<DefectRecord> defects;
    std::vector<DeploymentRecord> deployments;
    std::vector<MitigationRecord> mitigations;
};

class LoadError : public std::runtime_error {
public:
    explicit LoadError(const std::string& msg) : std::runtime_error(msg) {}
};

// 从目录读取三个标准文件名；也支持通过字符串直接载入（供单元测试构造内联数据）。
Dataset loadDataset(const std::string& dir);
Dataset parseDataset(const std::string& defectsJson,
                     const std::string& deploymentsJson,
                     const std::string& mitigationsJson);

}  // namespace memtriage

#endif
