// 区间抽象解释引擎实现。
#include "analyze.hpp"

#include <algorithm>
#include <deque>
#include <map>
#include <set>

namespace sec018 {

namespace {

std::string chain_prefix(const std::vector<std::string>& chain) {
  std::string s;
  for (const auto& c : chain) s += c + " -> ";
  return s;
}

}  // namespace

// ---------------- 约束精化 ----------------

std::string Analyzer::negate(const std::string& op) {
  if (op == "eq") return "ne";
  if (op == "ne") return "eq";
  if (op == "slt") return "sge";
  if (op == "sle") return "sgt";
  if (op == "sgt") return "sle";
  if (op == "sge") return "slt";
  if (op == "ult") return "uge";
  if (op == "ule") return "ugt";
  if (op == "ugt") return "ule";
  return "ult";  // uge
}

bool Analyzer::refine(VInfo& v, const std::string& op, BigInt value) {
  Interval& iv = v.iv;
  if (op == "eq") {
    iv.lo = iv.hi = value;
  } else if (op == "ne") {
    if (iv.singleton() && iv.lo == value) return false;
    if (iv.lo == value) iv.lo = iv.lo + 1;
    else if (iv.hi == value) iv.hi = iv.hi - 1;
  } else if (op == "slt" || op == "ult") {
    iv.hi = std::min(iv.hi, value - 1);
  } else if (op == "sle" || op == "ule") {
    iv.hi = std::min(iv.hi, value);
  } else if (op == "sgt" || op == "ugt") {
    iv.lo = std::max(iv.lo, value + 1);
  } else if (op == "sge" || op == "uge") {
    iv.lo = std::max(iv.lo, value);
  }
  iv.lo = std::max(iv.lo, v.type.tmin());
  iv.hi = std::min(iv.hi, v.type.tmax());
  return iv.lo <= iv.hi;
}

bool Analyzer::apply_cond(const Function& f, const std::map<std::string, VInfo>& st,
                          const std::string& cond_var, bool is_true,
                          std::map<std::string, VInfo>& out) {
  out = st;
  const Instr* def = f.def_of(cond_var);
  if (!def || def->op != Op::Cmp || def->args.size() != 2) return true;
  std::string op = is_true ? def->cmpop : negate(def->cmpop);
  auto it_a = st.find(def->args[0]);
  auto it_b = st.find(def->args[1]);
  if (it_a == st.end() || it_b == st.end()) return true;
  auto swap_op = [](const std::string& o) -> std::string {
    if (o == "slt") return "sgt";
    if (o == "sle") return "sge";
    if (o == "sgt") return "slt";
    if (o == "sge") return "sle";
    if (o == "ult") return "ugt";
    if (o == "ule") return "uge";
    if (o == "ugt") return "ult";
    if (o == "uge") return "ule";
    return o;
  };
  if (it_b->second.iv.singleton()) {
    VInfo refined = it_a->second;
    if (refine(refined, op, it_b->second.iv.lo)) {
      out[def->args[0]] = refined;
      // 先缩窄位宽再校验: 对被截断值施加的检查不能保护原始宽值。
      const Instr* d2 = f.def_of(def->args[0]);
      if (d2 && d2->op == Op::Trunc) narrow_checked_[d2->id] = true;
      return true;
    }
    return false;  // 不可行路径
  }
  if (it_a->second.iv.singleton()) {
    VInfo refined = it_b->second;
    if (refine(refined, swap_op(op), it_a->second.iv.lo)) {
      out[def->args[1]] = refined;
      const Instr* d2 = f.def_of(def->args[1]);
      if (d2 && d2->op == Op::Trunc) narrow_checked_[d2->id] = true;
      return true;
    }
    return false;
  }
  return true;
}

// ---------------- 长度使用检查 ----------------

void Analyzer::compute_first(const Function& f, const std::string& len_var,
                             const VInfo& len_info,
                             const std::map<int, VInfo>& def_values,
                             Finding& out) {
  // 数据流链: 从长度变量回溯到定义指令。
  std::vector<const Instr*> chain;
  std::vector<std::string> chain_vars;
  std::set<std::string> seen;
  std::deque<std::string> work{len_var};
  while (!work.empty()) {
    std::string v = work.front();
    work.pop_front();
    if (!seen.insert(v).second) continue;
    chain_vars.push_back(v);
    const Instr* d = f.def_of(v);
    if (!d) continue;
    chain.push_back(d);
    for (const auto& a : d->args) work.push_back(a);
  }
  // 收集链上风险(含"先缩窄再校验"模式: 链上变量是被检查过的截断的源)。
  std::vector<Risk> risks = len_info.risks;
  for (const Instr* d : chain) {
    auto it = def_values.find(d->id);
    if (it != def_values.end())
      for (const auto& r : it->second.risks) risks.push_back(r);
  }
  for (const auto& v : chain_vars) {
    for (const auto& in : f.instrs) {
      if (in.op == Op::Trunc && !in.args.empty() && in.args[0] == v &&
          narrow_checked_.count(in.id)) {
        risks.push_back(Risk{"NARROW", in.id, in.loc,
                             "截断到 " + in.type.name() +
                                 " 后才做范围检查, 原始宽值未受保护"});
      }
    }
  }
  auto by_pos = [](const Instr* a, const Instr* b) {
    if (a->loc.line != b->loc.line) return a->loc.line < b->loc.line;
    if (a->loc.col != b->loc.col) return a->loc.col < b->loc.col;
    return a->id < b->id;
  };
  if (!risks.empty()) {
    const Risk* best = &risks[0];
    for (const auto& r : risks) {
      if (r.loc.line < best->loc.line ||
          (r.loc.line == best->loc.line && r.loc.col < best->loc.col))
        best = &r;
    }
    out.has_first = true;
    out.first_id = best->instr_id;
    out.first_loc = best->loc;
    const Instr* fi = nullptr;
    for (const Instr* d : chain)
      if (d->id == best->instr_id) fi = d;
    out.first_text = fi ? fi->text : "(函数 " + f.name + " 内循环/递归收敛规则作用点)";
    out.first_reason = best->detail;
    return;
  }
  // 无风险标注: 找链上最早输出区间未落入有效长度范围的指令。
  std::sort(chain.begin(), chain.end(), by_pos);
  for (const Instr* d : chain) {
    auto it = def_values.find(d->id);
    if (it == def_values.end()) continue;
    if (!valid_interval(it->second.iv)) {
      out.has_first = true;
      out.first_id = d->id;
      out.first_loc = d->loc;
      out.first_text = d->text;
      out.first_reason = "该指令输出取值 " + iv_str(it->second.iv) +
                         " 未落入有效长度区间 [0, " + to_str(prog_.max_len) +
                         "], 且到达使用点的路径上缺少有效范围检查";
      return;
    }
  }
}

void Analyzer::check_length_use(const Function& f, const Instr& in,
                                const std::string& kind, const std::string& len_var,
                                const Interval* buf_size,
                                const std::string* buf_size_var,
                                const std::map<std::string, VInfo>& st,
                                const std::map<int, VInfo>& def_values,
                                const std::vector<std::string>& callsite_chain) {
  auto it = st.find(len_var);
  if (it == st.end()) return;
  const VInfo& v = it->second;

  Finding fd;
  fd.kind = kind;
  fd.func = f.name;
  fd.instr_id = in.id;
  fd.loc = in.loc;
  fd.callsite_chain = callsite_chain;
  fd.target = in.text;

  // 收集链上风险。
  std::vector<Risk> risks = v.risks;
  {
    std::set<std::string> seen;
    std::deque<std::string> work{len_var};
    std::vector<std::string> chain_vars;
    while (!work.empty()) {
      std::string x = work.front();
      work.pop_front();
      if (!seen.insert(x).second) continue;
      chain_vars.push_back(x);
      const Instr* d = f.def_of(x);
      if (!d) continue;
      auto dv = def_values.find(d->id);
      if (dv != def_values.end())
        for (const auto& r : dv->second.risks) risks.push_back(r);
      for (const auto& a : d->args) work.push_back(a);
    }
    for (const auto& x : chain_vars) {
      for (const auto& in2 : f.instrs) {
        if (in2.op == Op::Trunc && !in2.args.empty() && in2.args[0] == x &&
            narrow_checked_.count(in2.id))
          risks.push_back(Risk{"NARROW", in2.id, in2.loc,
                               "截断到 " + in2.type.name() + " 后才做范围检查"});
      }
    }
  }

  std::string len_desc = len_var + " ∈ " + iv_str(v.iv) + " (" + v.type.name() + ")";
  bool iv_ok = valid_interval(v.iv);
  bool iv_bad = v.iv.lo > prog_.max_len || v.iv.hi < 0;

  std::string verdict;
  if (!risks.empty()) {
    verdict = "unprotected";
    for (const auto& r : risks)
      fd.evidence.push_back("风险[" + r.code + "] @ " + loc_str(r.loc) + ": " + r.detail);
  } else if (iv_ok) {
    verdict = "safe";
  } else if (iv_bad) {
    verdict = "violation";
  } else {
    verdict = "unprotected";
  }
  fd.evidence.insert(fd.evidence.begin(),
                     "长度 " + len_desc + ", 有效区间为 [0, " + to_str(prog_.max_len) + "]");

  if (kind == "read") {
    // 读取长度与分配大小为同一变量且定义唯一时, 必然不越界。
    bool same_var_fit = false;
    if (buf_size_var && *buf_size_var == len_var) {
      int defs = 0;
      for (const auto& in2 : f.instrs)
        if (in2.dst == len_var) ++defs;
      same_var_fit = defs == 1;
    }
    if (same_var_fit) {
      fd.evidence.push_back("读取长度与分配大小为同一变量, 必然不越界");
    } else if (!buf_size) {
      if (verdict == "safe") verdict = "unprotected";
      fd.evidence.push_back("读取目标缓冲区未知, 无法证明不越界");
    } else {
      fd.evidence.push_back("缓冲区大小 ∈ " + iv_str(*buf_size));
      if (v.iv.lo > buf_size->hi) {
        verdict = "violation";
        fd.evidence.push_back("读取长度下界超过缓冲区上界, 必然越界");
      } else if (v.iv.hi > buf_size->lo) {
        if (verdict == "safe") verdict = "unprotected";
        fd.evidence.push_back("读取长度可能超过缓冲区大小, 无法证明不越界");
      } else {
        fd.evidence.push_back("读取长度上界不超过缓冲区下界, 不越界");
      }
    }
  }
  fd.verdict = verdict;
  if (verdict != "safe")
    compute_first(f, len_var, v, def_values, fd);

  std::string key = chain_prefix(callsite_chain) + f.name + "|" + kind + "|" +
                    std::to_string(in.id);
  findings_map_[key] = fd;
}

// ---------------- 函数级分析 ----------------

namespace {

Interval join_iv(const Interval& a, const Interval& b) {
  return Interval{std::min(a.lo, b.lo), std::max(a.hi, b.hi)};
}

void join_risks(std::vector<Risk>& dst, const std::vector<Risk>& src) {
  for (const auto& r : src) {
    bool dup = false;
    for (const auto& d : dst)
      if (d.code == r.code && d.instr_id == r.instr_id) { dup = true; break; }
    if (!dup) dst.push_back(r);
  }
}

}  // namespace

std::optional<Interval> Analyzer::analyze_func(
    const Function& f, const std::vector<Interval>* arg_ivs,
    const std::vector<std::string>& callsite_chain) {
  const int n = (int)f.instrs.size();
  if (n == 0) return std::nullopt;

  // 基本块划分。
  std::map<std::string, int> label_at;
  for (const auto& in : f.instrs)
    if (in.op == Op::Label) label_at[in.label] = in.id;
  std::set<int> leaders{0};
  for (const auto& in : f.instrs) {
    if (in.op == Op::Br) {
      if (label_at.count(in.label)) leaders.insert(label_at[in.label]);
      if (label_at.count(in.label2)) leaders.insert(label_at[in.label2]);
      if (in.id + 1 < n) leaders.insert(in.id + 1);
    } else if (in.op == Op::Jmp || in.op == Op::Ret) {
      if (in.id + 1 < n) leaders.insert(in.id + 1);
    }
    if (in.op == Op::Jmp && label_at.count(in.label))
      leaders.insert(label_at[in.label]);
  }
  std::vector<int> leader_vec(leaders.begin(), leaders.end());
  auto block_of = [&](int instr_id) {
    int b = 0;
    for (int l : leader_vec) {
      if (l <= instr_id) b = l;
      else break;
    }
    return b;
  };

  std::map<int, std::map<std::string, VInfo>> in_state;   // 块首 -> 汇合状态
  std::map<int, int> visits;
  std::map<int, VInfo> def_values;                        // 指令id -> 定义值(末次)
  std::optional<Interval> ret_iv;
  std::deque<int> worklist{0};
  std::set<int> queued{0};

  // 入口状态: 参数取实参区间或类型全域。
  {
    std::map<std::string, VInfo> entry;
    for (size_t i = 0; i < f.param_names.size(); ++i) {
      VInfo v;
      v.type = f.param_types[i];
      if (arg_ivs && i < arg_ivs->size()) {
        v.iv = (*arg_ivs)[i];
        v.iv.lo = std::max(v.iv.lo, v.type.tmin());
        v.iv.hi = std::min(v.iv.hi, v.type.tmax());
      } else {
        v.iv = Interval{v.type.tmin(), v.type.tmax()};
      }
      entry[f.param_names[i]] = v;
    }
    in_state[0] = entry;
  }

  auto get = [](const std::map<std::string, VInfo>& st, const std::string& v,
                VInfo& out) -> bool {
    auto it = st.find(v);
    if (it == st.end()) return false;
    out = it->second;
    return true;
  };

  while (!worklist.empty()) {
    int bstart = worklist.front();
    worklist.pop_front();
    queued.erase(bstart);
    int& vc = visits[bstart];
    if (vc >= kMaxVisits) {
      std::string note = "函数 " + f.name + " 基本块@" + std::to_string(bstart) +
                         " 达到每块迭代上限 " + std::to_string(kMaxVisits) +
                         ", 停止继续迭代(收敛规则)";
      if (convergence_set_.insert(note).second)
        result_.convergence_notes.push_back(note);
      continue;
    }
    ++vc;
    auto st = in_state[bstart];

    // 顺序执行块内指令。
    int i = bstart;
    std::vector<std::pair<int, std::map<std::string, VInfo>>> edges;
    while (i < n) {
      const Instr& in = f.instrs[i];
      if (i != bstart && leaders.count(i)) break;  // 进入下一块
      bool fallthrough = true;
      switch (in.op) {
        case Op::Label:
          break;
        case Op::Param: {
          // 入口块由 in_state 提供; 非入口块(不可达参数指令)忽略。
          auto it = st.find(in.dst);
          if (it != st.end()) def_values[in.id] = it->second;
          break;
        }
        case Op::Const: {
          VInfo v;
          v.type = in.type;
          v.iv = Interval{in.imm, in.imm};
          st[in.dst] = v;
          def_values[in.id] = v;
          break;
        }
        case Op::Move: {
          VInfo v;
          if (get(st, in.args[0], v)) {
            st[in.dst] = v;
            def_values[in.id] = v;
          }
          break;
        }
        case Op::Binop: {
          VInfo a, b;
          if (!get(st, in.args[0], a) || !get(st, in.args[1], b)) break;
          Interval m{0, 0};
          bool ok = true;
          if (in.binop == "add") {
            m = Interval{a.iv.lo + b.iv.lo, a.iv.hi + b.iv.hi};
          } else if (in.binop == "sub") {
            m = Interval{a.iv.lo - b.iv.hi, a.iv.hi - b.iv.lo};
          } else if (in.binop == "mul") {
            BigInt c1 = a.iv.lo * b.iv.lo, c2 = a.iv.lo * b.iv.hi;
            BigInt c3 = a.iv.hi * b.iv.lo, c4 = a.iv.hi * b.iv.hi;
            m = Interval{std::min({c1, c2, c3, c4}), std::max({c1, c2, c3, c4})};
          } else if (in.binop == "shl") {
            if (b.iv.lo < 0 || b.iv.hi >= in.type.bits) {
              ok = false;
              m = Interval{in.type.tmin(), in.type.tmax()};
            } else {
              BigInt lo_sh = BigInt(1) << (int)b.iv.lo;
              BigInt hi_sh = BigInt(1) << (int)b.iv.hi;
              BigInt c1 = a.iv.lo * lo_sh, c2 = a.iv.lo * hi_sh;
              BigInt c3 = a.iv.hi * lo_sh, c4 = a.iv.hi * hi_sh;
              m = Interval{std::min({c1, c2, c3, c4}), std::max({c1, c2, c3, c4})};
            }
          }
          VInfo r;
          r.type = in.type;
          join_risks(r.risks, a.risks);
          join_risks(r.risks, b.risks);
          bool fits = ok && in.type.contains(m.lo) && in.type.contains(m.hi);
          if (fits) {
            r.iv = m;
          } else {
            r.iv = Interval{in.type.tmin(), in.type.tmax()};
            r.risks.push_back(Risk{
                "OVERFLOW", in.id, in.loc,
                in.type.name() + " " + in.binop + " 的数学结果 ∈ " + iv_str(m) +
                    " 超出类型范围 [" + to_str(in.type.tmin()) + ", " +
                    to_str(in.type.tmax()) + "], 结果回绕; 之后对该值的检查作用于回绕后的值(运算后才检查范围)"});
          }
          st[in.dst] = r;
          def_values[in.id] = r;
          break;
        }
        case Op::Trunc: {
          VInfo s;
          if (!get(st, in.args[0], s)) break;
          VInfo r;
          r.type = in.type;
          r.risks = s.risks;
          if (in.type.contains(s.iv.lo) && in.type.contains(s.iv.hi)) {
            r.iv = s.iv;
          } else {
            r.iv = Interval{in.type.tmin(), in.type.tmax()};
          }
          st[in.dst] = r;
          def_values[in.id] = r;
          break;
        }
        case Op::Zext: {
          VInfo s;
          if (!get(st, in.args[0], s)) break;
          VInfo r;
          r.type = in.type;
          r.risks = s.risks;
          if (!s.type.is_signed) {
            r.iv = s.iv;  // 无符号零扩展保持数值
          } else {
            r.iv = Interval{0, in.type.tmax()};
          }
          st[in.dst] = r;
          def_values[in.id] = r;
          break;
        }
        case Op::Sext: {
          VInfo s;
          if (!get(st, in.args[0], s)) break;
          VInfo r;
          r.type = in.type;
          r.risks = s.risks;
          r.iv = s.iv;  // 符号扩展保持数值
          st[in.dst] = r;
          def_values[in.id] = r;
          break;
        }
        case Op::Cmp: {
          VInfo a, b;
          if (!get(st, in.args[0], a) || !get(st, in.args[1], b)) break;
          // 先求 eq 的判定, ne 直接取反。
          Interval eq_iv{0, 1};
          if (a.iv.singleton() && b.iv.singleton() && a.iv.lo == b.iv.lo)
            eq_iv = Interval{1, 1};
          else if (a.iv.hi < b.iv.lo || b.iv.hi < a.iv.lo)
            eq_iv = Interval{0, 0};
          auto eval = [&](const std::string& op) -> Interval {
            if (op == "eq") return eq_iv;
            if (op == "ne") {
              if (eq_iv.singleton()) return Interval{1 - eq_iv.lo, 1 - eq_iv.lo};
              return Interval{0, 1};
            }
            // 统一归约为 x </<= y; 有符号性已体现在类型取值范围内。
            Interval x = a.iv, y = b.iv;
            bool strict = true;
            if (op == "sgt" || op == "ugt") { std::swap(x, y); strict = true; }
            else if (op == "sle" || op == "ule") { strict = false; }
            else if (op == "sge" || op == "uge") { std::swap(x, y); strict = false; }
            if (strict) {
              if (x.hi < y.lo) return Interval{1, 1};
              if (x.lo >= y.hi) return Interval{0, 0};
              return Interval{0, 1};
            }
            if (x.hi <= y.lo) return Interval{1, 1};
            if (x.lo > y.hi) return Interval{0, 0};
            return Interval{0, 1};
          };
          VInfo r;
          r.type = IntType{false, 8};
          r.iv = eval(in.cmpop);
          st[in.dst] = r;
          def_values[in.id] = r;
          break;        }
        case Op::Check: {
          // 检查失败即拒绝报文: 后续路径假设条件为真。
          std::map<std::string, VInfo> refined;
          if (!apply_cond(f, st, in.args[0], true, refined)) {
            fallthrough = false;  // 不可行路径, 不再产生后继
            break;
          }
          st = refined;
          break;
        }
        case Op::Br: {
          std::map<std::string, VInfo> tst, fst;
          bool tok = apply_cond(f, st, in.args[0], true, tst);
          bool fok = apply_cond(f, st, in.args[0], false, fst);
          if (label_at.count(in.label) && tok)
            edges.emplace_back(label_at[in.label], tst);
          if (label_at.count(in.label2) && fok)
            edges.emplace_back(label_at[in.label2], fst);
          fallthrough = false;
          break;
        }
        case Op::Jmp: {
          if (label_at.count(in.label)) edges.emplace_back(label_at[in.label], st);
          fallthrough = false;
          break;
        }
        case Op::Call: {
          handle_call(f, in, st, def_values, callsite_chain);
          if (!in.dst.empty() && in.dst != "_") {
            auto it = st.find(in.dst);
            if (it != st.end()) def_values[in.id] = it->second;
          }
          break;
        }
        case Op::Alloc: {
          VInfo s;
          if (get(st, in.args[0], s)) {
            VInfo bs;
            bs.type = s.type;
            bs.iv = s.iv;
            st["\1buf:" + in.dst] = bs;
            buf_srcvar_[f.name + "\1" + in.dst] = in.args[0];
            check_length_use(f, in, "alloc", in.args[0], nullptr, nullptr, st,
                             def_values, callsite_chain);
          }
          break;
        }
        case Op::Read: {
          auto bit = st.find("\1buf:" + in.args[0]);
          const Interval* bs = bit == st.end() ? nullptr : &bit->second.iv;
          auto vit = buf_srcvar_.find(f.name + "\1" + in.args[0]);
          const std::string* bsv =
              vit == buf_srcvar_.end() ? nullptr : &vit->second;
          if (st.count(in.args[1]))
            check_length_use(f, in, "read", in.args[1], bs, bsv, st, def_values,
                             callsite_chain);
          break;
        }
        case Op::Ret: {
          if (!in.args.empty()) {
            VInfo v;
            if (get(st, in.args[0], v)) {
              if (ret_iv) ret_iv = join_iv(*ret_iv, v.iv);
              else ret_iv = v.iv;
            }
          }
          fallthrough = false;
          break;
        }
      }
      if (!fallthrough) break;
      ++i;
      if (i < n && leaders.count(i)) {
        edges.emplace_back(i, st);
        break;
      }
      if (i >= n) break;
    }

    // 传播到后继, 汇合 + widening。
    for (auto& [target, es] : edges) {
      int tb = block_of(target);
      auto& cur = in_state[tb];
      bool changed = false;
      if (cur.empty()) {
        cur = es;
        changed = true;
      } else {
        bool widen = visits[tb] >= kWidenAfter;
        for (auto& [name, v] : es) {
          auto it = cur.find(name);
          if (it == cur.end()) {
            cur[name] = v;
            changed = true;
            continue;
          }
          Interval nv = join_iv(it->second.iv, v.iv);
          bool grew = nv.lo < it->second.iv.lo || nv.hi > it->second.iv.hi;
          if (widen && grew) {
            // widening: 直接跳到类型边界, 保证有限步收敛。
            if (nv.lo < it->second.iv.lo) nv.lo = v.type.tmin();
            if (nv.hi > it->second.iv.hi) nv.hi = v.type.tmax();
            std::vector<Risk> wr{Risk{
                "WIDENED", -1, f.loc,
                "循环内变量 " + name +
                    " 经 widening 收敛到类型全域(循环收敛规则: 每块精确迭代 " +
                    std::to_string(kWidenAfter) + " 次后 widening)"}};
            join_risks(it->second.risks, wr);
            std::string note = "函数 " + f.name + ": 变量 " + name +
                               " 触发 widening(循环收敛规则)";
            if (convergence_set_.insert(note).second)
              result_.convergence_notes.push_back(note);
          }
          size_t old_risks = it->second.risks.size();
          join_risks(it->second.risks, v.risks);
          if (grew || it->second.risks.size() != old_risks) changed = true;
          it->second.iv = nv;
        }
      }
      if (changed && visits[tb] < kMaxVisits && !queued.count(tb)) {
        worklist.push_back(tb);
        queued.insert(tb);
      }
    }
  }
  return ret_iv;
}

// ---------------- 调用处理 ----------------

void Analyzer::handle_call(const Function& f, const Instr& in,
                           std::map<std::string, VInfo>& st,
                           std::map<int, VInfo>& /*def_values*/,
                           const std::vector<std::string>& callsite_chain) {
  std::vector<Interval> arg_ivs;
  std::vector<VInfo> arg_vis;
  for (const auto& a : in.args) {
    auto it = st.find(a);
    if (it != st.end()) {
      arg_ivs.push_back(it->second.iv);
      arg_vis.push_back(it->second);
    } else {
      arg_ivs.push_back(Interval{0, 0});
      arg_vis.push_back(VInfo{});
    }
  }
  std::string site = f.name + " 调用点 @ " + loc_str(in.loc);

  auto sit = prog_.summaries.find(in.callee);
  const Function* callee = prog_.find_func(in.callee);

  Interval ret_iv;
  bool has_ret = !in.dst.empty() && in.dst != "_";
  std::vector<Risk> ret_risks;

  if (sit != prog_.summaries.end()) {
    // 有函数摘要: 校验 requires, 应用 ensures。
    const Summary& sum = sit->second;
    std::string verdict = "safe";
    Finding fd;
    fd.kind = "call";
    fd.func = f.name;
    fd.instr_id = in.id;
    fd.loc = in.loc;
    fd.callsite_chain = callsite_chain;
    fd.target = in.text;
    for (const auto& rq : sum.requires) {
      if (rq.arg_index < 0 || rq.arg_index >= (int)arg_ivs.size()) continue;
      const Interval& iv = arg_ivs[rq.arg_index];
      VInfo tmp;
      tmp.type = IntType{true, 64};
      tmp.iv = iv;
      VInfo all = tmp;
      all.iv = Interval{tmp.type.tmin(), tmp.type.tmax()};
      bool satisfied = refine(tmp, rq.op, rq.value) && tmp.iv == iv;
      bool violated = !refine(all, rq.op, rq.value) ||
                      (rq.op == "<=" && iv.lo > rq.value) ||
                      (rq.op == "<" && iv.lo >= rq.value) ||
                      (rq.op == ">=" && iv.hi < rq.value) ||
                      (rq.op == ">" && iv.hi <= rq.value) ||
                      (rq.op == "==" && !(iv.singleton() && iv.lo == rq.value));
      std::string desc = "实参#" + std::to_string(rq.arg_index) + " ∈ " + iv_str(iv) +
                         ", 摘要要求 " + rq.op + " " + to_str(rq.value);
      if (satisfied && !violated) {
        fd.evidence.push_back(desc + ": 满足");
      } else if (violated) {
        fd.evidence.push_back(desc + ": 必然违反");
        verdict = "violation";
      } else {
        fd.evidence.push_back(desc + ": 无法证明满足");
        if (verdict == "safe") verdict = "unprotected";
      }
    }
    fd.verdict = verdict;
    if (verdict != "safe") {
      fd.has_first = true;
      fd.first_id = in.id;
      fd.first_loc = in.loc;
      fd.first_text = in.text;
      fd.first_reason = verdict == "violation"
                            ? "实参必然违反被调函数的摘要前提"
                            : "无法证明实参满足被调函数的摘要前提";
    }
    std::string key = chain_prefix(callsite_chain) + f.name + "|call|" +
                      std::to_string(in.id);
    findings_map_[key] = fd;

    ret_iv = Interval{in.type.tmin(), in.type.tmax()};
    for (const auto& en : sum.ensures) {
      if (en.arg_index != -1) continue;
      VInfo tmp;
      tmp.type = in.type;
      tmp.iv = ret_iv;
      if (refine(tmp, en.op, en.value)) ret_iv = tmp.iv;
    }
  } else if (callee) {
    bool recursive = false;
    for (const auto& n : call_stack_)
      if (n == callee->name) recursive = true;
    if (recursive || depth_ >= kMaxDepth) {
      std::string note = "函数 " + callee->name +
                         " 涉及递归或超过调用深度上限 " + std::to_string(kMaxDepth) +
                         ", 无摘要可用, 返回值按类型全域处理(递归收敛规则), 相关结论为待确认";
      if (convergence_set_.insert(note).second)
        result_.convergence_notes.push_back(note);
      ret_iv = Interval{in.type.tmin(), in.type.tmax()};
      ret_risks.push_back(Risk{"RECURSION", in.id, in.loc,
                               "递归/深度超限且无函数摘要, 返回值不可证明(递归收敛规则)"});
      Finding fd;
      fd.kind = "call";
      fd.func = f.name;
      fd.instr_id = in.id;
      fd.loc = in.loc;
      fd.callsite_chain = callsite_chain;
      fd.target = in.text;
      fd.verdict = "unprotected";
      fd.evidence.push_back("被调函数 " + callee->name +
                            " 递归或超出深度上限且无摘要, 无法分析其内部长度使用");
      fd.has_first = true;
      fd.first_id = in.id;
      fd.first_loc = in.loc;
      fd.first_text = in.text;
      fd.first_reason = "递归调用缺少函数摘要, 无法证明长度约束被保持";
      std::string key = chain_prefix(callsite_chain) + f.name + "|call|" +
                        std::to_string(in.id);
      findings_map_[key] = fd;
    } else {
      // 上下文敏感: 用实参区间分析被调函数, 各调用点独立。
      std::vector<std::string> chain2 = callsite_chain;
      chain2.push_back(site);
      call_stack_.push_back(callee->name);
      ++depth_;
      auto r = analyze_func(*callee, &arg_ivs, chain2);
      --depth_;
      call_stack_.pop_back();
      if (r) ret_iv = *r;
      else ret_iv = Interval{in.type.tmin(), in.type.tmax()};
    }
  } else {
    // 未定义且无摘要: 约束在调用中丢失。
    ret_iv = Interval{in.type.tmin(), in.type.tmax()};
    ret_risks.push_back(Risk{"CALL_NO_SUMMARY", in.id, in.loc,
                             "被调函数 " + in.callee +
                                 " 缺少函数摘要, 调用方的范围约束在调用中丢失"});
    Finding fd;
    fd.kind = "call";
    fd.func = f.name;
    fd.instr_id = in.id;
    fd.loc = in.loc;
    fd.callsite_chain = callsite_chain;
    fd.target = in.text;
    fd.verdict = "unprotected";
    for (size_t i = 0; i < arg_ivs.size(); ++i)
      fd.evidence.push_back("实参#" + std::to_string(i) + " ∈ " + iv_str(arg_ivs[i]));
    fd.evidence.push_back("被调函数 " + in.callee +
                          " 无定义且无函数摘要, 无法确认其内部对长度参数的使用");
    fd.has_first = true;
    fd.first_id = in.id;
    fd.first_loc = in.loc;
    fd.first_text = in.text;
    fd.first_reason = "调用 " + in.callee + " 缺少函数摘要, 范围约束在调用边界丢失";
    std::string key = chain_prefix(callsite_chain) + f.name + "|call|" +
                      std::to_string(in.id);
    findings_map_[key] = fd;
  }

  if (has_ret) {
    VInfo r;
    r.type = in.type;
    r.iv = ret_iv;
    r.risks = ret_risks;
    for (const auto& av : arg_vis) join_risks(r.risks, av.risks);
    st[in.dst] = r;
  }
}

// ---------------- 入口 ----------------

void Analyzer::finalize() {
  result_.findings.clear();
  for (auto& [k, fd] : findings_map_) result_.findings.push_back(fd);
  std::sort(result_.findings.begin(), result_.findings.end(),
            [](const Finding& a, const Finding& b) {
              if (a.loc.line != b.loc.line) return a.loc.line < b.loc.line;
              if (a.loc.col != b.loc.col) return a.loc.col < b.loc.col;
              if (a.func != b.func) return a.func < b.func;
              return a.callsite_chain.size() < b.callsite_chain.size();
            });
}

AnalysisResult Analyzer::analyze_root(const std::string& fname) {
  const Function* f = prog_.find_func(fname);
  if (f) analyze_func(*f, nullptr, {});
  finalize();
  return result_;
}

AnalysisResult Analyzer::run() {
  std::set<std::string> called;
  for (const auto& f : prog_.funcs)
    for (const auto& in : f.instrs)
      if (in.op == Op::Call && prog_.find_func(in.callee)) called.insert(in.callee);
  std::vector<std::string> roots;
  for (const auto& f : prog_.funcs)
    if (!called.count(f.name)) roots.push_back(f.name);
  if (roots.empty())
    for (const auto& f : prog_.funcs) roots.push_back(f.name);
  std::string desc;
  for (size_t i = 0; i < roots.size(); ++i) {
    if (i) desc += ", ";
    desc += roots[i];
  }
  result_.entry_desc = desc;
  for (const auto& r : roots) {
    const Function* f = prog_.find_func(r);
    if (f) analyze_func(*f, nullptr, {});
  }
  finalize();
  return result_;
}

}  // namespace sec018
