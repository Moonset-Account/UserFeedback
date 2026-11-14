"""Comparison semantics: comparability, outcome classes and conflict handling."""

import unittest

from parser_regression import compare_reports, load_report, parse_report

from .helpers import export, fixture_path, record


def run_compare(baseline_results, current_results):
    baseline = parse_report(export("base-1", baseline_results), source="base.json")
    current = parse_report(export("cur-1", current_results), source="cur.json")
    return compare_reports(baseline, current)


def rows_of(summary, section):
    return summary["sections"][section]


class AccountingInvariantTests(unittest.TestCase):
    def assert_group_accounting(self, summary):
        """Every emitted row is counted, and counts add up to the rows present."""
        counts = summary["counts"]
        section_rows = {name: len(rows) for name, rows in summary["sections"].items()}
        counted_by_section = {
            "new_failures": "new_failures",
            "fixed": "fixed",
            "persistent_failures": "persistent_failures",
            "unstable": "unstable",
            "not_executed": "not_executed",
            "missing_from_current": "missing_from_current",
            "scope_mismatch": "scope_mismatch",
            "needs_confirmation": "needs_confirmation",
        }
        for section, count_key in counted_by_section.items():
            self.assertEqual(section_rows[section], counts[count_key],
                             f"{section} rows vs counts[{count_key}]")

        # Rows + stable passes + newly passing + folded baseline rows must
        # cover every group seen in either export, with no group counted twice.
        emitted = sum(section_rows.values())
        accounted = (emitted + counts["stable_passes"] + counts["newly_passing"]
                     + summary["suppressed_groups"])
        self.assertEqual(accounted, summary["accounted_groups"],
                         "emitted rows + stable passes + newly passing + "
                         "suppressed baseline rows must equal the accounted "
                         "group total")

    def test_fixture_summary_accounting(self):
        summary = compare_reports(
            load_report(fixture_path("baseline.json")),
            load_report(fixture_path("current.json")),
        )
        self.assert_group_accounting(summary)

    def test_synthetic_mixed_summary_accounting(self):
        summary = run_compare(
            [record("a", content="c1", status="failed", failure_category="parse:bad"),
             record("b", content="c2", status="passed"),
             record("c", content="c3", status="passed"),
             record("d", content="c4", parse_mode="strong", status="passed"),
             record("e", content="c5", status="skipped")],
            [record("a", content="c1", status="failed", failure_category="parse:bad"),
             record("b", content="c2", status="failed", failure_category="timeout:10s"),
             record("c", content="c3", status="passed"),
             record("c", content="c3", attempt=2, status="failed",
                    failure_category="parse:bad"),
             record("d", content="c4", status="failed",
                    failure_category="parse:new"),
             record("f", content="c6", status="skipped"),
             record("g", content="c7", status="passed")],
        )
        self.assert_group_accounting(summary)
        self.assertEqual(summary["counts"]["new_failures"], 1)        # b
        self.assertEqual(summary["counts"]["persistent_failures"], 1)  # a
        self.assertEqual(summary["counts"]["unstable"], 1)            # c
        self.assertEqual(summary["counts"]["scope_mismatch"], 1)      # d (strict)
        self.assertEqual(summary["counts"]["not_executed"], 1)        # f
        self.assertEqual(summary["counts"]["missing_from_current"], 1)  # e
        self.assertEqual(summary["counts"]["newly_passing"], 1)       # g
        self.assertEqual(summary["suppressed_groups"], 1)             # baseline d (strong)
        self.assertEqual(summary["accounted_groups"], 8)


