// fmtrewrite: mechanically rewrite C11 calls that pass a runtime string in
// the format-argument position of predeclared formatting functions.
//
// The tool drives clang to emit a JSON AST, analyzes source ranges from it,
// and emits a unified diff for the calls that can be fixed textually.
// Everything involving macros, runtime format specifiers, or declarations the
// tool cannot bind to a supported signature is routed to a review list.

#include "Diff.hpp"
#include "Json.hpp"
#include "Rewriter.hpp"

#include <array>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <memory>
#include <sstream>
#include <string>
#include <unistd.h>
#include <vector>

#include <sys/wait.h>

namespace {

// The two supported functions and their format-argument positions (0-based).
const std::vector<rewriter::TargetSpec> kTargets = {
    {"printf", "int (const char *, ...)", 0},
    {"syslog", "void (int, const char *, ...)", 1},
};

std::string readFile(const std::string& path) {
    std::ifstream in(path, std::ios::binary);
    if (!in) throw std::runtime_error("cannot open input file: " + path);
    std::ostringstream ss;
    ss << in.rdbuf();
    return ss.str();
}

struct ProcessOutput {
    int exitCode = 0;
    std::string out;
    std::string err;
};

ProcessOutput runCommand(const std::vector<std::string>& argv) {
    // Build an argv array for execvp-style invocation through /bin/sh -c,
    // with every argument shell-quoted. stderr goes to a private temp file.
    auto shellQuote = [](const std::string& s) {
        std::string q = "'";
        for (char c : s) {
            if (c == '\'') q += "'\\''";
            else q.push_back(c);
        }
        q.push_back('\'');
        return q;
    };
    std::string errPath =
        (std::getenv("TMPDIR") ? std::getenv("TMPDIR") : "/tmp/");
    errPath += "fmtrewrite.clang.XXXXXX";
    std::vector<char> errPathBuf(errPath.begin(), errPath.end());
    errPathBuf.push_back('\0');
    int errFd = mkstemp(errPathBuf.data());
    if (errFd < 0)
        throw std::runtime_error("failed to create temp file for clang stderr");
    close(errFd);  // popen child only needs the path
    std::string errFileName(errPathBuf.data());

    std::string cmdline;
    for (size_t i = 0; i < argv.size(); ++i) {
        if (i) cmdline.push_back(' ');
        cmdline += shellQuote(argv[i]);
    }
    cmdline += " 2>";
    cmdline += shellQuote(errFileName);

    ProcessOutput result;
    FILE* pipe = popen(cmdline.c_str(), "r");
    if (!pipe) {
        std::remove(errFileName.c_str());
        throw std::runtime_error("failed to invoke clang");
    }
    std::array<char, 8192> buffer{};
    while (fgets(buffer.data(), buffer.size(), pipe))
        result.out += buffer.data();
    int status = pclose(pipe);
    if (status == -1) throw std::runtime_error("pclose failed");
    result.exitCode = WIFEXITED(status) ? WEXITSTATUS(status) : 128;

    std::ifstream errFile(errFileName, std::ios::binary);
    if (errFile) {
        std::ostringstream ss;
        ss << errFile.rdbuf();
        result.err = ss.str();
    }
    std::remove(errFileName.c_str());
    return result;
}

// Filter out the exact diagnostics clang emits for the defect class we
// rewrite. Clang prints blocks that start with "file:line:col: warning: ..."
// or "... note: ..."; all following non-header lines are source context for
// that block. We drop every format-security warning block (and its notes and
// context lines), keep any other diagnostic, and drop the final
// "N warnings generated." summary.
bool isDiagnosticHeader(const std::string& line, const char* kind) {
    size_t colon = line.find(':');
    if (colon == std::string::npos) return false;
    std::string marker = std::string(": ") + kind + ": ";
    return line.find(marker, colon) != std::string::npos;
}

std::string filterKnownDiagnostics(const std::string& stderrText) {
    std::istringstream in(stderrText);
    std::ostringstream out;
    std::string line;
    bool skipping = false;
    while (std::getline(in, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.find(" warnings generated.") != std::string::npos ||
            line.find(" warning generated.") != std::string::npos)
            continue;

        if (isDiagnosticHeader(line, "warning")) {
            skipping =
                line.find("-Wformat-security") != std::string::npos;
            if (!skipping) out << line << '\n';
            continue;
        }
        if (isDiagnosticHeader(line, "note")) {
            // Notes always attach to the preceding warning; keep skipping if
            // the warning was a format-security one.
            if (!skipping) out << line << '\n';
            continue;
        }
        if (!skipping) out << line << '\n';
    }
    std::string kept = out.str();
    while (!kept.empty() && kept.back() == '\n') kept.pop_back();
    return kept;
}

void printUsage() {
    std::cerr <<
        "usage: fmtrewrite [--apply] [--clang <path>] [--json] [--quiet]\n"
        "                  <source.c> [-- <extra clang arguments>]\n"
        "\n"
        "  --apply        rewrite the file in place instead of printing a diff\n"
        "  --clang PATH   clang executable to use (default: clang)\n"
        "  --json         print the machine-readable report to stdout\n"
        "  --quiet        suppress the human-readable report\n";
}

} // namespace

