#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# End-to-end checks for the patch-plan validator. The validator only reads
# plan files; this script only compares its deterministic text output.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$ROOT/build/patchplan-validator"
PLAN="$ROOT/plans"
OUT="$ROOT/build/test-output"
mkdir -p "$OUT"

CXX="${CXX:-clang++}"
"$CXX" -std=c++17 -Wall -Wextra -Wpedantic -Werror -O2 \
  "$ROOT/src/main.cpp" "$ROOT/src/parser.cpp" "$ROOT/src/engine.cpp" \
  -o "$BIN" || { echo "BUILD: FAIL"; exit 1; }
echo "BUILD: OK (clang++ -std=c++17 -Wall -Wextra -Wpedantic -Werror)"

fail=0
# check <plan> <expected-exit> <keyword> [more keywords ...]
check() {
  local plan="$1" want_exit="$2"; shift 2
  "$BIN" --exhaustive "$PLAN/$plan" > "$OUT/$plan.out" 2>&1
  local got=$?
  local name="$(basename "$plan")"
  if [ "$got" -ne "$want_exit" ]; then
    echo "FAIL $name: exit code $got, want $want_exit"; fail=1; return
  fi
  for kw in "$@"; do
    if ! grep -Fq "$kw" "$OUT/$plan.out"; then
      echo "FAIL $name: missing output fragment: $kw"; fail=1; return
    fi
  done
  echo "PASS $name (exit=$got, keywords matched)"
}

# 1. Staged success: full forward apply, safe rollback, exhaustive agrees.
check 01-staged-success.plan 0 \
  "all 3 steps applied without constraint breach" \
  "SAFE ROLLBACK SEQUENCE (1 action(s))" "R1 (reverts patch P1)" \
  "EXHAUSTIVE CROSS-CHECK: 1 permutation(s), 1 safe; agreement=YES" \
  "W1: 1/1 changes" "W2: 2/2 changes" "RESULT: ACCEPTED"

# 2. Dependency cycle reported in pre-flight with a deterministic cycle path.
check 02-dependency-cycle.plan 1 \
  "VIOLATION: DEPENDENCY_CYCLE" "P1 -> P3 -> P2 -> P1" "RESULT: REJECTED"

# 3. Mutex conflict at the earliest possible step, with versions named.
check 03-mutex-patches.plan 1 \
  "VIOLATION: MUTEX_CONFLICT" "step:        2" "patch:       P2" \
  "fromVersion: v1" "toVersion:   v2" "RESULT: REJECTED"

# 4. Mid-flight failure with all rollback orderings safe: lexicographically
#    first shortest sequence is chosen deterministically.
check 04-safe-rollback.plan 0 \
  "SAFE ROLLBACK SEQUENCE (3 action(s))" "RA (reverts patch PA)" \
  "EXHAUSTIVE CROSS-CHECK: 6 permutation(s), 6 safe; agreement=YES" \
  "RESULT: ACCEPTED"

# 5. No safe rollback: concrete conflict evidence plus exhaustive 0 safe.
check 05-no-safe-rollback.plan 1 \
  "NO SAFE ROLLBACK EXISTS within budget" "ROLLBACK_INCOMPATIBLE" \
  "svc-x: v2 -> v1 vs svc-y@v2" \
  "EXHAUSTIVE CROSS-CHECK: 1 permutation(s), 0 safe; agreement=YES"

# 6. Unique valid path among several candidates: exactly 1/6 safe.
check 06-unique-valid-path.plan 0 \
  "EXHAUSTIVE CROSS-CHECK: 6 permutation(s), 1 safe; agreement=YES" \
  "lexicographically first safe path: RA RB RC" \
  "SAFE ROLLBACK SEQUENCE (3 action(s))"

# 7. Window capacity breach with change accounting.
check 07-window-capacity.plan 1 \
  "VIOLATION: WINDOW_CAPACITY" "capacity 1 exhausted" "W1: 1/1 changes"

# 8. Cross-service version mismatch naming both services and versions.
check 08-version-mismatch.plan 1 \
  "VIOLATION: COMPATIBILITY" "step:        2" \
  "conflictsWith: svc-b@v2" "svc-a@v2"

# 9. Budget exhaustion evidence even though every transition is safe.
check 09-budget-exhausted.plan 1 \
  "NO SAFE ROLLBACK EXISTS within budget" "ROLLBACK_BUDGET_EXHAUSTED" \
  "rollback budget of 2" "6 permutation(s), 0 safe"

# 9a. Version precondition: current and expected version both reported.
check 10-version-precondition.plan 1 \
  "VIOLATION: VERSION_PRECONDITION" "step:        2" \
  "service:     svc-a" "fromVersion: v1" "toVersion:   v3" \
  "expects 'v2'"

# 9b. Dependency not yet applied at the scheduled step.
check 11-dependency-missing.plan 1 \
  "VIOLATION: DEPENDENCY_MISSING" "step:        1" \
  "dependency patch 'P1' of 'P2' is not applied yet"

# 10. Determinism: identical input must produce byte-identical diagnostics.
"$BIN" --exhaustive "$PLAN"/*.plan > "$OUT/run-a.txt" 2>&1
"$BIN" --exhaustive "$PLAN"/*.plan > "$OUT/run-b.txt" 2>&1
if cmp -s "$OUT/run-a.txt" "$OUT/run-b.txt"; then
  echo "PASS deterministic-diagnostics (two runs byte-identical)"
else
  echo "FAIL deterministic-diagnostics: output differs between runs"; fail=1
fi

# 11. Parse errors are rejected without touching any release action.
printf 'service s v1\npatch P s v1 -> v2\nstep 1 P window=NOWINDOW\n' \
  > "$OUT/bad-parse.plan"
if "$BIN" "$OUT/bad-parse.plan" 2>&1 | grep -Fq "REJECTED (parse error)"; then
  echo "PASS parse-error-rejected"
else
  echo "FAIL parse-error-rejected"; fail=1
fi

# 12. Missing file is a clean usage-level failure, not a crash.
if "$BIN" "$OUT/does-not-exist.plan" 2>&1 | grep -Fq "cannot open plan file"; then
  echo "PASS missing-file-handled"
else
  echo "FAIL missing-file-handled"; fail=1
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "ALL TESTS PASSED"
  exit 0
fi
echo "SOME TESTS FAILED"
exit 1
