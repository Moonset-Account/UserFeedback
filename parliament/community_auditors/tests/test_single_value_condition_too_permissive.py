import unittest

from nose.tools import assert_equal, assert_in

from parliament import analyze_policy_string


class TestSingleValueConditionTooPermissive(unittest.TestCase):
    """Test class for single value condition too permissive auditor"""

    # Statements without a Condition element (at the start and in the middle)
    # are valid and must not stop the auditor from checking the statements
    # that do have conditions.
    example_policy_string = """{
      "Version": "2012-10-17",
      "Statement": [
        {
          "Sid": "NoConditionFirst",
          "Effect": "Allow",
          "Action": "s3:GetObject",
          "Resource": "arn:aws:s3:::secretbucket/*"
        },
        {
          "Sid": "ForAllValuesCondition",
          "Effect": "Allow",
          "Action": "s3:GetObject",
          "Resource": "arn:aws:s3:::secretbucket/*",
          "Condition": {
            "ForAllValues:StringEquals": {
              "aws:ResourceTag/Tag": ["Value"]
            }
          }
        },
        {
          "Sid": "NoConditionMiddle",
          "Effect": "Allow",
          "Action": "s3:GetObject",
          "Resource": "arn:aws:s3:::secretbucket/*"
        },
        {
          "Sid": "ForAnyValuesCondition",
          "Effect": "Allow",
          "Action": "s3:GetObject",
          "Resource": "arn:aws:s3:::secretbucket/*",
          "Condition": {
            "ForAnyValues:StringEquals": {
              "aws:SourceIp": ["10.0.0.0/8"]
            }
          }
        }
      ]
    }"""

    def test_statements_without_condition_do_not_abort_audit(self):
        policy = analyze_policy_string(
            self.example_policy_string, include_community_auditors=True
        )

        # The statements without a Condition produce no findings from this
        # auditor (and no EXCEPTION finding from a crashed auditor), while
        # both conditional statements are still checked.
        assert_equal(policy.finding_ids, set(["SINGLE_VALUE_CONDITION_TOO_PERMISSIVE"]))

        findings = [
            f
            for f in policy.findings
            if f.issue == "SINGLE_VALUE_CONDITION_TOO_PERMISSIVE"
        ]
        assert_equal(len(findings), 2)

        # Each finding remains locatable: it points at the line of the
        # statement that contains the offending condition. The statement
        # node starts at the "{" line, right before its "Sid" line.
        expected_lines = []
        for lineno, line in enumerate(self.example_policy_string.splitlines(), 1):
            if '"Sid": "ForAllValuesCondition"' in line:
                expected_lines.append(lineno - 1)
            if '"Sid": "ForAnyValuesCondition"' in line:
                expected_lines.append(lineno - 1)

        for finding in findings:
            assert_in("lineno", finding.location)
            assert_in("column", finding.location)
        assert_equal(
            sorted(f.location["lineno"] for f in findings), sorted(expected_lines)
        )
