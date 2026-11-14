// 区间抽象解释引擎: 有符号/无符号语义、分支路径精化、
// 循环 widening 收敛、调用上下文敏感分析、三类缺陷模式识别。
#pragma once

#include <map>
#include <set>

#include "ir.hpp"

namespace sec018 {

// 风险标注: 附着在值上, 记录"在哪条指令失去了长度保护"。
struct Risk {
  std::string code;    // OVERFLOW / NARROW / CALL_NO_SUMMARY / WIDENED / RECURSION
  int instr_id = -1;
  SrcLoc loc;
  std::string detail;
};

struct VInfo {
  Interval iv{0, 0};
  IntType type;
  std::vector<Risk> risks;
};

struct Finding {
  std::string kind;            // alloc / read / call
  std::string func;            // 所在函数
  int instr_id = -1;
  SrcLoc loc;
  std::string verdict;         // safe / violation / unprotected(待确认)
  std::string target;          // 操作描述
  std::vector<std::string> evidence;   // 判定依据
  // 首个无法保证长度有效的指令
  bool has_first = false;
  int first_id = -1;
  SrcLoc first_loc;
  std::string first_text;
  std::string first_reason;
  std::vector<std::string> callsite_chain;  // 跨函数调用点上下文
};

struct AnalysisResult {
  std::vector<Finding> findings;             // 按发现顺序
  std::vector<std::string> convergence_notes; // 收敛规则触发记录
  std::string entry_desc;
};

class Analyzer {
 public:
  explicit Analyzer(const Program& prog) : prog_(prog) {}
  AnalysisResult run();

  // 供测试直接使用: 分析单个函数(作为根, 参数取类型全域)。
  AnalysisResult analyze_root(const std::string& fname);

 private:
  const Program& prog_;
  AnalysisResult result_;
  std::vector<std::string> call_stack_;       // 递归检测
  std::map<std::string, Finding> findings_map_;  // 按调用上下文去重, 末次(最宽)覆盖
  std::map<int, bool> narrow_checked_;        // 结果被检查过的 trunc 指令
  std::map<std::string, std::string> buf_srcvar_;  // 缓冲区 -> 分配大小变量名
  std::set<std::string> convergence_set_;     // 收敛记录去重
  int depth_ = 0;
  static constexpr int kMaxDepth = 8;         // 调用/递归深度上限
  static constexpr int kWidenAfter = 4;       // 每块精确迭代上限, 之后 widening
  static constexpr int kMaxVisits = 32;       // 每块绝对迭代上限

  // 处理 call 指令: 摘要校验 / 上下文敏感内联分析 / 缺摘要兜底。
  void handle_call(const Function& f, const Instr& in,
                   std::map<std::string, VInfo>& st,
                   std::map<int, VInfo>& def_values,
                   const std::vector<std::string>& callsite_chain);

  // 将 findings_map_ 汇总进 result_.findings 并排序。
  void finalize();

  // 分析函数; arg_ivs 为实参区间(空 = 类型全域)。返回返回值区间(无返回值为 nullopt)。
  std::optional<Interval> analyze_func(
      const Function& f, const std::vector<Interval>* arg_ivs,
      const std::vector<std::string>& callsite_chain);

  // 约束精化: 对 var 施加 op value; 失败(不可行路径)返回 false。
  static bool refine(VInfo& v, const std::string& op, BigInt value);
  static std::string negate(const std::string& op);

  // 在状态上应用 cmp 派生的分支条件(cond 为 cmp 结果)。
  // 返回 false 表示该路径不可行(out 不可用)。
  bool apply_cond(const Function& f, const std::map<std::string, VInfo>& st,
                  const std::string& cond_var, bool is_true,
                  std::map<std::string, VInfo>& out);

  // 计算首个无法保证长度有效的指令。
  void compute_first(const Function& f, const std::string& len_var,
                     const VInfo& len_info,
                     const std::map<int, VInfo>& def_values,
                     Finding& out);

  // 检查 alloc/read 长度有效性并生成 finding。
  // buf_size_var 非空时为分配大小变量的名字(用于同变量必然不越界判定)。
  void check_length_use(const Function& f, const Instr& in, const std::string& kind,
                        const std::string& len_var, const Interval* buf_size,
                        const std::string* buf_size_var,
                        const std::map<std::string, VInfo>& st,
                        const std::map<int, VInfo>& def_values,
                        const std::vector<std::string>& callsite_chain);

  bool valid_interval(const Interval& iv) const {
    return iv.lo >= 0 && iv.hi <= prog_.max_len;
  }
};

// 渲染文本报告。
std::string render_report(const Program& prog, const AnalysisResult& res,
                          const std::string& input_name);
// 渲染补丁前后判定差异。
std::string render_diff(const Program& p1, const AnalysisResult& r1,
                        const Program& p2, const AnalysisResult& r2,
                        const std::string& n1, const std::string& n2);

}  // namespace sec018
