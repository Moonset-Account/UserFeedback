// 文本报告渲染: 分析报告与补丁前后判定差异。
#include <map>
#include <sstream>

#include "analyze.hpp"

namespace sec018 {

namespace {

const char* verdict_label(const std::string& v) {
  if (v == "safe") return "安全";
  if (v == "violation") return "违规";
  return "待确认";
}

std::string chain_str(const Finding& fd) {
  std::string s;
  for (const auto& c : fd.callsite_chain) s += "  调用上下文: " + c + "\n";
  return s;
}

}  // namespace

std::string render_report(const Program& prog, const AnalysisResult& res,
                          const std::string& input_name) {
  std::ostringstream os;
  os << "# 报文长度整数范围审计报告\n";
  os << "输入: " << input_name << "\n";
  os << "策略: 有效长度区间 [0, " << to_str(prog.max_len) << "]\n";
  os << "分析入口: " << (res.entry_desc.empty() ? "(无)" : res.entry_desc) << "\n";
  os << "收敛规则: 循环每基本块精确迭代 4 次后 widening 到类型全域(上限 32 次); "
        "调用/递归深度上限 8, 超限或无摘要时输出「待确认」\n\n";

  int n_safe = 0, n_viol = 0, n_unprot = 0;
  const Finding* global_first = nullptr;
  for (const auto& fd : res.findings) {
    if (fd.verdict == "safe") ++n_safe;
    else if (fd.verdict == "violation") ++n_viol;
    else ++n_unprot;
    os << "[" << verdict_label(fd.verdict) << "] " << fd.kind << " 函数 " << fd.func
       << " @ " << loc_str(fd.loc) << ": " << fd.target << "\n";
    os << chain_str(fd);
    for (const auto& ev : fd.evidence) os << "  依据: " << ev << "\n";
    if (fd.has_first) {
      os << "  首个无法保证长度有效的指令: @ " << loc_str(fd.first_loc) << ": "
         << fd.first_text << "\n";
      os << "    原因: " << fd.first_reason << "\n";
      if (!global_first ||
          (fd.first_loc.line < global_first->first_loc.line) ||
          (fd.first_loc.line == global_first->first_loc.line &&
           fd.first_loc.col < global_first->first_loc.col))
        global_first = &fd;
    }
    os << "\n";
  }

  os << "## 汇总\n";
  os << "安全: " << n_safe << ", 违规: " << n_viol << ", 待确认: " << n_unprot << "\n";
  if (global_first) {
    os << "全局首个无法保证长度有效的指令: @ " << loc_str(global_first->first_loc)
       << ": " << global_first->first_text << " (函数 " << global_first->func << ")\n";
    os << "原因: " << global_first->first_reason << "\n";
  } else {
    os << "全局首个无法保证长度有效的指令: 无(所有长度操作均可证明有效)\n";
  }
  if (!res.convergence_notes.empty()) {
    os << "\n## 收敛规则触发记录\n";
    for (const auto& note : res.convergence_notes) os << "- " << note << "\n";
  }
  return os.str();
}

std::string render_diff(const Program& p1, const AnalysisResult& r1,
                        const Program& p2, const AnalysisResult& r2,
                        const std::string& n1, const std::string& n2) {
  (void)p1;
  (void)p2;
  std::ostringstream os;
  os << "# 补丁前后判定差异\n";
  os << "补丁前: " << n1 << "\n";
  os << "补丁后: " << n2 << "\n\n";

  // 按 (函数, 操作类别, 同类序号) 配对, 容忍补丁引起的行号漂移。
  auto key_of = [](const Finding& fd, std::map<std::string, int>& counter) {
    std::string base = fd.func + "|" + fd.kind + "|" +
                       std::to_string(fd.callsite_chain.size());
    int ord = counter[base]++;
    return base + "|" + std::to_string(ord);
  };
  std::map<std::string, int> c1, c2;
  std::map<std::string, const Finding*> m1, m2;
  for (const auto& fd : r1.findings) m1[key_of(fd, c1)] = &fd;
  for (const auto& fd : r2.findings) m2[key_of(fd, c2)] = &fd;

  int changed = 0, same = 0, added = 0, removed = 0;
  for (const auto& [k, f1] : m1) {
    auto it = m2.find(k);
    if (it == m2.end()) {
      os << "[移除] " << f1->func << " " << f1->kind << " @ " << loc_str(f1->loc)
         << ": " << verdict_label(f1->verdict) << " -> (操作已删除)\n";
      ++removed;
      continue;
    }
    const Finding* f2 = it->second;
    if (f1->verdict == f2->verdict) {
      os << "[相同] " << f1->func << " " << f1->kind << " @ " << loc_str(f1->loc)
         << " -> @ " << loc_str(f2->loc) << ": " << verdict_label(f1->verdict) << "\n";
      ++same;
    } else {
      os << "[变化] " << f1->func << " " << f1->kind << " @ " << loc_str(f1->loc)
         << " -> @ " << loc_str(f2->loc) << ": " << verdict_label(f1->verdict)
         << " -> " << verdict_label(f2->verdict) << "\n";
      ++changed;
    }
  }
  for (const auto& [k, f2] : m2) {
    if (m1.count(k)) continue;
    os << "[新增] " << f2->func << " " << f2->kind << " @ " << loc_str(f2->loc)
       << ": " << verdict_label(f2->verdict) << "\n";
    ++added;
  }
  os << "\n汇总: 变化 " << changed << ", 相同 " << same << ", 新增 " << added
     << ", 移除 " << removed << "\n";
  return os.str();
}

}  // namespace sec018
