"""Core scanning engine for the Secret/Config Diff Scanner."""

import fnmatch
import hashlib
import json
import posixpath
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .patterns import (
    SecretPattern, ConfigCategory,
    DEFAULT_SECRET_PATTERNS, DEFAULT_DETECTION_RULES, DEFAULT_CONFIG_CATEGORIES,
    match_secret, match_rules, match_rules_all, match_context_rules, classify_config_file, validate_rule_registry,
    is_explicit_placeholder,
)
from .diff_parser import DiffFile, DiffLine, parse_diff, get_target_path
from .allowlist import AllowlistRule, load_allowlist, is_suppressed
from .config import ScannerConfig, load_config, apply_overrides, resolve_policy
from .baseline import load_baseline
from .safeio import read_regular_bounded
from .engine import RuleRegistry


MAX_DIFF_BYTES = 4 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_LINE_CHARS = 256 * 1024


@dataclass
class Finding:
    """A single finding from a scan."""
    type: str           # "secret" or "config"
    pattern_name: str   # e.g. "AWS_SECRET_KEY", "ENV_CONFIG_CHANGE"
    severity: str       # "critical", "high", "medium", "low"
    file: str           # File path
    line: int           # Line number (0 for file-level)
    matched_text: str   # The matched text
    line_content: str   # Full line content
    rule: str           # Stable generic export rule
    rule_id: str = "CRT-SEC-000"
    category: str = "secret"
    confidence: str = "medium"
    remediation: str = "Review and remediate the finding."
    identity_path: Optional[str] = None

    @property
    def fingerprint(self) -> str:
        """Return a line-stable, secret-safe identity for baseline matching."""
        path = posixpath.normpath((self.identity_path or self.file).replace("\\", "/"))
        if re.match(r"^[A-Z]:", path):
            path = path[0].lower() + path[1:]
        evidence = " ".join(self.matched_text.replace("\r", "\n").split())
        payload = "\0".join(("coderisktools-finding-v1", self.rule_id, path, evidence))
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class ConfigChange:
    """A config change detected in a diff."""
    type: str           # "config"
    file: str           # File path
    severity: str       # Severity level
    change_type: str    # "modified", "added", "deleted"
    description: str    # Human-readable description