class FixtureSummaryTests(unittest.TestCase):
    """Golden expectations for the delivered baseline/current sample pair."""

    @classmethod
    def setUpClass(cls):
        cls.summary = compare_reports(
            load_report(fixture_path("baseline.json")),
            load_report(fixture_path("current.json")),
        )

    def test_counts(self):
        self.assertEqual(self.summary["counts"], {
            "new_failures": 4,
            "fixed": 1,
            "persistent_failures": 4,
            "unstable": 1,
            "not_executed": 1,
            "missing_from_current": 1,
            "scope_mismatch": 1,
            "needs_confirmation": 1,
            "stable_passes": 2,
            "newly_passing": 1,
        })

    def test_compared_group_count(self):
        self.assertEqual(self.summary["compared_groups"], 13)

    def test_new_failures_are_named(self):
        names = {row["sample"] for row in rows_of(self.summary, "new_failures")}
        self.assertEqual(names, {"flaky-net", "skip-to-fail", "latency-probe", "brand-new"})

    def test_fixed_is_named(self):
        rows = rows_of(self.summary, "fixed")
        self.assertEqual([row["sample"] for row in rows], ["catastrophic-regex"])
        self.assertEqual(rows[0]["baseline_failure_categories"], ["parse:stack_overflow"])

    def test_persistent_are_named(self):
        names = {row["sample"] for row in rows_of(self.summary, "persistent_failures")}
        self.assertEqual(names, {"attr-quote", "utf8-bom", "crlf-normalize", "mixed-timeout"})

    def test_unstable_contains_single_pass_and_single_fail(self):
        rows = rows_of(self.summary, "unstable")
        self.assertEqual([row["sample"] for row in rows], ["nested-comment"])
        self.assertEqual([entry["status"] for entry in rows[0]["attempt_details"]],
                         ["passed", "failed"])

    def test_not_executed_is_counted_separately_from_stable_pass(self):
        rows = rows_of(self.summary, "not_executed")
        self.assertEqual([row["sample"] for row in rows], ["skip-me"])
        self.assertEqual(self.summary["counts"]["stable_passes"], 2)
        self.assertNotIn("skip-me",
                         {row["sample"] for row in
                          rows_of(self.summary, "missing_from_current")})

    def test_rename_matches_original_baseline_entry(self):
        # old-name-sample (baseline) and new-name-sample (current) carry the
        # same content, so the pair is a stable pass rather than a
        # new-sample + missing-baseline pair.
        all_emitted_names = {
            row["sample"]
            for rows in self.summary["sections"].values()
            for row in rows
        }
        self.assertNotIn("new-name-sample", all_emitted_names)
        self.assertNotIn("old-name-sample", all_emitted_names)
        self.assertEqual(self.summary["counts"]["new_failures"], 4)
        self.assertEqual(self.summary["counts"]["missing_from_current"], 1)

    def test_duplicate_attempt_is_pending_confirmation(self):
        rows = rows_of(self.summary, "needs_confirmation")
        self.assertEqual([row["sample"] for row in rows], ["dup-sample"])
        row = rows[0]
        self.assertEqual(row["status"], "pending_confirmation")
        self.assertIsNone(row["current_outcome"])
        self.assertEqual(row["conflicts"][0]["build"], "current")
        self.assertEqual(row["conflicts"][0]["attempt"], 1)
        self.assertEqual(len(row["conflicts"][0]["entries"]), 2)

    def test_scope_mismatch_not_compared(self):
        rows = rows_of(self.summary, "scope_mismatch")
        self.assertEqual([row["sample"] for row in rows], ["scope-test"])
        self.assertEqual(rows[0]["parse_mode"], "lenient")
        self.assertEqual(rows[0]["scope_evidence"][0]["parse_mode"], "strict")
        self.assertEqual(rows[0]["scope_evidence"][0]["outcome"], "failed")
        self.assertNotIn("scope-test",
                         {row["sample"] for row in rows_of(self.summary, "missing_from_current")})

    def test_timeout_origin_distinguishes_existing_and_new(self):
        by_name = {}
        for section in ("new_failures", "persistent_failures"):
            for row in rows_of(self.summary, section):
                by_name[row["sample"]] = row
        self.assertEqual(by_name["utf8-bom"]["timeout"], "pre_existing_timeout")
        self.assertEqual(by_name["attr-quote"]["timeout"], "no_timeout")
        self.assertEqual(by_name["flaky-net"]["timeout"], "new_timeout")
        self.assertEqual(by_name["latency-probe"]["timeout"], "new_timeout")

    def test_new_error_category_is_separated_from_existing_timeout(self):
        row = next(r for r in rows_of(self.summary, "persistent_failures")
                   if r["sample"] == "mixed-timeout")
        self.assertEqual(row["failure_categories"],
                         ["parse:out_of_memory", "timeout:30s"])
        self.assertEqual(row["new_failure_categories"], ["parse:out_of_memory"])
        self.assertEqual(row["pre_existing_failure_categories"], ["timeout:30s"])
        self.assertEqual(row["timeout"], "pre_existing_timeout")

    def test_missing_row_reports_baseline_build_only(self):
        rows = rows_of(self.summary, "missing_from_current")
        self.assertEqual([row["sample"] for row in rows], ["removed-sample"])
        self.assertIsNone(rows[0]["build_id"])
        self.assertEqual(rows[0]["baseline_build_id"], "base-2026-11-01")
        # The row must not pretend the current build ran the sample.
        self.assertEqual(rows[0]["attempts"], [])
        self.assertEqual(rows[0]["failure_categories"], [])
        self.assertEqual(rows[0]["timeout"], "not_applicable")
        self.assertEqual(rows[0]["current_outcome"], None)

    def test_scope_mismatch_row_does_not_borrow_current_side_data(self):
        row = rows_of(self.summary, "scope_mismatch")[0]
        self.assertEqual(row["attempts"], [1])
        self.assertEqual(row["failure_categories"], [])
        self.assertEqual(row["baseline_failure_categories"], [])
        # The other scope's outcome is preserved as evidence rather than being
        # merged into this row's comparison fields.
        self.assertEqual(row["scope_evidence"][0]["outcome"], "failed")
        self.assertEqual(row["scope_evidence"][0]["parse_mode"], "strict")

    def test_attempt_numbers_and_build_identifiers_are_present(self):
        row = next(r for r in rows_of(self.summary, "new_failures")
                   if r["sample"] == "brand-new")
        self.assertEqual(row["build_id"], "build-2026-11-14")
        self.assertEqual(row["attempts"], [1])
        self.assertEqual(row["environment"], "linux")

    def test_all_rows_carry_required_fields(self):
        required = {"summary", "parse_mode", "environment", "build_id",
                    "attempts", "failure_categories", "identity", "timeout"}
        for section, rows in self.summary["sections"].items():
            for row in rows:
                missing = required - set(row)
                self.assertEqual(missing, set(), f"{section}: missing {missing}")


