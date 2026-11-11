#!/usr/bin/env bash
#
# End-to-end tests for fmtrewrite.
#
# Covers:
#   1. same-named custom functions are never rewritten (review list)
#   2. legitimate literal format calls produce an empty diff
#   3. fixable calls receive the mechanical "%s" insertion
#   4. macro definitions / macro-expanded calls go to review with locations
#   5. a mixed file combining every decision path
#   6. clang compiles the patched source, runtime output matches expected,
#      and a second run is idempotent (no repeated rewriting)
#
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOOL="$ROOT/build/fmtrewrite"
CASES="$ROOT/tests/cases"
EXPECTED="$ROOT/tests/expected"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/fmtrewrite-tests.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

CLANG="${CLANG:-clang}"
FAILURES=0
TESTS=0

pass() { TESTS=$((TESTS+1)); echo "ok   - $1"; }
fail() { TESTS=$((TESTS+1)); FAILURES=$((FAILURES+1)); echo "FAIL - $1"; echo "       $2"; }

assert_grep() {  # pattern file message
    if grep -Eq "$1" "$2"; then pass "$3"; else fail "$3" "pattern /$1/ not found in $2"; fi
}
assert_not_grep() {
    if ! grep -Eq "$1" "$2"; then pass "$3"; else fail "$3" "unexpected pattern /$1/ in $2"; fi
}
assert_contains() {  # fixed-string file message
    if grep -Fq "$1" "$2"; then pass "$3"; else fail "$3" "string not found in $2: $1"; fi
}
assert_file_empty() {
    if [[ ! -s "$1" ]]; then pass "$2"; else fail "$2" "file $1 is not empty:"; sed 's/^/       /' "$1"; fi
}

[[ -x "$TOOL" ]] || { echo "build the tool first: make"; exit 2; }

echo "== 1. same-named custom function =="
cp "$CASES/01_same_name_custom.c" "$WORK/t1.c"
"$TOOL" --json "$WORK/t1.c" > "$WORK/t1.report" 2> "$WORK/t1.err"
rc=$?
[[ $rc -eq 0 ]] && pass "tool exits 0 despite review items" || fail "tool exit" "rc=$rc"
assert_file_empty "$WORK/t1.err" "no stderr noise"
assert_grep '"rewritten": 0' "$WORK/t1.report" "no mechanical rewrite of the custom function"
assert_grep 'UnsupportedCallee|"call does not bind' "$WORK/t1.report" "review names unsupported callee"
assert_grep '01_same_name_custom|t1.c' "$WORK/t1.report" "review carries file location"
# Source must be byte-identical (no --apply used, but double-check no edit data).
cmp -s "$CASES/01_same_name_custom.c" "$WORK/t1.c" \
    && pass "source untouched" || fail "source untouched" "file changed"

echo "== 2. literal-safe calls =="
cp "$CASES/02_literal_safe.c" "$WORK/t2.c"
"$TOOL" "$WORK/t2.c" > "$WORK/t2.diff" 2> "$WORK/t2.err"
rc=$?
[[ $rc -eq 0 ]] && pass "tool exits 0" || fail "tool exit" "rc=$rc"
# Report goes to stderr; stdout must contain only the (empty) diff.
assert_not_grep 'warning|error' "$WORK/t2.err" "no clang diagnostics surfaced"
# A safe run prints only the summary on stderr, no diff headers on stdout.
assert_not_grep '^--- ' "$WORK/t2.diff" "no diff produced for literal calls"
assert_grep 'already literal \(safe\): 5' "$WORK/t2.err" "all five literal calls counted safe"
assert_not_grep 'REVIEW' "$WORK/t2.err" "no review entries"