@dataclass
class ScanResult:
    """Result of a scan operation."""
    scanner: str            # "secret-config-diff-scanner"
    version: str            # Version string
    timestamp: str          # ISO timestamp
    input_type: str         # "diff", "staged", "directory"
    input_source: str       # File path or "staged" or directory
    findings: list[Finding] = field(default_factory=list)
    config_changes: list[ConfigChange] = field(default_factory=list)
    fail_on_severity: str = "low"
    warn_on_severity: str = "low"
    baseline_suppressed: int = 0
    baseline_total: int = 0
    baseline_matched: int = 0
    baseline_stale: int = 0
    vulnerability_findings: list[dict] = field(default_factory=list)
    vulnerability_baseline_total: int = 0
    vulnerability_baseline_matched: int = 0
    vulnerability_baseline_suppressed: int = 0
    vulnerability_baseline_stale: int = 0
    vulnerability_policy_evaluated: bool = False
    vulnerability_policy_failed: int = 0

    _severity_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}

    @property
    def summary(self) -> dict:
        """Return a summary dict of findings."""
        severity_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        secret_count = 0
        policy_count = 0
        config_count = 0
        vulnerability_count = len(self.vulnerability_findings)

        for f in self.findings:
            severity_counts[f.severity] = severity_counts.get(f.severity, 0) + 1
            if f.type == "secret":
                secret_count += 1
            elif f.type == "policy":
                policy_count += 1

        for c in self.config_changes:
            severity_counts[c.severity] = severity_counts.get(c.severity, 0) + 1
            config_count += 1

        all_severities = [item.severity for item in self.findings] + [item.severity for item in self.config_changes]
        fail_rank = self._severity_order[self.fail_on_severity]
        warn_rank = self._severity_order[self.warn_on_severity]
        failing_count = sum(self._severity_order.get(level, 0) >= fail_rank for level in all_severities)
        warning_count = sum(warn_rank <= self._severity_order.get(level, 0) < fail_rank for level in all_severities)
        return {
            "total_findings": len(self.findings) + len(self.config_changes),
            "critical": severity_counts.get("critical", 0),
            "high": severity_counts.get("high", 0),
            "medium": severity_counts.get("medium", 0),
            "low": severity_counts.get("low", 0),
            "secret_findings": secret_count,
            "policy_findings": policy_count,
            "config_findings": config_count,
            "vulnerability_findings": vulnerability_count,
            "vulnerability_baseline_total": self.vulnerability_baseline_total,
            "vulnerability_baseline_matched": self.vulnerability_baseline_matched,
            "vulnerability_baseline_suppressed": self.vulnerability_baseline_suppressed,
            "vulnerability_baseline_stale": self.vulnerability_baseline_stale,
            "vulnerability_policy_evaluated": self.vulnerability_policy_evaluated,
            "vulnerability_policy_failed": self.vulnerability_policy_failed,
            "failing_findings": failing_count,
            "warning_findings": warning_count,
            "baseline_suppressed": self.baseline_suppressed,
            "baseline_total": self.baseline_total,
            "baseline_matched": self.baseline_matched,
            "baseline_stale": self.baseline_stale,
        }

    def has_secrets(self) -> bool:
        """Return True if any secret findings exist."""
        return any(f.type == "secret" for f in self.findings)

    def has_config_changes(self) -> bool:
        """Return True if any config changes exist."""
        return len(self.config_changes) > 0

    def to_json(self) -> str:
        """Return JSON formatted output."""
        from .formatters import format_json
        return format_json(self)

    def to_markdown(self) -> str:
        """Return Markdown formatted output."""
        from .formatters import format_markdown
        return format_markdown(self)

    def to_html(self) -> str:
        """Return HTML formatted output."""
        from .formatters import format_html
        return format_html(self)

    def to_sarif(self) -> str:
        """Return SARIF formatted output."""
        from .formatters import format_sarif
        return format_sarif(self)

    def to_github(self) -> str:
        """Return GitHub Actions workflow annotations."""
        from .formatters import format_github
        return format_github(self)

    @property
    def exit_code(self) -> int:
        """Return exit code based on findings.
        0 = clean, 1 = secrets detected, 2 = config changes only, 3 = error
        """
        fail_rank = self._severity_order[self.fail_on_severity]
        if any(self._severity_order.get(f.severity, 0) >= fail_rank for f in self.findings if f.type == "secret"):
            return 1
        if any(self._severity_order.get(f.severity, 0) >= fail_rank for f in self.findings if f.type == "policy"):
            return 2
        if any(self._severity_order.get(c.severity, 0) >= fail_rank for c in self.config_changes):
            return 2
        if self.vulnerability_findings and (not self.vulnerability_policy_evaluated or self.vulnerability_policy_failed):
            return 2
        return 0


