"""Export the immutable legacy detector registry as a declarative source pack."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from src.patterns import DEFAULT_CONTEXT_RULES, DEFAULT_DETECTION_RULES

SOURCE_LOCK = "66924ea"

RULE_PROVENANCE = {
    "CRT-CI-010": {
        "source": "GitHub Docs — Script injections",
        "url": "https://docs.github.com/en/actions/concepts/security/script-injections",
        "license": "vendor-documentation",
    },
    "CRT-CI-011": {
        "source": "GitHub Docs — Script injections",
        "url": "https://docs.github.com/en/actions/concepts/security/script-injections",
        "license": "vendor-documentation",
    },
    "CRT-CI-012": {
        "source": "GitHub Docs — Script injections",
        "url": "https://docs.github.com/en/actions/concepts/security/script-injections",
        "license": "vendor-documentation",
    },
    "CRT-CI-013": {
        "source": "actions/checkout — clean input",
        "url": "https://github.com/actions/checkout#inputs",
        "license": "vendor-documentation",
    },
    "CRT-CI-014": {
        "source": "GitHub Docs — Self-hosted runners",
        "url": "https://docs.github.com/en/actions/concepts/runners/self-hosted-runners",
        "license": "vendor-documentation",
    },
    "CRT-CI-015": {
        "source": "GitHub Docs — Secure use reference",
        "url": "https://docs.github.com/en/actions/reference/security/secure-use",
        "license": "vendor-documentation",
    },
    "CRT-CI-016": {
        "source": "GitHub Docs — Self-hosted runners",
        "url": "https://docs.github.com/en/actions/concepts/runners/self-hosted-runners",
        "license": "vendor-documentation",
    },
    "CRT-CI-017": {
        "source": "GitHub Docs — Running jobs in a container",
        "url": "https://docs.github.com/en/actions/how-tos/write-workflows/choose-where-workflows-run/run-jobs-in-a-container",
        "license": "vendor-documentation",
    },
    "CRT-CI-018": {
        "source": "GitHub Docs — Events that trigger workflows",
        "url": "https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows",
        "license": "vendor-documentation",
    },
    "CRT-CI-019": {
        "source": "GitHub Docs — Events that trigger workflows",
        "url": "https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows",
        "license": "vendor-documentation",
    },
    "CRT-CI-020": {
        "source": "GitHub Docs — Using secrets in GitHub Actions",
        "url": "https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets",
        "license": "vendor-documentation",
    },
    "CRT-CI-021": {
        "source": "GitHub Docs — Securely using pull_request_target",
        "url": "https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target",
        "license": "vendor-documentation",
    },
    "CRT-CI-022": {
        "source": "GitHub Docs — Securely using pull_request_target",
        "url": "https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target",
        "license": "vendor-documentation",
    },
    "CRT-CI-023": {
        "source": "GitHub Docs — Events that trigger workflows",
        "url": "https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows",
        "license": "vendor-documentation",
    },
    "CRT-CI-024": {
        "source": "GitHub Docs — Controlling permissions for GITHUB_TOKEN",
        "url": "https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/controlling-permissions-for-github_token",
        "license": "vendor-documentation",
    },
    "CRT-CI-025": {
        "source": "GitHub Docs — Script injections",
        "url": "https://docs.github.com/en/actions/concepts/security/script-injections",
        "license": "vendor-documentation",
    },
    "CRT-CI-026": {
        "source": "GitHub Docs — Script injections",
        "url": "https://docs.github.com/en/actions/concepts/security/script-injections",
        "license": "vendor-documentation",
    },
    "CRT-CI-027": {
        "source": "GitHub Docs — Script injections",
        "url": "https://docs.github.com/en/actions/concepts/security/script-injections",
        "license": "vendor-documentation",
    },
    "CRT-CI-028": {
        "source": "GitHub Docs — Script injections",
        "url": "https://docs.github.com/en/actions/concepts/security/script-injections",
        "license": "vendor-documentation",
    },
    "CRT-CI-029": {
        "source": "GitHub Docs — Secure use reference",
        "url": "https://docs.github.com/en/actions/reference/security/secure-use",
        "license": "vendor-documentation",
    },
    "CRT-CI-030": {
        "source": "GitHub Security Lab — workflow_run head_branch injection examples",
        "url": "https://securitylab.github.com/advisories/GHSL-2024-274-GHSL-2024-275_Cilium/",
        "license": "vendor-documentation",
    },
    "CRT-CI-031": {
        "source": "GitHub Docs — Script injections",
        "url": "https://docs.github.com/en/actions/concepts/security/script-injections",
        "license": "vendor-documentation",
    },
    "CRT-CI-032": {
        "source": "GitHub Docs — Script injections",
        "url": "https://docs.github.com/en/actions/concepts/security/script-injections",
        "license": "vendor-documentation",
    },
    "CRT-CI-033": {
        "source": "GitHub Docs — Script injections",
        "url": "https://docs.github.com/en/actions/concepts/security/script-injections",
        "license": "vendor-documentation",
    },
    "CRT-CI-034": {
        "source": "GitHub Docs — Script injections",
        "url": "https://docs.github.com/en/actions/concepts/security/script-injections",
        "license": "vendor-documentation",
    },
    "CRT-CI-035": {
        "source": "GitHub Docs — Events that trigger workflows / contexts",
        "url": "https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows",
        "license": "vendor-documentation",
    },
    "CRT-CI-036": {
        "source": "GitHub Docs — Workflow syntax and script injections",
        "url": "https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax",
        "license": "vendor-documentation",
    },
    "CRT-CI-037": {
        "source": "GitHub Docs — Contexts reference",
        "url": "https://docs.github.com/en/actions/reference/workflows-and-actions/contexts",
        "license": "vendor-documentation",
    },
    "CRT-CI-038": {
        "source": "GitHub Docs — Script injections",
        "url": "https://docs.github.com/en/actions/concepts/security/script-injections",
        "license": "vendor-documentation",
    },
    "CRT-CI-039": {
        "source": "GitHub Docs — Script injections",
        "url": "https://docs.github.com/en/actions/concepts/security/script-injections",
        "license": "vendor-documentation",
    },
    "CRT-CI-040": {
        "source": "GitHub Docs — Script injections",
        "url": "https://docs.github.com/en/actions/concepts/security/script-injections",
        "license": "vendor-documentation",
    },
    "CRT-CI-041": {
        "source": "GitHub Docs — Script injections",
        "url": "https://docs.github.com/en/actions/concepts/security/script-injections",
        "license": "vendor-documentation",
    },
    "CRT-CI-042": {
        "source": "GitHub Docs — Script injections",
        "url": "https://docs.github.com/en/actions/concepts/security/script-injections",
        "license": "vendor-documentation",
    },
    "CRT-CI-043": {
        "source": "GitHub Docs — Script injections",
        "url": "https://docs.github.com/en/actions/concepts/security/script-injections",
        "license": "vendor-documentation",
    },
    "CRT-CI-044": {
        "source": "GitHub Docs — Script injections / workflow_run context",
        "url": "https://docs.github.com/en/actions/concepts/security/script-injections",
        "license": "vendor-documentation",
    },
    "CRT-CI-045": {
        "source": "GitHub Docs — OpenID Connect",
        "url": "https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-google-cloud-platform",
        "license": "vendor-documentation",
    },
    "CRT-CI-046": {
        "source": "GitHub Docs — Deployments and environments",
        "url": "https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments",
        "license": "vendor-documentation",
    },
    "CRT-CI-047": {
        "source": "GitHub Docs — Deployments and environments",
        "url": "https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments",
        "license": "vendor-documentation",
    },
    "CRT-CI-048": {
        "source": "GitHub Docs — GITHUB_TOKEN / contexts",
        "url": "https://docs.github.com/en/actions/concepts/security/github_token",
        "license": "vendor-documentation",
    },
    "CRT-CI-049": {
        "source": "GitHub Docs — Secure use reference / container actions",
        "url": "https://docs.github.com/en/actions/reference/security/secure-use",
        "license": "vendor-documentation",
    },
    "CRT-CI-050": {
        "source": "GitHub Docs — Secure use reference / pin actions",
        "url": "https://docs.github.com/en/actions/reference/security/secure-use",
        "license": "vendor-documentation",
    },
    "CRT-CI-051": {
        "source": "GitHub Docs — Secure use reference / pin actions",
        "url": "https://docs.github.com/en/actions/reference/security/secure-use",
        "license": "vendor-documentation",
    },
    "CRT-CI-052": {
        "source": "GitHub Docs — Secure use reference / pin actions",
        "url": "https://docs.github.com/en/actions/reference/security/secure-use",
        "license": "vendor-documentation",
    },
    "CRT-CI-053": {
        "source": "GitHub Docs — Secure use reference / pin actions",
        "url": "https://docs.github.com/en/actions/reference/security/secure-use",
        "license": "vendor-documentation",
    },
    "CRT-CI-054": {
        "source": "GitHub Docs — Secure use reference / pin actions",
        "url": "https://docs.github.com/en/actions/reference/security/secure-use",
        "license": "vendor-documentation",
    },
    "CRT-SEC-180": {
        "source": "Paddle Developer Docs — API key format",
        "url": "https://developer.paddle.com/api-reference/about/authentication/",
        "license": "vendor-documentation",
    },
    "CRT-SEC-181": {
        "source": "Paddle Developer Docs — webhook endpoint secret format",
        "url": "https://developer.paddle.com/api-reference/notification-settings/create-notification-setting/",
        "license": "vendor-documentation",
    },
    "CRT-SEC-182": {
        "source": "Cloudinary Documentation — API environment variable format",
        "url": "https://cloudinary.com/documentation/cloudinary_cli",
        "license": "vendor-documentation",
    },
    "CRT-SEC-183": {
        "source": "Microsoft Learn — Azure DevOps personal access token entity definition",
        "url": "https://learn.microsoft.com/en-us/purview/sit-defn-azure-devops-personal-access-token",
        "license": "vendor-documentation",
    },
    "CRT-SEC-184": {
        "source": "Confluent Cloud Documentation — API secret format and checksum",
        "url": "https://docs.confluent.io/cloud/current/security/authenticate/workload-identities/service-accounts/api-keys/overview.html",
        "license": "vendor-documentation",
    },
    "CRT-SEC-185": {
        "source": "Shopify Developer Changelog — access token length and prefix format",
        "url": "https://shopify.dev/changelog/length-of-the-shopify-access-token-is-increasing",
        "license": "vendor-documentation",
    },
    "CRT-SEC-186": {
        "source": "Shopify Developer Changelog — access token length and prefix format",
        "url": "https://shopify.dev/changelog/length-of-the-shopify-access-token-is-increasing",
        "license": "vendor-documentation",
    },
    "CRT-SEC-187": {
        "source": "Shopify Developer Changelog — access token length and prefix format",
        "url": "https://shopify.dev/changelog/length-of-the-shopify-access-token-is-increasing",
        "license": "vendor-documentation",
    },
    "CRT-SEC-188": {
        "source": "Shopify Developer Changelog — app secret key length and prefix format",
        "url": "https://shopify.dev/changelog/app-secret-key-length-has-increased",
        "license": "vendor-documentation",
    },
}


def provenance_for(rule_id: str) -> dict:
    provenance = RULE_PROVENANCE.get(rule_id, {
        "source": "CodeRiskTools legacy detection registry",
        "url": "https://coderisktools.invalid/source-registry",
        "license": "project-policy",
    }).copy()
    provenance["source_lock"] = SOURCE_LOCK
    return provenance


def build_pack() -> dict:
    rules = []
    for rule in DEFAULT_DETECTION_RULES:
        rules.append({
            "name": rule.name,
            "regex": rule.regex,
            "severity": rule.severity,
            "description": rule.description,
            "rule_id": rule.rule_id,
            "category": rule.category,
            "confidence": rule.confidence,
            "remediation": rule.remediation,
            "kind": rule.kind,
            "file_globs": list(rule.file_globs),
            "provenance": provenance_for(rule.rule_id),
        })
    context_rules = []
    for rule in DEFAULT_CONTEXT_RULES:
        context_rules.append({
            "name": rule.name,
            "required_regexes": list(rule.required_regexes),
            "max_line_span": rule.max_line_span,
            "severity": rule.severity,
            "description": rule.description,
            "rule_id": rule.rule_id,
            "category": rule.category,
            "confidence": rule.confidence,
            "remediation": rule.remediation,
            "kind": rule.kind,
            "file_globs": list(rule.file_globs),
            "provenance": provenance_for(rule.rule_id),
        })
    return {
        "schema": "coderisktools.rule-source-pack",
        "version": 2,
        "source_commit": SOURCE_LOCK,
        "detector_count": len(rules) + len(context_rules),
        "rules": rules,
        "context_rules": context_rules,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(build_pack(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
