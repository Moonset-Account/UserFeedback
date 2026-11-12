// fixcheck — 发版前修复符号来源核对工具 (C++17)
//
// 读取四类本地构建记录，判断发布包中的修复符号是否来自正确的源码修订：
//   1. 修复源文件摘要清单  FIX <id> <目标> <源文件> <修复后摘要> <符号1,符号2,...>
//   2. 编译单元记录        CU  <id> <目标> <对象文件> <源文件> <源摘要>
//   3. 归档成员            AR  <id> <目标> <归档> <成员名> <对象文件>
//   4. 链接符号归属记录    SYM <id> <目标> <符号> <提供对象>
//
// 结论分类：
//   OK            修复符号来自正确的源码修订
//   STALE_OBJECT  发布包打包了旧对象（摘要与修复修订不一致）
//   MISSING_UNIT  编译单元缺失（修复源未编译/未归档/未链接进目标）
//   AMBIGUOUS     来源无法唯一确认（多条冲突记录或缺少归属链）
//
// 用法: fixcheck <fix_manifest> <compile_units> <archive_members> <link_symbols>
// 退出码: 0 = 全部通过；1 = 存在非 OK 结论；2 = 输入格式错误。

#include <algorithm>
#include <fstream>
#include <iostream>
#include <set>
#include <sstream>
#include <string>
#include <vector>