class OutcomeRuleTests(unittest.TestCase):
    def test_single_pass_and_single_fail_is_unstable(self):
        summary = run_compare(
            [record("s", content="c", attempt=1, status="failed",
                    failure_category="parse:bad")],
            [record("s", content="c", attempt=1, status="passed"),
             record("s", content="c", attempt=2, status="failed",
                    failure_category="parse:bad")],
        )
        self.assertEqual(summary["counts"]["unstable"], 1)
        self.assertEqual(summary["counts"]["new_failures"], 0)

    def test_all_attempts_failed_is_failed_not_unstable(self):
        summary = run_compare(
            [record("s", content="c", attempt=1, status="passed")],
            [record("s", content="c", attempt=1, status="failed",
                    failure_category="parse:bad"),
             record("s", content="c", attempt=2, status="failed",
                    failure_category="parse:bad")],
        )
        self.assertEqual(summary["counts"]["new_failures"], 1)
        self.assertEqual(summary["counts"]["unstable"], 0)

    def test_all_skipped_is_not_executed_even_if_baseline_failed(self):
        summary = run_compare(
            [record("s", content="c", status="failed", failure_category="parse:bad")],
            [record("s", content="c", status="skipped")],
        )
        self.assertEqual(summary["counts"]["not_executed"], 1)
        self.assertEqual(summary["counts"]["fixed"], 0)
        self.assertEqual(summary["counts"]["persistent_failures"], 0)

    def test_skipped_plus_passed_is_a_pass_and_notes_the_skip(self):
        summary = run_compare(
            [record("s", content="c", status="passed")],
            [record("s", content="c", attempt=1, status="skipped"),
             record("s", content="c", attempt=2, status="passed")],
        )
        self.assertEqual(summary["counts"]["stable_passes"], 1)
        self.assertEqual(summary["counts"]["unstable"], 0)

    def test_newly_passing_is_costed_separately_from_stable_pass(self):
        summary = run_compare(
            [record("s", content="c", status="passed")],
            [record("s", content="c", status="passed"),
             record("t", content="d", status="passed")],
        )
        self.assertEqual(summary["counts"]["stable_passes"], 1)
        self.assertEqual(summary["counts"]["newly_passing"], 1)

    def test_failure_from_unstable_baseline_counts_as_persistent(self):
        summary = run_compare(
            [record("s", content="c", attempt=1, status="passed"),
             record("s", content="c", attempt=2, status="failed",
                    failure_category="parse:bad")],
            [record("s", content="c", attempt=1, status="failed",
                    failure_category="parse:bad")],
        )
        self.assertEqual(summary["counts"]["persistent_failures"], 1)
        self.assertEqual(summary["counts"]["unstable"], 0)


