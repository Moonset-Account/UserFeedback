#!/usr/bin/env bash
set -euo pipefail

case "${CRT_PROFILE:-}" in
  balanced|strict|secrets-only) ;;
  *) printf '%s\n' 'ERROR: invalid CodeRiskTools profile' >&2; exit 3 ;;
esac

: "${CRT_ACTION_PATH:?missing action path}"
: "${RUNNER_TEMP:?missing runner temp}"

export PYTHONPATH="${CRT_ACTION_PATH}${PYTHONPATH:+:${PYTHONPATH}}"

diff_file="${RUNNER_TEMP}/coderisktools-change.diff"
report_file="${RUNNER_TEMP}/coderisktools-scan.json"

python "${CRT_ACTION_PATH}/scripts/collect-diff.py" \
  --base "${CRT_BASE_SHA:-}" \
  --head "${CRT_HEAD_SHA:-}" \
  --output "${diff_file}"

set +e
python -m src scan --diff "${diff_file}" --profile "${CRT_PROFILE}" --format json --output "${report_file}"
status=$?
set -e

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  printf 'report=%s\n' "${report_file}" >> "${GITHUB_OUTPUT}"
fi

case "$status" in
  0|1|2) exit "$status" ;;
  *) printf '%s\n' 'ERROR: CodeRiskTools scanner execution failed' >&2; exit 3 ;;
esac