namespace {

struct FixRec { std::string id, target, source, digest; std::vector<std::string> symbols; };
struct CuRec  { std::string id, target, object, source, digest; };
struct ArRec  { std::string id, target, archive, member, object; };
struct SymRec { std::string id, target, symbol, object; };

struct Data {
    std::vector<FixRec> fixes;
    std::vector<CuRec>  cus;
    std::vector<ArRec>  ars;
    std::vector<SymRec> syms;
};

std::vector<std::string> splitWs(const std::string& line) {
    std::vector<std::string> out;
    std::istringstream is(line);
    std::string tok;
    while (is >> tok) out.push_back(tok);
    return out;
}

std::vector<std::string> splitComma(const std::string& s) {
    std::vector<std::string> out;
    std::string cur;
    std::istringstream is(s);
    while (std::getline(is, cur, ',')) if (!cur.empty()) out.push_back(cur);
    return out;
}

std::string join(const std::vector<std::string>& v, const char* sep) {
    std::string r;
    for (size_t i = 0; i < v.size(); ++i) { if (i) r += sep; r += v[i]; }
    return r;
}

std::string baseNoExt(const std::string& p) {
    auto slash = p.find_last_of('/');
    std::string s = (slash == std::string::npos) ? p : p.substr(slash + 1);
    auto dot = s.find_last_of('.');
    if (dot != std::string::npos) s = s.substr(0, dot);
    return s;
}

// 逐行解析记录文件；tag 为期望的记录前缀；fieldCount 为前缀后的字段数。
std::vector<std::vector<std::string>> parseFile(const std::string& path,
                                                const std::string& tag,
                                                size_t fieldCount) {
    std::ifstream in(path);
    if (!in) {
        std::cerr << "错误: 无法打开输入文件 " << path << "\n";
        std::exit(2);
    }
    std::vector<std::vector<std::string>> rows;
    std::string line;
    size_t lineno = 0;
    while (std::getline(in, line)) {
        ++lineno;
        auto t = splitWs(line);
        if (t.empty() || t[0].rfind("#", 0) == 0) continue;
        if (t[0] != tag || t.size() != fieldCount + 1) {
            std::cerr << "错误: " << path << ":" << lineno
                      << " 记录格式不符（期望 " << tag << " + " << fieldCount << " 字段）\n";
            std::exit(2);
        }
        rows.emplace_back(t.begin() + 1, t.end());
    }
    return rows;
}

Data loadData(const std::string& fixPath, const std::string& cuPath,
              const std::string& arPath, const std::string& symPath) {
    Data d;
    for (auto& r : parseFile(fixPath, "FIX", 5))
        d.fixes.push_back({r[0], r[1], r[2], r[3], splitComma(r[4])});
    for (auto& r : parseFile(cuPath, "CU", 5))
        d.cus.push_back({r[0], r[1], r[2], r[3], r[4]});
    for (auto& r : parseFile(arPath, "AR", 5))
        d.ars.push_back({r[0], r[1], r[2], r[3], r[4]});
    for (auto& r : parseFile(symPath, "SYM", 4))
        d.syms.push_back({r[0], r[1], r[2], r[3]});
    return d;
}

enum class Verdict { OK, STALE_OBJECT, MISSING_UNIT, AMBIGUOUS };

const char* verdictName(Verdict v) {
    switch (v) {
        case Verdict::OK:           return "OK";
        case Verdict::STALE_OBJECT: return "STALE_OBJECT";
        case Verdict::MISSING_UNIT: return "MISSING_UNIT";
        case Verdict::AMBIGUOUS:    return "AMBIGUOUS";
    }
    return "?";
}

struct Finding {
    std::string target, symbol, source, digest;
    Verdict verdict = Verdict::OK;
    std::vector<std::string> notes;     // 结论说明
    std::vector<std::string> evidence;  // 输入记录编号（定位缓存/重建目标用）
    std::string action;                 // 处置建议
};

void addEvidence(Finding& f, const std::string& rec) {
    if (std::find(f.evidence.begin(), f.evidence.end(), rec) == f.evidence.end())
        f.evidence.push_back(rec);
}

Finding analyze(const FixRec& fix, const std::string& sym, const Data& d) {
    Finding f;
    f.target = fix.target; f.symbol = sym; f.source = fix.source; f.digest = fix.digest;
    addEvidence(f, "FIX:" + fix.id);

    // 1) 链接符号归属：该目标中谁提供了此符号？
    std::vector<const SymRec*> syms;
    for (auto& s : d.syms)
        if (s.target == fix.target && s.symbol == sym) { syms.push_back(&s); addEvidence(f, "SYM:" + s.id); }

    if (syms.empty()) {
        // 符号未进入链接：查编译单元与归档，定位缺失环节。
        f.verdict = Verdict::MISSING_UNIT;
        std::vector<const CuRec*> cus;
        for (auto& c : d.cus)
            if (c.target == fix.target && c.source == fix.source) { cus.push_back(&c); addEvidence(f, "CU:" + c.id); }
        if (cus.empty()) {
            f.notes.push_back("编译单元缺失：目标缺少修复源文件 " + fix.source +
                              " 的编译记录，修复未参与该目标构建");
        } else {
            bool archived = false;
            for (auto* c : cus)
                for (auto& a : d.ars)
                    if (a.target == fix.target && a.object == c->object) {
                        archived = true; addEvidence(f, "AR:" + a.id);
                    }
            f.notes.push_back(archived
                ? "编译单元已归档但符号未出现在链接记录中，修复对象未被链接进目标"
                : "编译单元存在但对象未进入任何归档成员，修复未打包进发布包");
        }
        f.action = "核对构建配置后重新构建目标 " + fix.target;
        return f;
    }

    std::set<std::string> providers;
    for (auto* s : syms) providers.insert(s->object);
    if (providers.size() > 1) {
        f.verdict = Verdict::AMBIGUOUS;
        f.notes.push_back("链接记录给出多个不同的提供对象（" +
                          join(std::vector<std::string>(providers.begin(), providers.end()), ", ") +
                          "），来源无法唯一确认");
        f.action = "消除重复归属记录后复查目标 " + fix.target;
        return f;
    }
    const std::string obj = *providers.begin();

    // 2) 提供对象的编译来源。
    std::vector<const CuRec*> cus;
    for (auto& c : d.cus)
        if (c.target == fix.target && c.object == obj) { cus.push_back(&c); addEvidence(f, "CU:" + c.id); }
    if (cus.empty()) {
        for (auto& a : d.ars)
            if (a.target == fix.target && (a.object == obj || a.member == obj))
                addEvidence(f, "AR:" + a.id);
        f.verdict = Verdict::AMBIGUOUS;
        f.notes.push_back("提供对象 " + obj + " 没有对应的编译单元记录，来源无法唯一确认");
        f.action = "补齐编译单元记录后复查目标 " + fix.target;
        return f;
    }
    std::set<std::string> digests, sources;
    for (auto* c : cus) { digests.insert(c->digest); sources.insert(c->source); }
    if (digests.size() > 1) {
        f.verdict = Verdict::AMBIGUOUS;
        f.notes.push_back("对象 " + obj + " 对应多条摘要不同的编译记录，来源无法唯一确认");
        f.action = "清理冲突的编译缓存后复查目标 " + fix.target;
        return f;
    }
    const std::string src = *sources.begin(), dg = *digests.begin();

    if (src != fix.source) {
        f.verdict = Verdict::AMBIGUOUS;
        f.notes.push_back("符号由 " + src + " 编译的对象 " + obj +
                          " 提供，并非修复源文件 " + fix.source + "，无法确认修复是否生效");
        f.action = "人工确认符号归属后复查目标 " + fix.target;
        return f;
    }

    if (dg == fix.digest) {
        f.verdict = Verdict::OK;
        if (baseNoExt(obj) != baseNoExt(fix.source))
            f.notes.push_back("符号由对象 " + obj +
                              " 提供（对象名与源文件不对应，可能为优化后改由另一对象提供或合并编译产物），"
                              "其源摘要与修复修订一致");
        else
            f.notes.push_back("提供对象 " + obj + " 的源摘要与修复修订一致");
        return f;
    }

    // 3) 旧对象：摘要与修复修订不一致。
    f.verdict = Verdict::STALE_OBJECT;
    f.notes.push_back("提供对象 " + obj + " 编译自旧修订（摘要 " + dg +
                      "，期望 " + fix.digest + "），发布包仍打包旧对象");
    // 同名归档成员覆盖检测：同一归档同一成员名是否另有指向新修订的对象。
    for (auto& a1 : d.ars) {
        if (!(a1.target == fix.target && a1.object == obj)) continue;
        for (auto& a2 : d.ars) {
            if (!(a2.target == fix.target && a2.archive == a1.archive &&
                  a2.member == a1.member && a2.object != obj)) continue;
            for (auto& c : d.cus) {
                if (c.target == fix.target && c.object == a2.object && c.digest == fix.digest) {
                    addEvidence(f, "AR:" + a1.id);
                    addEvidence(f, "AR:" + a2.id);
                    addEvidence(f, "CU:" + c.id);
                    f.notes.push_back("归档 " + a1.archive + " 的同名成员 " + a1.member +
                                      " 存在新对象 " + a2.object +
                                      "，但链接选中了旧对象（同名成员覆盖）");
                }
            }
        }
    }
    f.action = "清理缓存对象 " + obj + " 并重建目标 " + fix.target;
    return f;
}

} // namespace