class ComparabilityRuleTests(unittest.TestCase):
    def test_different_parse_mode_is_not_compared(self):
        summary = run_compare(
            [record("s", content="c", parse_mode="strict", status="passed")],
            [record("s", content="c", parse_mode="lenient", status="failed",
                    failure_category="parse:bad")],
        )
        self.assertEqual(summary["counts"]["new_failures"], 0)
        self.assertEqual(summary["counts"]["scope_mismatch"], 1)
        self.assertEqual(summary["counts"]["missing_from_current"], 0)

    def test_different_environment_is_not_compared(self):
        summary = run_compare(
            [record("s", content="c", environment="linux", status="passed")],
            [record("s", content="c", environment="darwin", status="failed",
                    failure_category="parse:bad")],
        )
        self.assertEqual(summary["counts"]["new_failures"], 0)
        self.assertEqual(summary["counts"]["scope_mismatch"], 1)

    def test_removed_baseline_scope_does_not_emit_a_duplicate_missing_row(self):
        summary = run_compare(
            [record("s", content="c", parse_mode="strict", status="failed",
                    failure_category="parse:bad")],
            [record("s", content="c", parse_mode="lenient", status="passed")],
        )
        self.assertEqual(summary["counts"]["scope_mismatch"], 1)
        self.assertEqual(summary["counts"]["missing_from_current"], 0)
        self.assertEqual(summary["counts"]["fixed"], 0)

    def test_baseline_scope_is_not_marked_missing_when_current_scope_is_skipped(self):
        summary = run_compare(
            [record("s", content="c", parse_mode="strict", status="passed")],
            [record("s", content="c", parse_mode="lenient", status="skipped")],
        )
        # The lenient group is reported as not executed; the strict baseline
        # group must not silently disappear, and must not be called missing
        # either, because the content is still exported.
        self.assertEqual(summary["counts"]["not_executed"], 1)
        self.assertEqual(summary["counts"]["missing_from_current"], 0)
        self.assertEqual(summary["counts"]["scope_mismatch"], 1)
        baseline_row = next(row for row in summary["sections"]["scope_mismatch"]
                            if row["parse_mode"] == "strict")
        self.assertEqual(baseline_row["current_scope_evidence"][0]["parse_mode"],
                         "lenient")
        self.assertEqual(baseline_row["current_scope_evidence"][0]["outcome"],
                         "not_executed")

    def test_baseline_scope_still_reported_when_current_scope_conflicts(self):
        summary = run_compare(
            [record("s", content="c", parse_mode="strict", status="passed")],
            [record("s", content="c", parse_mode="lenient", attempt=1, status="passed"),
             record("s", content="c", parse_mode="lenient", attempt=1, status="failed",
                    failure_category="parse:bad")],
        )
        self.assertEqual(summary["counts"]["needs_confirmation"], 1)
        self.assertEqual(summary["counts"]["missing_from_current"], 0)
        self.assertEqual(summary["counts"]["scope_mismatch"], 1)

    def test_mode_and_environment_compare_case_insensitively(self):
        summary = run_compare(
            [record("s", content="c", parse_mode="Strict", environment="Linux",
                    status="passed")],
            [record("s", content="c", parse_mode="strict", environment="linux",
                    status="failed", failure_category="parse:bad")],
        )
        self.assertEqual(summary["counts"]["new_failures"], 1)
        self.assertEqual(summary["counts"]["scope_mismatch"], 0)

    def test_changed_content_with_same_name_is_a_new_sample(self):
        summary = run_compare(
            [record("same-name", content="old", status="passed")],
            [record("same-name", content="new", status="failed",
                    failure_category="parse:bad")],
        )
        self.assertEqual(summary["counts"]["new_failures"], 1)
        self.assertEqual(summary["counts"]["missing_from_current"], 1)

    def test_rename_with_unchanged_content_matches_baseline(self):
        summary = run_compare(
            [record("old-name", content="same", status="failed",
                    failure_category="parse:bad")],
            [record("new-name", content="same", status="passed")],
        )
        self.assertEqual(summary["counts"]["fixed"], 1)
        rows = summary["sections"]["fixed"]
        self.assertEqual(rows[0]["sample"], "new-name")
        self.assertEqual(rows[0]["baseline_sample"], "old-name")
        self.assertTrue(rows[0]["renamed"])


