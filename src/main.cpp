// CLI 入口: secvuln018_analyzer
//   analyze <file.ir>            输出审计报告
//   diff <before.ir> <after.ir>  输出补丁前后判定差异
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

#include "analyze.hpp"

namespace {

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

int do_analyze(const std::string& path) {
  bool ok = false;
  std::string src = read_file(path, ok);
  if (!ok) {
    std::cerr << "错误: 无法读取输入文件: " << path << "\n";
    return 2;
  }
  sec018::Program prog;
  std::string err;
  if (!sec018::parse_program(src, prog, err)) {
    std::cerr << "解析失败: " << err << "\n";
    return 2;
  }
  sec018::Analyzer an(prog);
  auto res = an.run();
  std::cout << sec018::render_report(prog, res, path);
  return 0;
}

int do_diff(const std::string& p1, const std::string& p2) {
  bool ok1 = false, ok2 = false;
  std::string s1 = read_file(p1, ok1);
  std::string s2 = read_file(p2, ok2);
  if (!ok1 || !ok2) {
    std::cerr << "错误: 无法读取输入文件\n";
    return 2;
  }
  sec018::Program prog1, prog2;
  std::string err;
  if (!sec018::parse_program(s1, prog1, err)) {
    std::cerr << "解析失败(补丁前): " << err << "\n";
    return 2;
  }
  if (!sec018::parse_program(s2, prog2, err)) {
    std::cerr << "解析失败(补丁后): " << err << "\n";
    return 2;
  }
  sec018::Analyzer a1(prog1), a2(prog2);
  auto r1 = a1.run();
  auto r2 = a2.run();
  std::cout << sec018::render_diff(prog1, r1, prog2, r2, p1, p2);
  return 0;
}

}  // namespace

int main(int argc, char** argv) {
  if (argc >= 3 && std::string(argv[1]) == "analyze") return do_analyze(argv[2]);
  if (argc >= 4 && std::string(argv[1]) == "diff") return do_diff(argv[2], argv[3]);
  std::cerr << "用法:\n"
               "  secvuln018_analyzer analyze <file.ir>\n"
               "  secvuln018_analyzer diff <before.ir> <after.ir>\n";
  return 2;
}