int main(int argc, char** argv) {
    if (argc != 5) {
        std::cerr << "用法: " << argv[0]
                  << " <fix_manifest> <compile_units> <archive_members> <link_symbols>\n";
        return 2;
    }
    Data d = loadData(argv[1], argv[2], argv[3], argv[4]);

    // 按清单中目标首次出现的顺序分组输出。
    std::vector<std::string> targets;
    for (auto& f : d.fixes)
        if (std::find(targets.begin(), targets.end(), f.target) == targets.end())
            targets.push_back(f.target);

    bool allOk = true;
    struct Count { int ok = 0, stale = 0, missing = 0, ambiguous = 0; };
    std::vector<std::pair<std::string, Count>> summary;

    for (auto& t : targets) {
        std::cout << "===== 构建目标: " << t << " =====\n";
        Count cnt;
        for (auto& fix : d.fixes) {
            if (fix.target != t) continue;
            for (auto& sym : fix.symbols) {
                Finding r = analyze(fix, sym, d);
                switch (r.verdict) {
                    case Verdict::OK:           ++cnt.ok;        break;
                    case Verdict::STALE_OBJECT: ++cnt.stale;     allOk = false; break;
                    case Verdict::MISSING_UNIT: ++cnt.missing;   allOk = false; break;
                    case Verdict::AMBIGUOUS:    ++cnt.ambiguous; allOk = false; break;
                }
                std::cout << "[" << verdictName(r.verdict) << "] " << r.symbol
                          << "  (源: " << r.source << ", 修复摘要: " << r.digest << ")\n";
                for (auto& n : r.notes) std::cout << "    结论: " << n << "\n";
                std::cout << "    依据记录: " << join(r.evidence, " ") << "\n";
                if (!r.action.empty()) std::cout << "    处置: " << r.action << "\n";
            }
        }
        summary.push_back({t, cnt});
        std::cout << "\n";
    }

    std::cout << "===== 汇总 =====\n";
    for (auto& [t, c] : summary) {
        std::cout << "目标 " << t << ": OK=" << c.ok
                  << " STALE_OBJECT=" << c.stale
                  << " MISSING_UNIT=" << c.missing
                  << " AMBIGUOUS=" << c.ambiguous;
        if (c.stale || c.missing || c.ambiguous) {
            std::cout << "  -> 待处理:";
            if (c.stale)     std::cout << " 清理缓存对象并重建";
            if (c.missing)   std::cout << " 重新构建目标";
            if (c.ambiguous) std::cout << " 补齐记录后复查";
        }
        std::cout << "\n";
    }
    std::cout << (allOk ? "总体结论: 全部修复符号来源正确\n"
                        : "总体结论: 存在异常，发版前需按上述记录编号定位处理\n");
    return allOk ? 0 : 1;
}