echo "== 3. mechanically fixable calls =="
cp "$CASES/03_fixable.c" "$WORK/t3.c"
"$TOOL" "$WORK/t3.c" > "$WORK/t3.diff" 2> "$WORK/t3.err"
assert_not_grep 'warning|error' "$WORK/t3.err" "no clang diagnostics surfaced"
assert_grep 'printf\("%s", msg\);' "$WORK/t3.diff" "printf gets %s before the runtime arg"
assert_grep 'syslog\(LOG_ERR, "%s", msg\);' "$WORK/t3.diff" "syslog format position (index 1) handled"
assert_grep 'printf\("%s", buffer\);' "$WORK/t3.diff" "char array argument handled"
assert_grep 'printf\("%s", (msg)\);' "$WORK/t3.diff" "parenthesised expression spelling kept"
assert_contains 'printf("%s", make_message() /* keep me */);' "$WORK/t3.diff" "inner comment preserved next to its argument"
assert_grep 'mechanically rewritten: 5' "$WORK/t3.err" "the five no-trailing-arg calls are rewritten"
assert_grep 'runtime format' "$WORK/t3.err" "trailing-vararg calls routed to review"
grep -c 'REVIEW' "$WORK/t3.err" | grep -q '2' \
    && pass "two review entries for the two trailing-vararg calls" \
    || fail "two review entries" "count mismatch"
# The pure diff on stdout must apply cleanly with the standard patch tool.
if command -v patch >/dev/null 2>&1; then
    cp "$CASES/03_fixable.c" "$WORK/t3_patch.c"
    (cd "$WORK" && patch -s t3_patch.c < "$WORK/t3.diff") \
        && pass "unified diff applies via patch(1)" \
        || fail "unified diff applies via patch(1)" "patch rejected the hunks"
    # And the patched result must equal what --apply writes.
    cp "$CASES/03_fixable.c" "$WORK/t3_apply.c"
    "$TOOL" --apply "$WORK/t3_apply.c" >/dev/null 2>&1
    cmp -s "$WORK/t3_patch.c" "$WORK/t3_apply.c" \
        && pass "patch(1) output matches --apply output" \
        || fail "patch vs apply equivalence" "outputs differ"
fi

echo "== 4. macro definitions and expansions =="
cp "$CASES/04_macros.c" "$WORK/t4.c"
"$TOOL" --json "$WORK/t4.c" > "$WORK/t4.report" 2> "$WORK/t4.err"
assert_file_empty "$WORK/t4.err" "no stderr noise"
assert_grep '"rewritten": 0' "$WORK/t4.report" "macro call sites are not rewritten"
assert_grep '"review": \[' "$WORK/t4.report" "review list present"
# Four macro cases, each with a line/column.
review_count=$(grep -c '"reason"' "$WORK/t4.report" || true)
[[ "$review_count" -eq 4 ]] && pass "all four macro cases listed ($review_count)" \
    || fail "all four macro cases listed" "found $review_count"
assert_grep 'macro expansion' "$WORK/t4.report" "reason text mentions macro expansion"
# The macro *definition* line itself must not be reported (only call sites).
assert_not_grep '"line": 1[012],' "$WORK/t4.report" "no reports point at #define lines"

echo "== 5. mixed decision paths =="
cp "$CASES/05_mixed.c" "$WORK/t5.c"
"$TOOL" --json "$WORK/t5.c" > "$WORK/t5.report" 2> "$WORK/t5.err"
assert_file_empty "$WORK/t5.err" "no stderr noise"
assert_grep '"rewritten": 1' "$WORK/t5.report" "the one library syslog call is rewritten"
assert_grep 'macro expansion' "$WORK/t5.report" "macro call routed to review"
assert_grep 'does not bind to a supported library declaration' "$WORK/t5.report" \
    "custom same-name printf routed to review"
assert_grep '"alreadyLiteral": 1' "$WORK/t5.report" "literal call left alone"
# printf calls: one macro (MacroExpansion) and one runtime (UnsupportedCallee);
# the library syslog is rewritten and the literal printf is safe.
review_count=$(grep -c '"reason"' "$WORK/t5.report" || true)
[[ "$review_count" -eq 2 ]] && pass "two review entries: macro + shadowed printf ($review_count)" \
    || fail "two review entries" "found $review_count"