class SecretScanner:
    """Main scanner class for detecting secrets and config changes."""

    def __init__(
        self,
        config_path: Optional[str] = None,
        allowlist_path: Optional[str] = None,
        severity_threshold: Optional[str] = None,
        config_check: Optional[bool] = None,
        profile: Optional[str] = None,
        baseline_path: Optional[str] = None,
        rule_pack_path: Optional[str] = None,
        rule_pack_paths: Optional[list[str]] = None,
        trusted_rule_keys: Optional[dict[str, bytes]] = None,
    ):
        self.config = load_config(config_path) if config_path else ScannerConfig()
        self.allowlist_rules: list[AllowlistRule] = (
            load_allowlist(allowlist_path) if allowlist_path else []
        )
        self.profile = profile or self.config.profile
        defaults = resolve_policy(self.profile)
        self.severity_threshold = severity_threshold or self.config.severity_threshold or defaults["severity_threshold"]
        self.config_check = config_check if config_check is not None else (self.config.config_check if self.config.config_check is not None else defaults["config_check"])
        self.fail_on_severity = self.config.fail_on_severity
        self.warn_on_severity = self.config.warn_on_severity
        self.baseline_fingerprints = load_baseline(baseline_path) if baseline_path else set()

        # Apply overrides, preserve metadata, and add custom patterns.
        validate_rule_registry(DEFAULT_DETECTION_RULES)
        self.patterns = apply_overrides(DEFAULT_DETECTION_RULES, self.config)
        if rule_pack_path:
            if not trusted_rule_keys:
                raise ValueError("A trusted public-key keyring is required for a rule pack")
            from .rulepacks import load_rule_pack
            packed_rules = load_rule_pack(rule_pack_path, trusted_rule_keys)
            existing_ids = {rule.rule_id for rule in self.patterns}
            if any(rule.rule_id in existing_ids for rule in packed_rules):
                raise ValueError("Signed rule pack conflicts with an existing rule ID")
            self.patterns.extend(packed_rules)
        if rule_pack_paths:
            if rule_pack_path:
                raise ValueError("Use either rule_pack_path or rule_pack_paths, not both")
            if not trusted_rule_keys:
                raise ValueError("A trusted public-key keyring is required for rule packs")
            from .rulepacks import load_rule_packs
            packed_rules = load_rule_packs(rule_pack_paths, trusted_rule_keys)
            existing_ids = {rule.rule_id for rule in self.patterns}
            if any(rule.rule_id in existing_ids for rule in packed_rules):
                raise ValueError("Rule packs conflict with an existing rule ID")
            self.patterns.extend(packed_rules)

        self.registry = RuleRegistry(self.patterns)

        # Severity order for threshold filtering
        self._severity_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}

    def _above_threshold(self, severity: str) -> bool:
        """Check if a severity level is at or above the threshold."""
        return self._severity_order[severity] >= self._severity_order[self.severity_threshold]

    @staticmethod
    def _path_matches(path: str, patterns: list[str]) -> bool:
        normalized = path.replace("\\", "/").lstrip("./")
        return any(fnmatch.fnmatchcase(normalized, pattern) or (pattern.startswith("**/") and fnmatch.fnmatchcase(normalized, pattern[3:])) for pattern in patterns)

    def _is_ignored(self, path: str) -> bool:
        return self._path_matches(path, self.config.ignore_paths)

    def _path_severity(self, path: str, severity: str) -> str:
        if self._path_matches(path, self.config.high_severity_paths):
            return "high" if self._severity_order[severity] < self._severity_order["high"] else severity
        if self._path_matches(path, self.config.medium_severity_paths):
            return "medium" if self._severity_order[severity] < self._severity_order["medium"] else severity
        return severity

    def _apply_baseline(self, result: ScanResult) -> ScanResult:
        result.baseline_total = len(self.baseline_fingerprints)
        if self.baseline_fingerprints:
            finding_fingerprints = {finding.fingerprint for finding in result.findings}
            matched = self.baseline_fingerprints & finding_fingerprints
            retained = [
                finding for finding in result.findings
                if finding.fingerprint not in self.baseline_fingerprints
            ]
            result.baseline_suppressed = len(result.findings) - len(retained)
            result.baseline_matched = len(matched)
            result.baseline_stale = result.baseline_total - result.baseline_matched
            result.findings = retained
        return result

    def scan_diff(self, diff_path: str) -> ScanResult:
        """Scan a unified diff file for secrets and config changes."""
        filepath = Path(diff_path)
        if not filepath.exists():
            raise FileNotFoundError(f"Diff file not found: {diff_path}")

        if filepath.is_dir():
            raise ValueError(f"Path is a directory, not a file: {diff_path}. Use --dir mode instead.")

        try:
            raw = read_regular_bounded(filepath, MAX_DIFF_BYTES, "diff input")
            diff_text = raw.decode("utf-8", errors="replace")
        except (OSError, ValueError) as exc:
            raise ValueError("Diff input cannot be safely read or exceeds the byte limit.") from exc

        if not diff_text.strip():
            from . import __version__
            result = ScanResult(
                scanner="secret-config-diff-scanner",
                version=__version__,
                timestamp=datetime.now(timezone.utc).isoformat(),
                input_type="diff",
                input_source=diff_path,
                findings=[],
                config_changes=[],
                fail_on_severity=self.fail_on_severity,
                warn_on_severity=self.warn_on_severity,
            )
            return self._apply_baseline(result)

        diff_files = parse_diff(diff_text)

        from . import __version__
        result = ScanResult(
            scanner="secret-config-diff-scanner",
            version=__version__,
            timestamp=datetime.now(timezone.utc).isoformat(),
            input_type="diff",
            input_source=diff_path,
            fail_on_severity=self.fail_on_severity,
            warn_on_severity=self.warn_on_severity,
        )

        for diff_file in diff_files:
            target = get_target_path(diff_file)
            self._scan_diff_file(diff_file, target, result)

        return self._apply_baseline(result)

    def scan_diff_text(self, diff_text: str, source: str = "memory") -> ScanResult:
        """Scan supplied bounded unified-diff text without creating a temporary file."""
        if not isinstance(diff_text, str):
            raise ValueError("diff input must be text")
        if len(diff_text.encode("utf-8")) > MAX_DIFF_BYTES:
            raise ValueError("diff input exceeds byte limit")
        from . import __version__
        result = ScanResult(
            scanner="secret-config-diff-scanner",
            version=__version__,
            timestamp=datetime.now(timezone.utc).isoformat(),
            input_type="diff",
            input_source=source,
            fail_on_severity=self.fail_on_severity,
            warn_on_severity=self.warn_on_severity,
        )
        for diff_file in parse_diff(diff_text) if diff_text.strip() else []:
            target = get_target_path(diff_file)
            self._scan_diff_file(diff_file, target, result)
        return self._apply_baseline(result)

    def scan_git_history(self, repo_path: str = ".", since_ref: Optional[str] = None, max_commits: int = 100) -> ScanResult:
        """Scan bounded commit diffs while keeping commit evidence out of findings."""
        from . import __version__
        from .git_history import collect_history_diffs
        history = collect_history_diffs(repo_path, since_ref=since_ref, max_commits=max_commits)
        combined = ScanResult(
            scanner="secret-config-diff-scanner", version=__version__,
            timestamp=datetime.now(timezone.utc).isoformat(), input_type="git-history",
            input_source=f"git-history:{Path(repo_path).resolve().name}",
            fail_on_severity=self.fail_on_severity, warn_on_severity=self.warn_on_severity,
        )
        baseline = self.baseline_fingerprints
        self.baseline_fingerprints = set()
        try:
            findings = {}
            config_changes = {}
            for _commit, diff_text in history:
                partial = self.scan_diff_text(diff_text, source="git-history")
                for finding in partial.findings:
                    findings.setdefault(finding.fingerprint, finding)
                for change in partial.config_changes:
                    key = (change.type, change.file, change.severity, change.change_type, change.description)
                    config_changes.setdefault(key, change)
            combined.findings = [findings[key] for key in sorted(findings)]
            combined.config_changes = [config_changes[key] for key in sorted(config_changes)]
        finally:
            self.baseline_fingerprints = baseline
        return self._apply_baseline(combined)

    def scan_gitleaks_report(self, report_path: str) -> ScanResult:
        """Import a strict redacted Gitleaks report into the shared finding schema."""
        from . import __version__
        from .adapters.gitleaks import import_gitleaks_report
        result = ScanResult(
            scanner="secret-config-diff-scanner", version=__version__,
            timestamp=datetime.now(timezone.utc).isoformat(), input_type="gitleaks-report",
            input_source=Path(report_path).name, findings=import_gitleaks_report(report_path),
            fail_on_severity=self.fail_on_severity, warn_on_severity=self.warn_on_severity,
        )
        return self._apply_baseline(result)

    def scan_gitleaks_binary(self, binary_path: str, repo_path: str = ".") -> ScanResult:
        """Run an explicitly supplied local Gitleaks binary and normalize its redacted output."""
        from . import __version__
        from .adapters.gitleaks import run_gitleaks
        findings, version = run_gitleaks(binary_path, repo_path)
        result = ScanResult(
            scanner="secret-config-diff-scanner", version=__version__,
            timestamp=datetime.now(timezone.utc).isoformat(), input_type="gitleaks-binary",
            input_source=f"gitleaks:{version}", findings=findings,
            fail_on_severity=self.fail_on_severity, warn_on_severity=self.warn_on_severity,
        )
        return self._apply_baseline(result)

    def scan_staged(self) -> ScanResult:
        """Scan bounded git staged changes without executing diff helpers."""
        from .gitdiff import GitDiffError, collect_git_diff
        try:
            raw = collect_git_diff(staged=True, max_bytes=MAX_DIFF_BYTES)
        except GitDiffError as exc:
            raise ValueError(str(exc)) from exc
        diff_text = raw.decode("utf-8", errors="replace")
        if not diff_text.strip():
            from . import __version__
            return ScanResult(
                scanner="secret-config-diff-scanner",
                version=__version__,
                timestamp=datetime.now(timezone.utc).isoformat(),
                input_type="staged",
                input_source="staged",
                findings=[],
                config_changes=[],
                fail_on_severity=self.fail_on_severity,
                warn_on_severity=self.warn_on_severity,
            )

        diff_files = parse_diff(diff_text)

        from . import __version__
        result = ScanResult(
            scanner="secret-config-diff-scanner",
            version=__version__,
            timestamp=datetime.now(timezone.utc).isoformat(),
            input_type="staged",
            input_source="staged",
            fail_on_severity=self.fail_on_severity,
            warn_on_severity=self.warn_on_severity,
        )

        for diff_file in diff_files:
            target = get_target_path(diff_file)
            self._scan_diff_file(diff_file, target, result)

        return self._apply_baseline(result)

    def scan_directory(
        self,
        directory: str,
        recursive: bool = False,
        vulnerability_db_path: Optional[str] = None,
        vulnerability_baseline_path: Optional[str] = None,
        write_vulnerability_baseline_path: Optional[str] = None,
        force_vulnerability_baseline: bool = False,
        vulnerability_policy_path: Optional[str] = None,
    ) -> ScanResult:
        """Scan directory files for secrets (not diffs, actual file content)."""
        dirpath = Path(directory)
        if dirpath.is_symlink():
            raise ValueError("Directory input must not be a symlink.")
        if not dirpath.is_dir():
            if dirpath.exists():
                raise ValueError(f"Path is not a directory: {directory}. Use --diff mode for files.")
            raise FileNotFoundError(f"Directory not found: {directory}")

        from . import __version__
        result = ScanResult(
            scanner="secret-config-diff-scanner",
            version=__version__,
            timestamp=datetime.now(timezone.utc).isoformat(),
            input_type="directory",
            input_source=directory,
            fail_on_severity=self.fail_on_severity,
            warn_on_severity=self.warn_on_severity,
        )

        pattern = "**/*" if recursive else "*"
        for filepath in sorted(dirpath.glob(pattern)):
            if filepath.is_symlink():
                continue
            relative_path = filepath.relative_to(dirpath).as_posix()
            relative_parts = Path(relative_path).parts[:-1]
            parent = dirpath
            unsafe_parent = False
            for part in relative_parts:
                parent = parent / part
                if parent.is_symlink():
                    unsafe_parent = True
                    break
            if unsafe_parent or not filepath.is_file():
                continue
            if any(part in {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__"} for part in relative_parts):
                continue
            if self._is_ignored(relative_path):
                continue
            # Skip binary-ish files. Security-relevant dotfiles such as .env
            # and .npmrc must be scanned; ignored metadata directories remain
            # controlled by the configured ignore paths.
            if filepath.suffix in (
                ".pyc", ".pyo", ".so", ".o", ".a", ".exe", ".dll",
                ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".ico",
                ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
                ".mp3", ".mp4", ".avi", ".mov", ".wav",
                ".pdf", ".doc", ".docx", ".xls", ".xlsx",
                ".woff", ".woff2", ".ttf", ".eot",
            ):
                continue

            # Check for secrets in file content
            self._scan_file_content(filepath, result, relative_path)

            # Check for config file classification
            if self.config_check:
                self._classify_config(filepath, "modified", result, relative_path)

        if vulnerability_db_path:
            from .vulnerability.baseline import load_vulnerability_baseline, write_vulnerability_baseline
            from .vulnerability.database import VulnerabilityDatabase
            from .vulnerability.pipeline import scan_inventory_with_baseline
            baseline = load_vulnerability_baseline(vulnerability_baseline_path) if vulnerability_baseline_path else set()
            database = VulnerabilityDatabase(vulnerability_db_path)
            try:
                vulnerability_result = scan_inventory_with_baseline(directory, database, baseline)
                result.vulnerability_findings = vulnerability_result.findings
                result.vulnerability_baseline_total = vulnerability_result.total
                result.vulnerability_baseline_matched = vulnerability_result.matched
                result.vulnerability_baseline_suppressed = vulnerability_result.suppressed
                result.vulnerability_baseline_stale = vulnerability_result.stale
                if vulnerability_policy_path:
                    from .vulnerability.policy import load_vulnerability_policy, policy_fails
                    policy = load_vulnerability_policy(vulnerability_policy_path)
                    result.vulnerability_policy_evaluated = True
                    result.vulnerability_policy_failed = sum(
                        policy_fails(item["severity"], policy) for item in result.vulnerability_findings
                    )
                if write_vulnerability_baseline_path:
                    from .vulnerability.pipeline import scan_inventory
                    all_findings = scan_inventory(directory, database)
                    write_vulnerability_baseline(
                        write_vulnerability_baseline_path,
                        [item["fingerprint"] for item in all_findings],
                        overwrite=force_vulnerability_baseline,
                    )
            finally:
                database.close()
        return self._apply_baseline(result)

    def _scan_diff_file(self, diff_file: DiffFile, target: str, result: ScanResult) -> None:
        """Scan a single DiffFile for secrets and config changes."""
        if self._is_ignored(target):
            return
        # Scan added lines for secrets
        for diff_line in diff_file.added_lines:
            matches = match_rules_all(diff_line.content, target, self.patterns, self.registry)
            for pattern, match in matches:
                matched_text = match.group(0)
                if is_explicit_placeholder(pattern, matched_text):
                    continue
                severity = self._path_severity(target, pattern.severity)

                # Check allowlist
                if is_suppressed(
                    pattern.name, severity, target, matched_text, self.allowlist_rules
                ):
                    continue

                # Check severity threshold
                if not self._above_threshold(severity):
                    continue

                result.findings.append(Finding(
                    type=pattern.kind,
                    pattern_name=(f"CUSTOM_PATTERN_{pattern.rule_id.removeprefix('CRT-CUSTOM-')}" if pattern.rule_id.startswith("CRT-CUSTOM-") else pattern.name),
                    severity=severity,
                    file=target,
                    line=diff_line.line_number,
                    matched_text=matched_text,
                    line_content=diff_line.content,
                    rule="secret-pattern" if pattern.kind == "secret" else "policy-pattern",
                    rule_id=pattern.rule_id,
                    category=pattern.category,
                    confidence=pattern.confidence,
                    remediation=pattern.remediation,
                ))

        for hunk_id in sorted({line.hunk_id for line in diff_file.target_lines}):
            hunk_lines = [line for line in diff_file.target_lines if line.hunk_id == hunk_id]
            added_numbers = {line.line_number for line in hunk_lines if line.line_type == "added"}
            self._append_context_findings(
                [(line.line_number, line.content) for line in hunk_lines],
                target, target, result, added_numbers,
            )

        # Classify config changes
        if self.config_check:
            categories = classify_config_file(target)
            for cat in categories:
                change_type = "added" if diff_file.is_new else ("deleted" if diff_file.is_deleted else "modified")
                result.config_changes.append(ConfigChange(
                    type="config",
                    file=target,
                    severity=self._path_severity(target, cat.severity),
                    change_type=change_type,
                    description=cat.description,
                ))

    def _scan_file_content(self, filepath: Path, result: ScanResult, policy_path: Optional[str] = None) -> None:
        """Scan a bounded regular non-symlink file for secrets."""
        try:
            raw = read_regular_bounded(filepath, MAX_FILE_BYTES, "scan input")
        except (OSError, ValueError) as exc:
            raise ValueError("Scan input cannot be safely read or exceeds the byte limit.") from exc
        content = raw.decode("utf-8", errors="replace")
        numbered_lines = list(enumerate(content.split("\n"), 1))
        if any(len(line) > MAX_LINE_CHARS for _, line in numbered_lines):
            raise ValueError("Scan input contains a line exceeding the byte limit.")
        for line_num, line in numbered_lines:
            matches = match_rules_all(line, policy_path or str(filepath), self.patterns, self.registry)
            for pattern, match in matches:
                matched_text = match.group(0)
                if is_explicit_placeholder(pattern, matched_text):
                    continue
                severity = self._path_severity(policy_path or str(filepath), pattern.severity)

                # Check allowlist
                if is_suppressed(
                    pattern.name, severity, str(filepath), matched_text, self.allowlist_rules
                ):
                    continue

                # Check severity threshold
                if not self._above_threshold(severity):
                    continue

                result.findings.append(Finding(
                    type=pattern.kind,
                    pattern_name=(f"CUSTOM_PATTERN_{pattern.rule_id.removeprefix('CRT-CUSTOM-')}" if pattern.rule_id.startswith("CRT-CUSTOM-") else pattern.name),
                    severity=severity,
                    file=str(filepath),
                    line=line_num,
                    matched_text=matched_text,
                    line_content=line,
                    rule="secret-pattern" if pattern.kind == "secret" else "policy-pattern",
                    rule_id=pattern.rule_id,
                    category=pattern.category,
                    confidence=pattern.confidence,
                    remediation=pattern.remediation,
                    identity_path=policy_path or str(filepath),
                ))

        self._append_context_findings(numbered_lines, policy_path or str(filepath), str(filepath), result)

    def _append_context_findings(
        self, lines: list[tuple[int, str]], policy_path: str, display_path: str,
        result: ScanResult, required_added_line_numbers: Optional[set[int]] = None,
    ) -> None:
        """Append bounded multi-line findings through the standard policy pipeline."""
        for context_match in match_context_rules(lines, policy_path):
            if required_added_line_numbers is not None and not required_added_line_numbers.intersection(context_match.component_line_numbers):
                continue
            rule = context_match.rule
            severity = self._path_severity(policy_path, rule.severity)
            if is_suppressed(rule.name, severity, display_path, context_match.matched_text, self.allowlist_rules):
                continue
            if not self._above_threshold(severity):
                continue
            result.findings.append(Finding(
                type=rule.kind,
                pattern_name=rule.name,
                severity=severity,
                file=display_path,
                line=context_match.line_number,
                matched_text=context_match.matched_text,
                line_content=context_match.line_content,
                rule="policy-pattern",
                rule_id=rule.rule_id,
                category=rule.category,
                confidence=rule.confidence,
                remediation=rule.remediation,
                identity_path=policy_path,
            ))

    def _classify_config(self, filepath: Path, change_type: str, result: ScanResult, policy_path: Optional[str] = None) -> None:
        """Classify a file path into config categories."""
        categories = classify_config_file(str(filepath))
        for cat in categories:
            # Deduplicate — only add if not already present
            existing = [c for c in result.config_changes if c.file == str(filepath) and c.type == cat.name]
            if not existing:
                result.config_changes.append(ConfigChange(
                    type="config",
                    file=str(filepath),
                    severity=self._path_severity(policy_path or str(filepath), cat.severity),
                    change_type=change_type,
                    description=cat.description,
                ))