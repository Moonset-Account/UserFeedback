// SEC-VULN-018 原生测试: 直接驱动解析器与分析引擎, 覆盖四个验收用例、
// 三类缺陷模式、循环/递归收敛规则、补丁前后差异与解析器健壮性。
// 用法: test_analyzer [cases目录]   (默认 tests/cases)
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "../src/analyze.hpp"

namespace {

int g_pass = 0;
int g_fail = 0;

void check(bool cond, const std::string& name, const std::string& detail = "") {
  if (cond) {
    ++g_pass;
    std::cout << "PASS " << name << "\n";
  } else {
    ++g_fail;
    std::cout << "FAIL " << name << (detail.empty() ? "" : " -- " + detail) << "\n";
  }
}

std::string read_file(const std::string& path, bool& ok) {
  std::ifstream in(path);
  if (!in) {
    ok = false;
    return "";
  }
  std::ostringstream ss;
  ss << in.rdbuf();
  ok = true;
  return ss.str();
}

sec018::AnalysisResult analyze_src(const std::string& src, sec018::Program& prog) {
  std::string err;
  if (!sec018::parse_program(src, prog, err)) {
    std::cout << "FAIL 解析失败: " << err << "\n";
    ++g_fail;
    return sec018::AnalysisResult{};
  }
  sec018::Analyzer an(prog);
  return an.run();
}

const sec018::Finding* find_op(const sec018::AnalysisResult& r,
                               const std::string& kind, int line,
                               size_t chain_len = 0) {
  for (const auto& f : r.findings)
    if (f.kind == kind && f.loc.line == line && f.callsite_chain.size() == chain_len)
      return &f;
  return nullptr;
}

size_t count_verdict(const sec018::AnalysisResult& r, const std::string& v) {
  size_t n = 0;
  for (const auto& f : r.findings)
    if (f.verdict == v) ++n;
  return n;
}

bool any_note(const sec018::AnalysisResult& r, const std::string& kw) {
  for (const auto& n : r.convergence_notes)
    if (n.find(kw) != std::string::npos) return true;
  return false;
}

bool any_evidence(const sec018::Finding& f, const std::string& kw) {
  for (const auto& e : f.evidence)
    if (e.find(kw) != std::string::npos) return true;
  return f.first_reason.find(kw) != std::string::npos;
}

std::string g_dir = "tests/cases";

std::string load(const std::string& name) {
  bool ok = false;
  std::string s = read_file(g_dir + "/" + name, ok);
  if (!ok) {
    std::cout << "FAIL 无法读取用例文件: " << name << "\n";
    ++g_fail;
  }
  return s;
}

// 验收用例1: 两条分支均满足范围约束。
void test_case1() {
  sec018::Program prog;
  auto r = analyze_src(load("case1_both_branches_safe.ir"), prog);
  check(r.findings.size() == 4, "case1.操作数=4",
        "实际=" + std::to_string(r.findings.size()));
  check(count_verdict(r, "safe") == 4, "case1.全部安全");
  check(count_verdict(r, "unprotected") == 0 && count_verdict(r, "violation") == 0,
        "case1.无待确认/违规");
}

// 验收用例2: 仅一条分支缺少范围保证。
void test_case2() {
  sec018::Program prog;
  auto r = analyze_src(load("case2_one_branch_unprotected.ir"), prog);
  const auto* small_alloc = find_op(r, "alloc", 7);
  const auto* big_alloc = find_op(r, "alloc", 11);
  const auto* big_read = find_op(r, "read", 12);
  check(small_alloc && small_alloc->verdict == "safe", "case2.then分支alloc安全");
  check(big_alloc && big_alloc->verdict == "unprotected", "case2.else分支alloc待确认");
  check(big_read && big_read->verdict == "unprotected", "case2.else分支read待确认");
  check(big_alloc && big_alloc->has_first && big_alloc->first_loc.line == 2,
        "case2.首个无法保证指令=参数定义@2:1",
        big_alloc && big_alloc->has_first
            ? "实际@" + sec018::loc_str(big_alloc->first_loc)
            : "无");
}

// 验收用例3: 不同调用参数受独立约束。
void test_case3() {
  sec018::Program prog;
  auto r = analyze_src(load("case3_callsite_independent.ir"), prog);
  // handle 内 alloc @ 13:1 应出现两次: 调用点@7:1(安全)与调用点@8:1(待确认)。
  const sec018::Finding *safe_site = nullptr, *unsafe_site = nullptr;
  for (const auto& f : r.findings) {
    if (f.kind == "alloc" && f.loc.line == 13 && f.callsite_chain.size() == 1) {
      if (f.callsite_chain[0].find("7:1") != std::string::npos) safe_site = &f;
      if (f.callsite_chain[0].find("8:1") != std::string::npos) unsafe_site = &f;
    }
  }
  check(safe_site && safe_site->verdict == "safe", "case3.受约束调用点安全");
  check(unsafe_site && unsafe_site->verdict == "unprotected",
        "case3.未受约束调用点待确认");
  check(unsafe_site && unsafe_site->has_first && unsafe_site->first_loc.line == 12,
        "case3.首个无法保证指令=被调函数参数@12:1",
        unsafe_site && unsafe_site->has_first
            ? "实际@" + sec018::loc_str(unsafe_site->first_loc)
            : "无");
}

// 验收用例4: 缺少函数摘要。
void test_case4() {
  sec018::Program prog;
  auto r = analyze_src(load("case4_missing_summary.ir"), prog);
  const auto* call = find_op(r, "call", 6);
  check(call && call->verdict == "unprotected", "case4.无摘要调用待确认");
  check(call && any_evidence(*call, "摘要"), "case4.依据提及缺少函数摘要");
  const auto* alloc = find_op(r, "alloc", 7);
  check(alloc && alloc->verdict == "unprotected", "case4.下游alloc待确认");
  check(alloc && alloc->has_first && alloc->first_loc.line == 6,
        "case4.首个无法保证指令=调用@6:1",
        alloc && alloc->has_first ? "实际@" + sec018::loc_str(alloc->first_loc) : "无");
}

// 缺陷模式: 运算后才检查范围。
void test_case5() {
  sec018::Program prog;
  auto r = analyze_src(load("case5_overflow_check_after_arith.ir"), prog);
  const auto* alloc = find_op(r, "alloc", 13);
  check(alloc && alloc->verdict == "unprotected", "case5.alloc待确认");
  check(alloc && any_evidence(*alloc, "OVERFLOW"), "case5.依据含OVERFLOW");
  check(alloc && alloc->has_first && alloc->first_loc.line == 9,
        "case5.首个无法保证指令=加法@9:1",
        alloc && alloc->has_first ? "实际@" + sec018::loc_str(alloc->first_loc) : "无");
}

// 缺陷模式: 先缩窄位宽再校验。
void test_case6() {
  sec018::Program prog;
  auto r = analyze_src(load("case6_narrow_before_check.ir"), prog);
  const auto* alloc = find_op(r, "alloc", 7);
  check(alloc && alloc->verdict == "unprotected", "case6.alloc待确认");
  check(alloc && any_evidence(*alloc, "NARROW"), "case6.依据含NARROW");
  check(alloc && alloc->has_first && alloc->first_loc.line == 3,
        "case6.首个无法保证指令=截断@3:1",
        alloc && alloc->has_first ? "实际@" + sec018::loc_str(alloc->first_loc) : "无");
}

// 循环收敛规则。
void test_case7() {
  sec018::Program prog;
  auto r = analyze_src(load("case7_loop_widening.ir"), prog);  // 必须有限步返回
  const auto* alloc = find_op(r, "alloc", 14);
  check(alloc && alloc->verdict == "unprotected", "case7.循环后alloc待确认");
  check(any_note(r, "widening"), "case7.记录widening收敛");
}

// 递归收敛规则。
void test_case8() {
  sec018::Program prog;
  auto r = analyze_src(load("case8_recursion.ir"), prog);  // 必须有限步返回
  const auto* alloc = find_op(r, "alloc", 4);
  check(alloc && alloc->verdict == "unprotected", "case8.递归返回alloc待确认");
  check(any_note(r, "递归"), "case8.记录递归收敛规则");
}

// 读取越界判定为违规。
void test_case9() {
  sec018::Program prog;
  auto r = analyze_src(load("case9_read_violation.ir"), prog);
  const auto* read = find_op(r, "read", 8);
  check(read && read->verdict == "violation", "case9.read违规");
  const auto* alloc = find_op(r, "alloc", 7);
  check(alloc && alloc->verdict == "safe", "case9.alloc本身安全");
}

// 函数摘要正面对照。
void test_case11() {
  sec018::Program prog;
  auto r = analyze_src(load("case11_summary_ok.ir"), prog);
  check(count_verdict(r, "unprotected") == 0 && count_verdict(r, "violation") == 0,
        "case11.有摘要且满足前提时无待确认/违规");
  const auto* alloc = find_op(r, "alloc", 7);
  check(alloc && alloc->verdict == "safe", "case11.alloc安全");
}

// 补丁前后判定差异。
void test_diff() {
  sec018::Program p1, p2;
  auto r1 = analyze_src(load("case10_patch_before.ir"), p1);
  auto r2 = analyze_src(load("case10_patch_after.ir"), p2);
  check(count_verdict(r1, "unprotected") == 2, "diff.补丁前2个待确认",
        "实际=" + std::to_string(count_verdict(r1, "unprotected")));
  check(count_verdict(r2, "unprotected") == 0, "diff.补丁后0个待确认");
  std::string diff = sec018::render_diff(p1, r1, p2, r2, "before", "after");
  check(diff.find("待确认 -> 安全") != std::string::npos,
        "diff.报告含「待确认 -> 安全」");
  check(diff.find("[相同]") != std::string::npos, "diff.报告含[相同]项");
}

// 解析器健壮性。
void test_parser_errors() {
  sec018::Program prog;
  std::string err;
  check(!sec018::parse_program("func f\n  const c u8 300 @ 1:1\nend\n", prog, err),
        "parse.常量超类型范围被拒绝");
  check(!sec018::parse_program("func f\n  binop a u32 div x y @ 1:1\nend\n", prog, err),
        "parse.非法运算被拒绝");
  check(!sec018::parse_program("func f\n  const c u8 1\nend\n", prog, err),
        "parse.缺少位置标注被拒绝");
  check(sec018::parse_program("policy max_len 10\nfunc f @ 1:1\n  ret _ @ 2:1\nend\n",
                              prog, err),
        "parse.合法程序被接受");
}

// 有符号语义: 有符号下界为负时长度无效, 检查下界后有效。
void test_signed() {
  const char* src =
      "policy max_len 100\n"
      "func main @ 1:1\n"
      "  param x s32 @ 2:1\n"
      "  const c0 s32 0 @ 3:1\n"
      "  const c10 s32 10 @ 4:1\n"
      "  cmp lo x sge c0 @ 5:1\n"
      "  check lo @ 6:1\n"
      "  cmp hi x sle c10 @ 7:1\n"
      "  check hi @ 8:1\n"
      "  alloc buf x @ 9:1\n"
      "  ret _ @ 10:1\n"
      "end\n";
  sec018::Program prog;
  auto r = analyze_src(src, prog);
  const auto* alloc = find_op(r, "alloc", 9);
  check(alloc && alloc->verdict == "safe", "signed.符号检查完备后安全");

  const char* src2 =
      "policy max_len 100\n"
      "func main @ 1:1\n"
      "  param x s32 @ 2:1\n"
      "  const c10 s32 10 @ 3:1\n"
      "  cmp hi x sle c10 @ 4:1\n"
      "  check hi @ 5:1\n"
      "  alloc buf x @ 6:1\n"
      "  ret _ @ 7:1\n"
      "end\n";
  sec018::Program prog2;
  auto r2 = analyze_src(src2, prog2);
  const auto* a2 = find_op(r2, "alloc", 6);
  check(a2 && a2->verdict == "unprotected", "signed.缺下界检查时待确认(负值)");
}

// u64 全域区间与 to_str 大数输出。
void test_u64() {
  sec018::IntType u64{false, 64};
  check(sec018::to_str(u64.tmax()) == "18446744073709551615", "u64.最大值输出正确");
  sec018::IntType s64{true, 64};
  check(sec018::to_str(s64.tmin()) == "-9223372036854775808", "s64.最小值输出正确");
}

}  // namespace

int main(int argc, char** argv) {
  if (argc > 1) g_dir = argv[1];
  test_case1();
  test_case2();
  test_case3();
  test_case4();
  test_case5();
  test_case6();
  test_case7();
  test_case8();
  test_case9();
  test_case11();
  test_diff();
  test_parser_errors();
  test_signed();
  test_u64();
  std::cout << "== 原生测试汇总: 通过 " << g_pass << ", 失败 " << g_fail << " ==\n";
  return g_fail == 0 && g_pass > 0 ? 0 : 1;
}