echo "== 5b. non-string typed format argument =="
cat > "$WORK/t5b.c" <<'EOF'
#include <stdio.h>
int main(void) {
    int number = 7;
    printf(number);
    return 0;
}
EOF
"$TOOL" --json "$WORK/t5b.c" > "$WORK/t5b.report" 2> "$WORK/t5b.err"
# The -Wint-conversion warning is surfaced (non-fatal) but analysis continues.
assert_grep '"rewritten": 0' "$WORK/t5b.report" "non-char type is not rewritten"
assert_grep 'non-string' "$WORK/t5b.report" "non-string type routed to review with reason"
assert_grep 'Wint-conversion' "$WORK/t5b.err" "unrelated clang warning is surfaced, not swallowed"

echo "== 6. compile, runtime behavior and idempotency =="
cp "$CASES/06_behavior.c" "$WORK/t6.c"

# 6a. The original source compiles (with format-security warnings, which is
#     precisely the defect we are fixing).
"$CLANG" -std=c11 -Wall -Wextra -Werror \
    -Wno-format-security -Wno-unused-parameter \
    -include "$ROOT/tests/syslog_shim.h" \
    "$WORK/t6.c" -o "$WORK/t6.before" 2> "$WORK/t6.compile.before" \
    && pass "original source compiles under clang" \
    || fail "original source compiles" "$(cat "$WORK/t6.compile.before")"

# 6b. Apply the rewrite and verify the patched source compiles with full
#     warnings-as-errors, including -Wformat-security.
"$TOOL" --apply "$WORK/t6.c" > "$WORK/t6.apply" 2> "$WORK/t6.apply.err"
assert_file_empty "$WORK/t6.apply" "apply mode writes nothing to stdout"
assert_grep 'mechanically rewritten: 4' "$WORK/t6.apply.err" "all four dynamic calls rewritten"
"$CLANG" -std=c11 -Wall -Wextra -Werror -Wformat-security -Wno-unused-parameter \
    -include "$ROOT/tests/syslog_shim.h" \
    "$WORK/t6.c" -o "$WORK/t6.after" 2> "$WORK/t6.compile.after" \
    && pass "patched source compiles with -Werror -Wformat-security" \
    || fail "patched source compiles" "$(cat "$WORK/t6.compile.after")"

# 6c. Runtime output must match the expected verbatim printing.
"$WORK/t6.after" > "$WORK/t6.out"
cmp -s "$EXPECTED/behavior.txt" "$WORK/t6.out" \
    && pass "runtime output matches expected verbatim text" \
    || { fail "runtime output matches" "diff:"; diff -u "$EXPECTED/behavior.txt" "$WORK/t6.out" | sed 's/^/       /'; }

# 6d. Idempotency: running again produces no edits and leaves the file alone.
cp "$WORK/t6.c" "$WORK/t6.snapshot"
"$TOOL" "$WORK/t6.c" > "$WORK/t6.rerun" 2> "$WORK/t6.rerun.err"
assert_file_empty "$WORK/t6.rerun" "second run emits no diff on stdout"
assert_not_grep '^--- ' "$WORK/t6.rerun" "second run emits no diff"
assert_grep 'mechanically rewritten: 0' "$WORK/t6.rerun.err" "second run rewrites nothing"
cmp -s "$WORK/t6.snapshot" "$WORK/t6.c" \
    && pass "file is byte-identical after the second run" \
    || fail "idempotency" "the second run changed the file"

# 6e. The applied edit text itself shows the exact safe transformation.
assert_grep 'printf\("%s", user_controlled\);' "$WORK/t6.c" "transformation is the canonical %s binding"

echo
echo "=================================================="
echo "$TESTS tests, $FAILURES failures"
echo "artifacts kept in: $WORK"
exit $((FAILURES != 0))
