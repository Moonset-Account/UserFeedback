// IR 解析与基础工具实现。
#include "ir.hpp"

#include <cctype>
#include <sstream>

namespace sec018 {

std::string to_str(BigInt v) {
  if (v == 0) return "0";
  bool neg = v < 0;
  unsigned __int128 u = neg ? (unsigned __int128)(-(v + 1)) + 1 : (unsigned __int128)v;
  std::string s;
  while (u > 0) {
    s.push_back(char('0' + (u % 10)));
    u /= 10;
  }
  if (neg) s.push_back('-');
  return std::string(s.rbegin(), s.rend());
}

std::optional<BigInt> parse_int(const std::string& s) {
  if (s.empty()) return std::nullopt;
  size_t i = 0;
  bool neg = false;
  if (s[i] == '-' || s[i] == '+') { neg = s[i] == '-'; ++i; }
  if (i >= s.size()) return std::nullopt;
  int base = 10;
  if (i + 2 <= s.size() && s[i] == '0' && (s[i + 1] == 'x' || s[i + 1] == 'X')) {
    base = 16; i += 2;
  }
  if (i >= s.size()) return std::nullopt;
  BigInt v = 0;
  for (; i < s.size(); ++i) {
    char c = s[i];
    int d = -1;
    if (c >= '0' && c <= '9') d = c - '0';
    else if (base == 16 && c >= 'a' && c <= 'f') d = c - 'a' + 10;
    else if (base == 16 && c >= 'A' && c <= 'F') d = c - 'A' + 10;
    else return std::nullopt;
    if (d >= base) return std::nullopt;
    v = v * base + d;
  }
  return neg ? -v : v;
}

std::string loc_str(const SrcLoc& l) {
  return std::to_string(l.line) + ":" + std::to_string(l.col);
}

BigInt IntType::tmin() const {
  if (is_signed) return -(BigInt(1) << (bits - 1));
  return 0;
}
BigInt IntType::tmax() const {
  if (is_signed) return (BigInt(1) << (bits - 1)) - 1;
  return (BigInt(1) << bits) - 1;
}
std::string IntType::name() const {
  return std::string(is_signed ? "s" : "u") + std::to_string(bits);
}

bool parse_type(const std::string& s, IntType& out) {
  if (s.size() < 2) return false;
  bool sign;
  if (s[0] == 'u') sign = false;
  else if (s[0] == 's') sign = true;
  else return false;
  int bits = 0;
  for (size_t i = 1; i < s.size(); ++i) {
    if (!std::isdigit((unsigned char)s[i])) return false;
    bits = bits * 10 + (s[i] - '0');
  }
  if (bits != 8 && bits != 16 && bits != 32 && bits != 64) return false;
  out.is_signed = sign;
  out.bits = bits;
  return true;
}

std::string iv_str(const Interval& iv) {
  return "[" + to_str(iv.lo) + ", " + to_str(iv.hi) + "]";
}

const Instr* Function::def_of(const std::string& var) const {
  for (const auto& in : instrs) {
    if (in.dst == var) return &in;
  }
  return nullptr;
}

const Function* Program::find_func(const std::string& name) const {
  for (const auto& f : funcs)
    if (f.name == name) return &f;
  return nullptr;
}

// ---------------- 解析器 ----------------

namespace {

std::vector<std::string> tokenize(const std::string& line) {
  std::vector<std::string> toks;
  std::string cur;
  bool in_tok = false;
  for (char c : line) {
    if (c == '#') break;  // 注释
    if (std::isspace((unsigned char)c)) {
      if (in_tok) { toks.push_back(cur); cur.clear(); in_tok = false; }
    } else {
      cur.push_back(c);
      in_tok = true;
    }
  }
  if (in_tok) toks.push_back(cur);
  return toks;
}

// 解析结尾的 "@ L:C"; 成功时移除最后两个 token。
bool take_loc(std::vector<std::string>& toks, SrcLoc& loc) {
  if (toks.size() < 2) return false;
  if (toks[toks.size() - 2] != "@") return false;
  const std::string& lc = toks.back();
  auto colon = lc.find(':');
  if (colon == std::string::npos) return false;
  auto l = parse_int(lc.substr(0, colon));
  auto c = parse_int(lc.substr(colon + 1));
  if (!l || !c) return false;
  loc.line = (int)*l;
  loc.col = (int)*c;
  toks.resize(toks.size() - 2);
  return true;
}

bool parse_constraint(const std::vector<std::string>& toks, size_t pos, bool ret_ok,
                      Constraint& out, std::string& err) {
  // requires: <argidx> <op> <int>   ensures: ret <op> <int>
  if (pos >= toks.size()) { err = "约束缺失"; return false; }
  size_t p = pos;
  if (toks[p] == "ret") {
    if (!ret_ok) { err = "requires 不支持 ret"; return false; }
    out.arg_index = -1;
    ++p;
  } else {
    auto idx = parse_int(toks[p]);
    if (!idx || *idx < 0) { err = "非法参数下标: " + toks[p]; return false; }
    out.arg_index = (int)*idx;
    ++p;
  }
  if (p + 1 >= toks.size()) { err = "约束不完整"; return false; }
  const std::string& op = toks[p];
  if (op != "<=" && op != "<" && op != ">=" && op != ">" && op != "==") {
    err = "非法约束运算符: " + op;
    return false;
  }
  auto v = parse_int(toks[p + 1]);
  if (!v) { err = "非法约束常量: " + toks[p + 1]; return false; }
  out.op = op;
  out.value = *v;
  return true;
}

}  // namespace

bool parse_program(const std::string& src, Program& out, std::string& err) {
  std::istringstream in(src);
  std::string line;
  Function* cur_func = nullptr;
  Summary* cur_sum = nullptr;
  int lineno = 0;
  while (std::getline(in, line)) {
    ++lineno;
    auto toks = tokenize(line);
    if (toks.empty()) continue;
    auto fail = [&](const std::string& msg) {
      err = "第 " + std::to_string(lineno) + " 行: " + msg;
      return false;
    };
    const std::string& head = toks[0];

    if (cur_sum) {
      if (head == "end") { cur_sum = nullptr; continue; }
      Constraint c;
      if (head == "requires") {
        if (!parse_constraint(toks, 1, false, c, err)) return fail(err);
        cur_sum->requires.push_back(c);
      } else if (head == "ensures") {
        if (!parse_constraint(toks, 1, true, c, err)) return fail(err);
        cur_sum->ensures.push_back(c);
      } else {
        return fail("summary 内非法指令: " + head);
      }
      continue;
    }

    if (!cur_func) {
      if (head == "policy") {
        if (toks.size() != 3 || toks[1] != "max_len")
          return fail("policy 仅支持: policy max_len <非负整数>");
        auto v = parse_int(toks[2]);
        if (!v || *v < 0) return fail("非法 max_len: " + toks[2]);
        out.max_len = *v;
      } else if (head == "func") {
        if (toks.size() < 2) return fail("func 缺少名称");
        SrcLoc loc;
        auto t = toks;
        t.erase(t.begin());
        std::string name = t[0];
        t.erase(t.begin());
        if (!t.empty() && !take_loc(t, loc)) return fail("func 位置标注非法");
        if (!t.empty()) return fail("func 存在多余参数");
        Function f;
        f.name = name;
        f.loc = loc;
        out.funcs.push_back(f);
        cur_func = &out.funcs.back();
      } else if (head == "summary") {
        if (toks.size() != 2) return fail("summary 缺少函数名");
        Summary s;
        s.fname = toks[1];
        out.summaries[toks[1]] = s;
        cur_sum = &out.summaries[toks[1]];
      } else {
        return fail("顶层非法指令: " + head);
      }
      continue;
    }

    // 函数体内
    if (head == "end") { cur_func = nullptr; continue; }
    Instr in;
    in.id = (int)cur_func->instrs.size();
    in.text = line;
    auto t = toks;
    t.erase(t.begin());  // 去掉 op 头
    if (head != "label") {
      if (!take_loc(t, in.loc)) return fail(head + " 缺少合法的 @ 行:列 标注");
    }
    auto need = [&](size_t n) {
      if (t.size() != n) { err = head + " 需要 " + std::to_string(n) + " 个参数"; }
      return t.size() == n;
    };

    if (head == "param") {
      if (!need(2)) return fail(err);
      if (!parse_type(t[1], in.type)) return fail("非法类型: " + t[1]);
      in.op = Op::Param;
      in.dst = t[0];
      cur_func->param_names.push_back(t[0]);
      cur_func->param_types.push_back(in.type);
    } else if (head == "const") {
      if (!need(3)) return fail(err);
      if (!parse_type(t[1], in.type)) return fail("非法类型: " + t[1]);
      auto v = parse_int(t[2]);
      if (!v) return fail("非法常量: " + t[2]);
      if (!in.type.contains(*v)) return fail("常量超出类型范围: " + t[2]);
      in.op = Op::Const;
      in.dst = t[0];
      in.imm = *v;
    } else if (head == "move") {
      if (!need(2)) return fail(err);
      in.op = Op::Move;
      in.dst = t[0];
      in.args = {t[1]};
    } else if (head == "binop") {
      if (!need(5)) return fail(err);
      if (!parse_type(t[1], in.type)) return fail("非法类型: " + t[1]);
      if (t[2] != "add" && t[2] != "sub" && t[2] != "mul" && t[2] != "shl")
        return fail("非法运算: " + t[2]);
      in.op = Op::Binop;
      in.dst = t[0];
      in.binop = t[2];
      in.args = {t[3], t[4]};
    } else if (head == "trunc" || head == "zext" || head == "sext") {
      if (!need(3)) return fail(err);
      if (!parse_type(t[1], in.type)) return fail("非法类型: " + t[1]);
      in.op = head == "trunc" ? Op::Trunc : (head == "zext" ? Op::Zext : Op::Sext);
      in.dst = t[0];
      in.args = {t[2]};
    } else if (head == "cmp") {
      if (!need(4)) return fail(err);
      static const char* kOps[] = {"eq","ne","slt","sle","sgt","sge","ult","ule","ugt","uge"};
      bool ok = false;
      for (auto* o : kOps) ok = ok || t[2] == o;
      if (!ok) return fail("非法比较: " + t[2]);
      in.op = Op::Cmp;
      in.dst = t[0];
      in.cmpop = t[2];
      in.args = {t[1], t[3]};
      in.type = IntType{false, 8};
    } else if (head == "check") {
      if (!need(1)) return fail(err);
      in.op = Op::Check;
      in.args = {t[0]};
    } else if (head == "br") {
      if (!need(3)) return fail(err);
      in.op = Op::Br;
      in.args = {t[0]};
      in.label = t[1];
      in.label2 = t[2];
    } else if (head == "jmp") {
      if (!need(1)) return fail(err);
      in.op = Op::Jmp;
      in.label = t[0];
    } else if (head == "label") {
      if (t.size() != 1) return fail("label 需要 1 个名称");
      in.op = Op::Label;
      in.label = t[0];
    } else if (head == "call") {
      // call <dst|_> <type|_> <fname> [args...]
      if (t.size() < 3) return fail("call 需要 <dst|_> <type|_> <fname> [args...]");
      in.op = Op::Call;
      in.dst = t[0];
      if (t[1] != "_" && !parse_type(t[1], in.type)) return fail("非法类型: " + t[1]);
      in.callee = t[2];
      in.args.assign(t.begin() + 3, t.end());
    } else if (head == "alloc") {
      if (!need(2)) return fail(err);
      in.op = Op::Alloc;
      in.dst = t[0];
      in.args = {t[1]};
    } else if (head == "read") {
      if (!need(2)) return fail(err);
      in.op = Op::Read;
      in.args = {t[0], t[1]};
    } else if (head == "ret") {
      if (!need(1)) return fail(err);
      in.op = Op::Ret;
      if (t[0] != "_") in.args = {t[0]};
    } else {
      return fail("未知指令: " + head);
    }
    cur_func->instrs.push_back(in);
  }
  if (cur_func) { err = "函数 " + cur_func->name + " 缺少 end"; return false; }
  if (cur_sum) { err = "summary " + cur_sum->fname + " 缺少 end"; return false; }
  return true;
}

}  // namespace sec018
