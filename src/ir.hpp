// SEC-VULN-018 报文长度整数范围审计 — IR 定义
// 输入为带源码位置的结构化指令(定宽整数/常量/分支/循环/调用/分配/读取)。
#pragma once

#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <vector>

namespace sec018 {

// 区间端点统一使用 __int128, 同时容纳 u64 全域与 s64 全域。
using BigInt = __int128_t;

std::string to_str(BigInt v);
std::optional<BigInt> parse_int(const std::string& s);

struct SrcLoc {
  int line = 0;
  int col = 0;
};
std::string loc_str(const SrcLoc& l);

// 定宽整数类型: 保留有符号/无符号语义。
struct IntType {
  bool is_signed = false;
  int bits = 32;
  BigInt tmin() const;
  BigInt tmax() const;
  std::string name() const;
  bool contains(BigInt v) const { return v >= tmin() && v <= tmax(); }
};
bool parse_type(const std::string& s, IntType& out);

struct Interval {
  BigInt lo = 0;
  BigInt hi = 0;
  bool contains(const Interval& o) const { return lo <= o.lo && o.hi <= hi; }
  bool singleton() const { return lo == hi; }
  bool operator==(const Interval& o) const { return lo == o.lo && hi == o.hi; }
};
std::string iv_str(const Interval& iv);

enum class Op {
  Param, Const, Move, Binop, Trunc, Zext, Sext, Cmp,
  Check, Br, Jmp, Label, Call, Alloc, Read, Ret
};

struct Instr {
  Op op = Op::Label;
  int id = -1;                 // 函数内序号
  SrcLoc loc;
  std::string dst;             // 定义的变量(若有)
  IntType type;                // param/const/binop/cast/call 结果类型
  std::vector<std::string> args;   // 操作数变量
  std::string binop;           // add/sub/mul/shl
  std::string cmpop;           // eq/ne/slt/sle/sgt/sge/ult/ule/ugt/uge
  BigInt imm = 0;              // const 立即数
  std::string label, label2;   // br 真/假目标, jmp 目标, label 名称
  std::string callee;          // call 目标函数
  std::string text;            // 原始文本(报告用)
};

struct Function {
  std::string name;
  SrcLoc loc;
  std::vector<std::string> param_names;
  std::vector<IntType> param_types;
  std::vector<Instr> instrs;
  const Instr* def_of(const std::string& var) const;
};

struct Constraint {             // summary 中的范围约束: ret 或第 idx 个实参
  int arg_index = -1;           // -1 表示返回值
  std::string op;               // <= < >= > ==
  BigInt value = 0;
};

struct Summary {
  std::string fname;
  std::vector<Constraint> requires;  // 调用方必须满足的前提
  std::vector<Constraint> ensures;   // 返回值保证
};

struct Program {
  BigInt max_len = 65535;       // policy max_len, 长度有效区间上界
  std::vector<Function> funcs;
  std::map<std::string, Summary> summaries;
  const Function* find_func(const std::string& name) const;
};

// 解析整个 IR 文本; 出错时返回 false 并填充 err(带行号)。
bool parse_program(const std::string& src, Program& out, std::string& err);

}  // namespace sec018
