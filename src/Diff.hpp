#ifndef FMTREWRITE_DIFF_HPP
#define FMTREWRITE_DIFF_HPP

#include <cstddef>
#include <string>
#include <vector>

namespace diff {

// Line-based unified diff (3 lines of context), in the exact shape produced
// by `diff -u`: headers start with "--- "/"+++ ", hunks with "@@".
std::string unified(const std::string& oldLabel,
                    const std::string& newLabel,
                    const std::string& oldText,
                    const std::string& newText);

// Apply a sorted list of pure insertions to source text. All edits must lie
// within [0, text.size()].
std::string applyInsertions(const std::string& text,
                            const std::vector<std::pair<size_t, std::string>>& edits);

} // namespace diff

#endif // FMTREWRITE_DIFF_HPP
