#include "Diff.hpp"

#include <algorithm>
#include <sstream>
#include <stdexcept>

namespace diff {

namespace {

std::vector<std::string> splitLines(const std::string& text, bool& trailingNewline) {
    std::vector<std::string> lines;
    trailingNewline = !text.empty() && text.back() == '\n';
    size_t start = 0;
    for (size_t i = 0; i < text.size(); ++i) {
        if (text[i] == '\n') {
            lines.push_back(text.substr(start, i - start));
            start = i + 1;
        }
    }
    if (start < text.size()) lines.push_back(text.substr(start));
    return lines;
}

enum class Op { Equal, Delete, Insert };

// LCS edit script over whole lines.
std::vector<Op> lcsScript(const std::vector<std::string>& a,
                          const std::vector<std::string>& b) {
    const size_t n = a.size();
    const size_t m = b.size();
    std::vector<std::vector<unsigned>> dp(
        n + 1, std::vector<unsigned>(m + 1, 0));
    for (size_t i = n; i-- > 0;)
        for (size_t j = m; j-- > 0;)
            dp[i][j] = (a[i] == b[j])
                           ? dp[i + 1][j + 1] + 1
                           : std::max(dp[i + 1][j], dp[i][j + 1]);

    std::vector<Op> ops;
    size_t i = 0, j = 0;
    while (i < n && j < m) {
        if (a[i] == b[j]) {
            ops.push_back(Op::Equal);
            ++i;
            ++j;
        } else if (dp[i + 1][j] >= dp[i][j + 1]) {
            ops.push_back(Op::Delete);
            ++i;
        } else {
            ops.push_back(Op::Insert);
            ++j;
        }
    }
    while (i++ < n) ops.push_back(Op::Delete);
    while (j++ < m) ops.push_back(Op::Insert);
    return ops;
}

std::string rangeLabel(unsigned start, unsigned count) {
    // Unified diff convention: a zero-length hunk starts one line earlier.
    if (count == 0) {
        return std::to_string(start) + ",0";
    }
    if (count == 1) return std::to_string(start);
    return std::to_string(start) + "," + std::to_string(count);
}

} // namespace

std::string unified(const std::string& oldLabel,
                    const std::string& newLabel,
                    const std::string& oldText,
                    const std::string& newText) {
    if (oldText == newText) return {};

    bool oldNL, newNL;
    std::vector<std::string> a = splitLines(oldText, oldNL);
    std::vector<std::string> b = splitLines(newText, newNL);
    std::vector<Op> ops = lcsScript(a, b);

    // Replay the script with indices.
    struct Row {
        Op op;
        size_t ai;
        size_t bi;
    };
    std::vector<Row> rows;
    rows.reserve(ops.size());
    size_t ai = 0, bi = 0;
    for (Op op : ops) {
        rows.push_back({op, ai, bi});
        if (op == Op::Equal) { ++ai; ++bi; }
        else if (op == Op::Delete) { ++ai; }
        else { ++bi; }
    }

    constexpr unsigned CONTEXT = 3;
    std::ostringstream out;
    out << "--- " << oldLabel << "\n";
    out << "+++ " << newLabel << "\n";

    size_t k = 0;
    while (k < rows.size()) {
        // Skip stretches of equal context beyond the window.
        if (rows[k].op == Op::Equal) { ++k; continue; }
        size_t first = k;
        while (first > 0 && rows[first - 1].op == Op::Equal &&
               k - first < CONTEXT)
            --first;
        size_t last = k;
        while (last < rows.size() && rows[last].op != Op::Equal)
            ++last;
        size_t end = last;
        unsigned ctx = 0;
        while (end < rows.size() && rows[end].op == Op::Equal &&
               ctx < CONTEXT) {
            ++end;
            ++ctx;
        }
        // Merge with a following hunk whose context window overlaps this one.
        size_t probe = end;
        while (probe < rows.size() && rows[probe].op == Op::Equal) {
            size_t gapStart = probe;
            while (probe < rows.size() && rows[probe].op == Op::Equal)
                ++probe;
            if (probe >= rows.size()) break;
            unsigned gap = static_cast<unsigned>(probe - gapStart);
            if (gap <= 2 * CONTEXT) {
                end = probe;
                while (end < rows.size() && rows[end].op != Op::Equal)
                    ++end;
                ctx = 0;
                while (end < rows.size() && rows[end].op == Op::Equal &&
                       ctx < CONTEXT) {
                    ++end;
                    ++ctx;
                }
                probe = end;
            } else {
                break;
            }
        }

        unsigned oldStart = 0, oldCount = 0, newStart = 0, newCount = 0;
        for (size_t x = first; x < end; ++x) {
            const Row& r = rows[x];
            if (r.op == Op::Equal) {
                if (oldCount == 0) oldStart = static_cast<unsigned>(r.ai);
                if (newCount == 0) newStart = static_cast<unsigned>(r.bi);
                ++oldCount;
                ++newCount;
            } else if (r.op == Op::Delete) {
                if (oldCount == 0) oldStart = static_cast<unsigned>(r.ai);
                if (newCount == 0) newStart = static_cast<unsigned>(r.bi);
                ++oldCount;
            } else {
                if (oldCount == 0) oldStart = static_cast<unsigned>(r.ai);
                if (newCount == 0) newStart = static_cast<unsigned>(r.bi);
                ++newCount;
            }
        }
        // Convert zero-based indices to one-based line numbers.
        out << "@@ -" << rangeLabel(oldStart + 1, oldCount) << " +"
            << rangeLabel(newStart + 1, newCount) << " @@\n";
        for (size_t x = first; x < end; ++x) {
            const Row& r = rows[x];
            if (r.op == Op::Equal)
                out << ' ' << a[r.ai] << '\n';
            else if (r.op == Op::Delete)
                out << '-' << a[r.ai] << '\n';
            else
                out << '+' << b[r.bi] << '\n';
        }
        k = end;
    }

    // Flag a missing final newline, like diff -u does.
    if (!oldNL && oldText != newText)
        out << "\\ No newline at end of file\n";
    (void)newNL;
    return out.str();
}

std::string applyInsertions(
    const std::string& text,
    const std::vector<std::pair<size_t, std::string>>& edits) {
    std::vector<std::pair<size_t, std::string>> sorted = edits;
    std::sort(sorted.begin(), sorted.end(),
              [](const auto& x, const auto& y) { return x.first < y.first; });
    std::string result;
    result.reserve(text.size());
    size_t cursor = 0;
    for (const auto& [offset, inserted] : sorted) {
        if (offset < cursor || offset > text.size())
            throw std::runtime_error(
                "internal: overlapping or out-of-range edit at offset " +
                std::to_string(offset));
        result.append(text, cursor, offset - cursor);
        result.append(inserted);
        cursor = offset;
    }
    result.append(text, cursor, std::string::npos);
    return result;
}

} // namespace diff