int main(int argc, char** argv) {
    bool apply = false;
    bool jsonMode = false;
    bool quiet = false;
    std::string clangPath = "clang";
    std::string sourceFile;
    std::vector<std::string> extraClangArgs;
    bool passThrough = false;

    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (passThrough) {
            extraClangArgs.push_back(arg);
        } else if (arg == "--") {
            passThrough = true;
        } else if (arg == "--apply") {
            apply = true;
        } else if (arg == "--json") {
            jsonMode = true;
        } else if (arg == "--quiet") {
            quiet = true;
        } else if (arg == "--clang") {
            if (++i >= argc) { printUsage(); return 2; }
            clangPath = argv[i];
        } else if (arg == "-h" || arg == "--help") {
            printUsage();
            return 0;
        } else if (!arg.empty() && arg[0] == '-') {
            std::cerr << "unknown option: " << arg << "\n";
            printUsage();
            return 2;
        } else if (sourceFile.empty()) {
            sourceFile = arg;
        } else {
            std::cerr << "unexpected positional argument: " << arg << "\n";
            return 2;
        }
    }
    if (sourceFile.empty()) {
        printUsage();
        return 2;
    }

    std::string sourceText;
    try {
        sourceText = readFile(sourceFile);
    } catch (const std::exception& e) {
        std::cerr << "fmtrewrite: " << e.what() << "\n";
        return 2;
    }

    // 1. Ask clang for the JSON AST of the translation unit.
    std::vector<std::string> cmd = {
        clangPath, "-std=c11", "-fsyntax-only",
        "-Xclang", "-ast-dump=json",
        sourceFile,
    };
    cmd.insert(cmd.end(), extraClangArgs.begin(), extraClangArgs.end());

    ProcessOutput co;
    try {
        co = runCommand(cmd);
    } catch (const std::exception& e) {
        std::cerr << "fmtrewrite: " << e.what() << "\n";
        return 2;
    }
    // A non-zero clang exit means no usable AST (hard parse/sema error).
    // Warnings alone (exit 0) do not block analysis; surface anything other
    // than the format-security class we are built to handle.
    if (co.exitCode != 0) {
        std::cerr << "fmtrewrite: clang failed to analyze " << sourceFile
                  << " (exit " << co.exitCode << ")\n";
        std::cerr << co.err;
        return 3;
    }
    if (std::string other = filterKnownDiagnostics(co.err); !other.empty())
        std::cerr << "fmtrewrite: clang diagnostics for " << sourceFile
                  << ":\n" << other << "\n";

    // 2. Parse the JSON AST.
    std::unique_ptr<json::Document> doc;
    try {
        auto parsed = json::Document::parse(co.out);
        doc = std::make_unique<json::Document>(std::move(parsed));
    } catch (const std::exception& e) {
        std::cerr << "fmtrewrite: " << e.what() << "\n";
        return 3;
    }

    // 3. Analyze and collect edits/review items.
    rewriter::Result result =
        rewriter::analyze(sourceFile, sourceText, doc->root(), kTargets);

    std::vector<std::pair<size_t, std::string>> insertions;
    insertions.reserve(result.edits.size());
    for (const rewriter::Edit& e : result.edits)
        insertions.emplace_back(e.offset, e.inserted);

    std::string newText = diff::applyInsertions(sourceText, insertions);
    std::string patch =
        diff::unified(sourceFile, sourceFile, sourceText, newText);

    // 4. Output.
    if (apply && newText != sourceText) {
        std::ofstream out(sourceFile, std::ios::binary | std::ios::trunc);
        if (!out) {
            std::cerr << "fmtrewrite: cannot write " << sourceFile << "\n";
            return 2;
        }
        out << newText;
    }

    if (jsonMode) {
        auto jsonEscape = [](std::string s) {
            std::string out;
            out.reserve(s.size() + 2);
            for (char c : s) {
                switch (c) {
                case '"': out += "\\\""; break;
                case '\\': out += "\\\\"; break;
                case '\b': out += "\\b"; break;
                case '\f': out += "\\f"; break;
                case '\n': out += "\\n"; break;
                case '\r': out += "\\r"; break;
                case '\t': out += "\\t"; break;
                default:
                    if (static_cast<unsigned char>(c) < 0x20) {
                        char buf[8];
                        std::snprintf(buf, sizeof(buf), "\\u%04x",
                                      static_cast<unsigned char>(c));
                        out += buf;
                    } else {
                        out.push_back(c);
                    }
                }
            }
            return out;
        };
        std::cout << "{\n";
        std::cout << "  \"file\": \"" << jsonEscape(sourceFile) << "\",\n";
        std::cout << "  \"targetCalls\": " << result.totalCallsSeen << ",\n";
        std::cout << "  \"alreadyLiteral\": " << result.literalSafe << ",\n";
        std::cout << "  \"rewritten\": " << result.edits.size() << ",\n";
        std::cout << "  \"review\": [\n";
        for (size_t i = 0; i < result.review.size(); ++i) {
            const auto& r = result.review[i];
            std::cout << "    {\"line\": " << r.loc.line
                      << ", \"column\": " << r.loc.col
                      << ", \"function\": \"" << jsonEscape(r.function)
                      << "\", \"reason\": \"" << jsonEscape(reasonText(r.reason))
                      << "\", \"detail\": \"" << jsonEscape(r.detail)
                      << "\"}" << (i + 1 < result.review.size() ? "," : "")
                      << "\n";
        }
        std::cout << "  ]\n";
        std::cout << "}\n";
    } else if (!apply) {
        // stdout carries ONLY the unified diff so it can be piped straight
        // into patch(1). In --apply mode nothing is written to stdout; the
        // human-readable report always goes to stderr.
        std::cout << patch;
    }

    if (!quiet && !jsonMode) {
        std::cerr << "# fmtrewrite summary for " << sourceFile << "\n"
                  << "#   target calls seen:      " << result.totalCallsSeen
                  << "\n"
                  << "#   already literal (safe): " << result.literalSafe
                  << "\n"
                  << "#   mechanically rewritten: " << result.edits.size()
                  << "\n"
                  << "#   routed to review:       " << result.review.size()
                  << "\n";
        for (const auto& r : result.review) {
            std::cerr << "# REVIEW " << sourceFile << ":" << r.loc.line << ":"
                      << r.loc.col << " " << r.function << "() - "
                      << reasonText(r.reason) << "\n"
                      << "#        " << r.detail << "\n";
        }
        if (apply && result.edits.empty())
            std::cerr << "# no changes applied\n";
    }

    return 0;
}
