#!/usr/bin/env python3
"""CLI entry point for the Secret/Config Diff Scanner."""

import argparse
import json
import os
import re
import sys
from pathlib import Path

from .scanner import SecretScanner, ScanResult
from .baseline import write_baseline
from .safeio import write_private_atomic
from . import __version__


def main():
    parser = argparse.ArgumentParser(
        prog="secret-scanner",
        description="Scan diffs, staged changes, and directories for secret-like literals and risky config changes.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # scan command
    scan_parser = subparsers.add_parser("scan", help="Scan for secrets and config changes")
    scan_group = scan_parser.add_mutually_exclusive_group(required=True)
    scan_group.add_argument("--diff", metavar="FILE", help="Unified diff file to scan")
    scan_group.add_argument("--staged", action="store_true", help="Scan git staged changes")
    scan_group.add_argument("--dir", metavar="DIR", help="Scan directory for secrets")
    scan_group.add_argument("--git-history", action="store_true", help="Scan bounded commit history in the current repository")
    scan_group.add_argument("--gitleaks-report", metavar="FILE", help="Import a strict redacted Gitleaks JSON/SARIF report")
    scan_group.add_argument("--gitleaks-binary", metavar="FILE", help="Run an explicitly supplied local Gitleaks executable")
    scan_parser.add_argument("--since-ref", metavar="REF", help="Only scan commits after REF (with --git-history)")
    scan_parser.add_argument("--max-commits", type=int, default=100, metavar="N", help="Maximum commits for --git-history (1-1000; default: 100)")

    scan_parser.add_argument("--format", choices=["json", "markdown", "html", "sarif", "github"],
                            default="json", help="Output format (default: json)")
    scan_parser.add_argument("--output", metavar="FILE", help="Write output to file instead of stdout")
    scan_parser.add_argument("--allowlist", metavar="FILE", help="Path to .secretsallowlist file")
    scan_parser.add_argument("--baseline", metavar="FILE", help="Path to strict finding baseline JSON")
    scan_parser.add_argument("--rule-pack", metavar="FILE", help="Path to an installed signed offline rule pack")
    scan_parser.add_argument("--rule-keyring", metavar="FILE", help="Path to the strict trusted public-key keyring")
    scan_parser.add_argument("--write-baseline", metavar="FILE", help="Atomically write fingerprints from this unsuppressed scan")
    scan_parser.add_argument("--force-baseline", action="store_true", help="Replace an existing regular baseline file")
    scan_parser.add_argument("--config", metavar="FILE", help="Path to severity-config.json")
    scan_parser.add_argument("--profile", choices=["balanced", "strict", "secrets-only"], default=None,
                            help="Policy profile; CLI selection overrides config profile")
    scan_parser.add_argument("--severity-threshold",
                            choices=["low", "medium", "high", "critical"],
                            default=None, help="Minimum severity; explicit CLI overrides config/profile")
    config_group = scan_parser.add_mutually_exclusive_group()
    config_group.add_argument("--config-check", dest="config_check", action="store_true", default=None,
                              help="Enable config detection; explicit CLI overrides config/profile")
    config_group.add_argument("--no-config-check", dest="config_check", action="store_false",
                              help="Disable config detection; explicit CLI overrides config/profile")
    scan_parser.add_argument("--recursive", action="store_true",
                            help="Recursively scan subdirectories (with --dir)")
    scan_parser.add_argument("--vulnerability-db", metavar="FILE",
                            help="Use an explicitly supplied local SQLite vulnerability database (with --dir)")
    scan_parser.add_argument("--vulnerability-baseline", metavar="FILE",
                            help="Suppress explicitly baselined vulnerability fingerprints (with --vulnerability-db)")
    scan_parser.add_argument("--write-vulnerability-baseline", metavar="FILE",
                            help="Write current vulnerability fingerprints to a local baseline file (with --vulnerability-db)")
    scan_parser.add_argument("--force-vulnerability-baseline", action="store_true",
                            help="Allow overwriting an existing vulnerability baseline")
    scan_parser.add_argument("--vulnerability-policy", metavar="FILE",
                            help="Apply explicit local vulnerability severity policy (with --vulnerability-db)")
    scan_parser.add_argument("--quiet", action="store_true",
                            help="Only output findings, no summary")

    osv_parser = subparsers.add_parser("osv-import", help="Import one explicitly supplied local OSV feed")
    osv_parser.add_argument("--input", required=True, metavar="FILE", help="Local OSV JSON file; no URLs are accepted")
    osv_parser.add_argument("--db", required=True, metavar="FILE", help="Target local SQLite vulnerability database")
    osv_parser.add_argument("--snapshot-id", required=True, metavar="ID")
    osv_parser.add_argument("--source-id", required=True, metavar="ID")
    osv_parser.add_argument("--activate", action="store_true", help="Activate the staged snapshot after successful import")
    osv_parser.add_argument("--keyring", metavar="FILE", help="Trusted offline Ed25519 keyring; requires a signed feed envelope")

    vuln_parser = subparsers.add_parser("vuln", help="Offline vulnerability inventory and scan operations")
    vuln_actions = vuln_parser.add_subparsers(dest="vuln_action", required=True)
    inventory_parser = vuln_actions.add_parser("inventory", help="Build a read-only local dependency inventory")
    inventory_parser.add_argument("--root", required=True, metavar="DIR")

    vuln_db_parser = subparsers.add_parser("vuln-db", help="Inspect the local vulnerability snapshot store")
    vuln_db_actions = vuln_db_parser.add_subparsers(dest="vuln_db_action", required=True)
    reconcile_parser = vuln_db_actions.add_parser("reconcile", help="Emit a read-only snapshot reconciliation report")
    reconcile_parser.add_argument("--root", required=True, metavar="DIR")
    reconcile_parser.add_argument("--active", required=True, metavar="PATH")
    reconcile_parser.add_argument("--output", metavar="FILE", help="Write the report atomically instead of stdout")
    status_parser = vuln_db_actions.add_parser("status", help="Show read-only snapshot-store status")
    status_parser.add_argument("--root", required=True, metavar="DIR")
    status_parser.add_argument("--active", required=True, metavar="PATH")
    verify_snapshot_parser = vuln_db_actions.add_parser("verify", help="Verify one staged or active snapshot")
    verify_snapshot_parser.add_argument("--snapshot", required=True, metavar="DIR")
    list_snapshots_parser = vuln_db_actions.add_parser("list-snapshots", help="List verified snapshot metadata")
    list_snapshots_parser.add_argument("--root", required=True, metavar="DIR")
    list_snapshots_parser.add_argument("--active", required=True, metavar="PATH")
    prune_parser = vuln_db_actions.add_parser("prune", help="Dry-run or explicitly prune old snapshots")
    prune_parser.add_argument("--root", required=True, metavar="DIR")
    prune_parser.add_argument("--active", required=True, metavar="PATH")
    prune_parser.add_argument("--keep-snapshot-id", action="append", default=[], metavar="ID")
    prune_parser.add_argument("--apply", action="store_true", help="Actually delete unprotected snapshots")
    rollback_parser = vuln_db_actions.add_parser("rollback", help="Explicitly roll back the active snapshot")
    rollback_parser.add_argument("--active", required=True, metavar="PATH")
    rollback_parser.add_argument("--target", required=True, metavar="DIR")
    rollback_parser.add_argument("--apply", action="store_true", help="Actually switch the active pointer")
    update_parser = vuln_db_actions.add_parser("update", help="Stage a local feed and optionally activate it")
    update_parser.add_argument("--input", required=True, metavar="FILE")
    update_parser.add_argument("--root", required=True, metavar="DIR")
    update_parser.add_argument("--source-id", required=True, metavar="ID")
    update_parser.add_argument("--snapshot-id", required=True, metavar="ID")
    update_parser.add_argument("--active", metavar="PATH")
    update_parser.add_argument("--apply", action="store_true", help="Actually switch the active pointer")
    source_status_parser = vuln_db_actions.add_parser("source-status", help="Show read-only source health metadata")
    source_status_parser.add_argument("--root", required=True, metavar="DIR")
    source_status_parser.add_argument("--active", required=True, metavar="PATH")
    database_info_parser = vuln_db_actions.add_parser("database-info", help="Show verified active database metadata")
    database_info_parser.add_argument("--active", required=True, metavar="PATH")
    fetch_parser = vuln_db_actions.add_parser("fetch", help="Explicitly fetch one allowlisted HTTPS JSON artifact")
    fetch_parser.add_argument("--url", required=True, metavar="URL")
    fetch_parser.add_argument("--allowed-host", action="append", required=True, metavar="HOST")
    fetch_parser.add_argument("--source-id", required=True, metavar="ID")
    fetch_parser.add_argument("--output", required=True, metavar="FILE")
    fetch_parser.add_argument("--provenance", metavar="FILE")
    fetch_parser.add_argument("--conditions", metavar="FILE", help="Verified provenance sidecar for conditional request headers")
    fetch_parser.add_argument("--etag", metavar="VALUE")
    fetch_parser.add_argument("--last-modified", metavar="VALUE")
    fetch_parser.add_argument("--max-bytes", type=int, default=64 * 1024 * 1024, metavar="N")
    fetch_parser.add_argument("--timeout", type=float, default=20.0, metavar="SECONDS")
    fetch_parser.add_argument("--max-attempts", type=int, default=1, metavar="N")
    fetch_parser.add_argument("--backoff-seconds", type=float, default=0.0, metavar="SECONDS")

    verify_parser = subparsers.add_parser("verify", help="Optionally verify one credential with explicit network consent")
    verify_parser.add_argument("--provider", required=True, choices=["github", "stripe"])
    verify_parser.add_argument("--credential-env", required=True, metavar="NAME", help="Environment variable holding the credential")
    verify_parser.add_argument("--consent-network", action="store_true", help="Consent to this run's allowlisted network request")

    rules_parser = subparsers.add_parser("rules", help="Install or roll back signed offline rule packs")
    rules_actions = rules_parser.add_subparsers(dest="rules_action", required=True)
    install_parser = rules_actions.add_parser("install")
    install_parser.add_argument("--source", required=True); install_parser.add_argument("--destination", required=True); install_parser.add_argument("--keyring", required=True)
    rollback_parser = rules_actions.add_parser("rollback")
    rollback_parser.add_argument("--destination", required=True); rollback_parser.add_argument("--keyring", required=True)

    hook_parser = subparsers.add_parser("hook", help="Scan a bounded AI-agent hook payload from stdin")
    hook_parser.add_argument("--agent", required=True, choices=["generic","codex","claude-code"])
    hook_parser.add_argument("--baseline", metavar="FILE")
    hook_parser.add_argument("--config", metavar="FILE")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(3)

    if args.command == "osv-import":
        from .vulnerability.database import VulnerabilityDatabase
        from .vulnerability.ingestion import ingest_osv_file
        try:
            database = VulnerabilityDatabase(args.db)
            try:
                report = ingest_osv_file(args.input, database, args.snapshot_id, args.source_id, activate=args.activate, keyring_path=args.keyring)
            finally:
                database.close()
        except (OSError, ValueError, RuntimeError) as exc:
            print(json.dumps({"state": "rejected", "errors": [str(exc)]}, indent=2))
            sys.exit(3)
        print(json.dumps(report.to_dict(), indent=2))
        sys.exit(0 if report.state in {"staged", "active"} else 3)

    if args.command == "vuln":
        from .vulnerability.inventory import build_inventory_report
        try:
            result = build_inventory_report(args.root)
        except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
            print(json.dumps({"state": "rejected", "errors": [str(exc)]}), file=sys.stderr)
            sys.exit(3)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        sys.exit(0)

    if args.command == "vuln-db":
        from .vulnerability.updater import (
            build_reconciliation_report,
            build_source_status_report,
            build_database_info,
            build_source_provenance,
            FetchConditions,
            FetchPolicy,
            fetch_json_artifact,
            load_fetch_conditions,
            persist_downloaded_artifact,
            prune_versioned_snapshots,
            stage_offline_update,
            verify_versioned_snapshot,
        )
        try:
            emit = True
            if args.vuln_db_action == "verify":
                result = verify_versioned_snapshot(args.snapshot)
            elif args.vuln_db_action == "rollback":
                if not args.apply:
                    raise ValueError("rollback requires explicit --apply")
                from .vulnerability.updater import rollback_versioned_snapshot
                result = rollback_versioned_snapshot(args.active, args.target)
            elif args.vuln_db_action == "update":
                result = stage_offline_update(
                    args.input,
                    args.root,
                    args.source_id,
                    args.snapshot_id,
                    args.active,
                    apply=args.apply,
                )
            elif args.vuln_db_action == "source-status":
                result = build_source_status_report(args.root, args.active)
            elif args.vuln_db_action == "database-info":
                result = build_database_info(args.active)
            elif args.vuln_db_action == "fetch":
                if args.conditions and (args.etag is not None or args.last_modified is not None):
                    raise ValueError("--conditions cannot be combined with --etag or --last-modified")
                conditions = load_fetch_conditions(args.conditions) if args.conditions else FetchConditions(etag=args.etag, last_modified=args.last_modified)
                artifact = fetch_json_artifact(
                    args.url,
                    FetchPolicy(
                        frozenset(args.allowed_host),
                        max_bytes=args.max_bytes,
                        timeout=args.timeout,
                        max_attempts=args.max_attempts,
                        backoff_seconds=args.backoff_seconds,
                    ),
                    conditions=conditions,
                )
                if artifact.not_modified:
                    result = {**build_source_provenance(args.source_id, artifact), "state": "not_modified"}
                else:
                    provenance_path = args.provenance or (args.output + ".provenance.json")
                    result = persist_downloaded_artifact(artifact, args.output, provenance_path, args.source_id)
            elif args.vuln_db_action == "prune":
                result = prune_versioned_snapshots(
                    args.root,
                    args.active,
                    set(args.keep_snapshot_id),
                    apply=args.apply,
                )
            else:
                reconciliation = build_reconciliation_report(args.root, args.active)
                if args.vuln_db_action == "list-snapshots":
                    result = {
                        "state": reconciliation["state"],
                        "active_snapshot_id": reconciliation["active_snapshot_id"],
                        "snapshot_ids": [item["snapshot_id"] for item in reconciliation["snapshots"]],
                        "snapshots": reconciliation["snapshots"],
                        "valid_snapshot_count": reconciliation["valid_snapshot_count"],
                        "invalid_snapshot_count": reconciliation["invalid_snapshot_count"],
                        "issues": reconciliation["issues"],
                        "report_sha256": reconciliation["report_sha256"],
                    }
                else:
                    result = reconciliation
                    if args.vuln_db_action == "reconcile" and args.output:
                        rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                        write_private_atomic(args.output, rendered.encode("utf-8"), "reconciliation report")
                        emit = False
            if emit:
                print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            print(json.dumps({"state": "rejected", "errors": [str(exc)]}), file=sys.stderr)
            sys.exit(3)
        if args.vuln_db_action in {"status", "list-snapshots", "source-status", "database-info"}:
            sys.exit(0 if result["state"] == "ok" else 3)
        if args.vuln_db_action == "verify":
            sys.exit(0)
        if args.vuln_db_action == "reconcile":
            sys.exit(0 if result["state"] == "ok" else 3)
        sys.exit(0)

    if args.command == "verify":
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", args.credential_env):
            print("Error: --credential-env must be a bounded uppercase environment name", file=sys.stderr)
            sys.exit(3)
        credential = os.environ.get(args.credential_env)
        if not credential:
            print("Error: credential environment variable is missing or empty", file=sys.stderr)
            sys.exit(3)
        from .verification import CredentialVerifier
        try:
            outcome = CredentialVerifier(enabled=True).verify(args.provider, credential, consent=args.consent_network)
        except (ValueError, RuntimeError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(3)
        print(json.dumps(outcome.to_dict(), indent=2))
        sys.exit(1 if outcome.status == "invalid" else (3 if outcome.status == "error" else 0))

    if args.command == "rules":
        from .rulepacks import install_rule_pack, load_trusted_keyring, rollback_rule_pack
        try:
            keys = load_trusted_keyring(args.keyring)
            if args.rules_action == "install":
                install_rule_pack(args.source, args.destination, keys)
            else:
                rollback_rule_pack(args.destination, keys)
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"Error: {exc}", file=sys.stderr); sys.exit(3)
        print(json.dumps({"status":"ok","action":args.rules_action}, indent=2)); sys.exit(0)

    if args.command == "hook":
        from .hooks import MAX_HOOK_BYTES, parse_hook_payload
        try:
            raw = sys.stdin.buffer.read(MAX_HOOK_BYTES + 1)
            diff = parse_hook_payload(args.agent, raw)
            scanner = SecretScanner(config_path=args.config, baseline_path=args.baseline)
            result = scanner.scan_diff_text(diff, source=f"agent-hook:{args.agent}")
            result.input_type = "agent-hook"
            print(result.to_json())
            sys.exit(result.exit_code)
        except (OSError, ValueError, RuntimeError):
            print("Error: invalid or unsafe agent hook payload", file=sys.stderr); sys.exit(3)

    if args.command == "scan":
        if args.force_baseline and not args.write_baseline:
            print("Error: --force-baseline requires --write-baseline", file=sys.stderr)
            sys.exit(3)
        if args.vulnerability_policy and not args.vulnerability_db:
            print("Error: --vulnerability-policy requires --vulnerability-db", file=sys.stderr)
            sys.exit(3)
        if (args.vulnerability_baseline or args.write_vulnerability_baseline or args.force_vulnerability_baseline) and not args.vulnerability_db:
            print("Error: vulnerability baseline options require --vulnerability-db", file=sys.stderr)
            sys.exit(3)
        if args.vulnerability_db and not args.dir:
            print("Error: --vulnerability-db requires --dir", file=sys.stderr)
            sys.exit(3)
        if (args.since_ref or args.max_commits != 100) and not args.git_history:
            print("Error: --since-ref and --max-commits require --git-history", file=sys.stderr)
            sys.exit(3)
        if (args.rule_pack is None) != (args.rule_keyring is None):
            print("Error: --rule-pack and --rule-keyring must be provided together", file=sys.stderr)
            sys.exit(3)
        if args.baseline and args.write_baseline:
            print("Error: --baseline and --write-baseline are mutually exclusive", file=sys.stderr)
            sys.exit(3)
        if args.output and args.write_baseline:
            if Path(args.output).resolve(strict=False) == Path(args.write_baseline).resolve(strict=False):
                print("Error: --output and --write-baseline must use different files", file=sys.stderr)
                sys.exit(3)
        try:
            trusted_rule_keys = None
            if args.rule_keyring:
                from .rulepacks import load_trusted_keyring
                trusted_rule_keys = load_trusted_keyring(args.rule_keyring)
            scanner = SecretScanner(
                config_path=args.config,
                allowlist_path=args.allowlist,
                severity_threshold=args.severity_threshold,
                config_check=args.config_check,
                profile=args.profile,
                baseline_path=args.baseline,
                rule_pack_path=args.rule_pack,
                trusted_rule_keys=trusted_rule_keys,
            )
        except (FileNotFoundError, ValueError, RuntimeError) as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(3)

        try:
            if args.diff:
                result = scanner.scan_diff(args.diff)
            elif args.staged:
                result = scanner.scan_staged()
            elif args.dir:
                result = scanner.scan_directory(
                    args.dir,
                    recursive=args.recursive,
                    vulnerability_db_path=args.vulnerability_db,
                    vulnerability_baseline_path=args.vulnerability_baseline,
                    write_vulnerability_baseline_path=args.write_vulnerability_baseline,
                    force_vulnerability_baseline=args.force_vulnerability_baseline,
                    vulnerability_policy_path=args.vulnerability_policy,
                )
            elif args.git_history:
                result = scanner.scan_git_history(".", since_ref=args.since_ref, max_commits=args.max_commits)
            elif args.gitleaks_report:
                result = scanner.scan_gitleaks_report(args.gitleaks_report)
            elif args.gitleaks_binary:
                result = scanner.scan_gitleaks_binary(args.gitleaks_binary, ".")
            else:
                print("Error: specify one supported scan source", file=sys.stderr)
                sys.exit(3)
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(3)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(3)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(3)

        if args.write_baseline:
            try:
                write_baseline(
                    args.write_baseline,
                    (finding.fingerprint for finding in result.findings),
                    overwrite=args.force_baseline,
                )
            except (OSError, ValueError) as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(3)

        # Format output
        if args.format == "json":
            output = result.to_json()
        elif args.format == "markdown":
            output = result.to_markdown()
        elif args.format == "html":
            output = result.to_html()
        elif args.format == "sarif":
            output = result.to_sarif()
        elif args.format == "github":
            output = result.to_github()
        else:
            output = result.to_json()

        # Quiet mode: only findings, no summary
        if args.quiet and args.format == "json":
            import json as json_mod
            data = json_mod.loads(output)
            data.pop("summary", None)
            output = json_mod.dumps(data, indent=2)
        elif args.quiet and args.format == "html":
            lines = output.split("\n")
            filtered = []
            skip = False
            for line in lines:
                if line == "<div class='summary'>":
                    skip = True
                    continue
                if skip and line == "</div>":
                    skip = False
                    continue
                if not skip:
                    filtered.append(line)
            output = "\n".join(filtered)
        elif args.quiet and args.format == "markdown":
            # Remove summary section from markdown
            lines = output.split("\n")
            filtered = []
            skip = False
            for line in lines:
                if line.startswith("## Summary"):
                    skip = True
                    continue
                if skip and line.startswith("## "):
                    skip = False
                if not skip:
                    filtered.append(line)
            output = "\n".join(filtered)

        # Write output
        if args.output:
            try:
                write_private_atomic(args.output, output.encode("utf-8"), "scan report")
            except (OSError, ValueError) as exc:
                print(f"Error: {exc}", file=sys.stderr)
                sys.exit(3)
        else:
            print(output)

        # Exit code
        sys.exit(result.exit_code)


if __name__ == "__main__":
    main()