class ConflictRuleTests(unittest.TestCase):
    def test_duplicate_attempt_in_current_is_not_classified(self):
        summary = run_compare(
            [record("s", content="c", attempt=1, status="passed")],
            [record("s", content="c", attempt=1, status="passed"),
             record("s", content="c", attempt=1, status="failed",
                    failure_category="parse:bad")],
        )
        self.assertEqual(summary["counts"]["needs_confirmation"], 1)
        self.assertEqual(summary["counts"]["new_failures"], 0)
        self.assertEqual(summary["counts"]["unstable"], 0)
        self.assertEqual(summary["counts"]["fixed"], 0)

    def test_duplicate_attempt_in_baseline_is_reported(self):
        summary = run_compare(
            [record("s", content="c", attempt=1, status="passed"),
             record("s", content="c", attempt=1, status="failed",
                    failure_category="parse:bad")],
            [record("s", content="c", attempt=1, status="passed")],
        )
        self.assertEqual(summary["counts"]["needs_confirmation"], 1)
        self.assertEqual(summary["sections"]["needs_confirmation"][0]
                         ["conflicts"][0]["build"], "baseline")

    def test_distinct_attempt_numbers_do_not_conflict(self):
        summary = run_compare(
            [record("s", content="c", attempt=1, status="passed")],
            [record("s", content="c", attempt=1, status="passed"),
             record("s", content="c", attempt=2, status="failed",
                    failure_category="parse:bad")],
        )
        self.assertEqual(summary["counts"]["needs_confirmation"], 0)
        self.assertEqual(summary["counts"]["unstable"], 1)


class TimeoutRuleTests(unittest.TestCase):
    def test_resolved_timeout(self):
        summary = run_compare(
            [record("s", content="c", status="failed", failure_category="timeout:30s")],
            [record("s", content="c", status="passed")],
        )
        self.assertEqual(summary["counts"]["fixed"], 1)
        self.assertEqual(summary["sections"]["fixed"][0]["timeout"], "timeout_resolved")

    def test_non_timeout_category_containing_the_word_is_not_a_timeout(self):
        summary = run_compare(
            [record("s", content="c", status="failed",
                    failure_category="parse:timeout_handler")],
            [record("s", content="c", status="failed",
                    failure_category="parse:timeout_handler")],
        )
        self.assertEqual(summary["sections"]["persistent_failures"][0]["timeout"],
                         "no_timeout")

    def test_timeout_suffix_forms_are_recognized(self):
        for category in ("timeout", "timeout:30s", "Timeout/Read", "timeout.5s"):
            summary = run_compare(
                [record("s", content="c", status="passed")],
                [record("s", content="c", status="failed",
                        failure_category=category)],
            )
            self.assertEqual(summary["sections"]["new_failures"][0]["timeout"],
                             "new_timeout", category)


if __name__ == "__main__":
    unittest.main()
