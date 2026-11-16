"""Validated, bounded severity and policy configuration for the scanner."""

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from .patterns import SecretPattern, DEFAULT_SECRET_PATTERNS
from .rulepacks import _validate_regex
from .safeio import read_regular_bounded

SEVERITIES = {"low", "medium", "high", "critical"}
POLICY_PROFILES = {
    "balanced": {"severity_threshold": "medium", "config_check": True},
    "strict": {"severity_threshold": "low", "config_check": True},
    "secrets-only": {"severity_threshold": "low", "config_check": False},
}
MAX_CONFIG_BYTES = 256 * 1024
MAX_CUSTOM_PATTERNS = 64
MAX_OVERRIDES = 256
MAX_PATH_PATTERNS = 256
MAX_TEXT_CHARS = 512
ROOT_KEYS = {
    "profile", "severity_threshold", "config_check", "thresholds",
    "pattern_overrides", "custom_patterns", "file_patterns",
}


@dataclass
class ScannerConfig:
    fail_on_severity: str = "medium"
    warn_on_severity: str = "low"
    pattern_overrides: dict[str, dict] = field(default_factory=dict)
    custom_patterns: list[SecretPattern] = field(default_factory=list)
    high_severity_paths: list[str] = field(default_factory=list)
    medium_severity_paths: list[str] = field(default_factory=list)
    ignore_paths: list[str] = field(default_factory=list)
    profile: str = "balanced"
    severity_threshold: Optional[str] = None
    config_check: Optional[bool] = None


def resolve_policy(profile: str) -> dict:
    if not isinstance(profile, str) or profile not in POLICY_PROFILES:
        raise ValueError(f"Unknown policy profile: {profile}")
    return dict(POLICY_PROFILES[profile])


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Config contains a duplicate JSON key")
        result[key] = value
    return result


def _object(value, name):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _string(value, name, maximum=MAX_TEXT_CHARS):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ValueError(f"{name} must be a bounded non-empty string")
    return value


def _string_list(value, name):
    if not isinstance(value, list) or len(value) > MAX_PATH_PATTERNS:
        raise ValueError(f"{name} must be a bounded array of strings")
    if not all(isinstance(item, str) and 1 <= len(item) <= MAX_TEXT_CHARS for item in value):
        raise ValueError(f"{name} must be a bounded array of strings")
    return value


def _severity(value, name):
    if value not in SEVERITIES:
        raise ValueError(f"{name} must be one of: critical, high, medium, low")
    return value


def load_config(config_path: str) -> ScannerConfig:
    filepath = Path(config_path)
    if not filepath.exists() and not filepath.is_symlink():
        return ScannerConfig()
    try:
        raw = read_regular_bounded(filepath, MAX_CONFIG_BYTES, "scanner config")
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
    except (UnicodeDecodeError, json.JSONDecodeError, OSError, RecursionError, ValueError) as exc:
        raise ValueError("Invalid or unsafe scanner config") from exc
    data = _object(data, "Config root")
    unknown = set(data) - ROOT_KEYS
    if unknown:
        raise ValueError("Config contains unknown root fields")

    config = ScannerConfig()
    config.profile = data.get("profile", "balanced")
    resolve_policy(config.profile)
    config.severity_threshold = data.get("severity_threshold")
    if config.severity_threshold is not None:
        _severity(config.severity_threshold, "severity_threshold")
    config.config_check = data.get("config_check")
    if config.config_check is not None and not isinstance(config.config_check, bool):
        raise ValueError("config_check must be true or false")

    thresholds = _object(data.get("thresholds", {}), "thresholds")
    if set(thresholds) - {"fail_on_severity", "warn_on_severity"}:
        raise ValueError("thresholds contains unknown fields")
    config.fail_on_severity = _severity(
        thresholds.get("fail_on_severity", "medium"), "thresholds.fail_on_severity"
    )
    config.warn_on_severity = _severity(
        thresholds.get("warn_on_severity", "low"), "thresholds.warn_on_severity"
    )

    config.pattern_overrides = _object(data.get("pattern_overrides", {}), "pattern_overrides")
    if len(config.pattern_overrides) > MAX_OVERRIDES:
        raise ValueError("pattern_overrides exceeds count limit")
    for name, override in config.pattern_overrides.items():
        _string(name, "pattern override name", 128)
        override = _object(override, f"pattern_overrides.{name}")
        if set(override) - {"severity"}:
            raise ValueError(f"pattern_overrides.{name} contains unknown fields")
        if "severity" in override:
            _severity(override["severity"], f"pattern_overrides.{name}.severity")

    custom = data.get("custom_patterns", [])
    if not isinstance(custom, list) or len(custom) > MAX_CUSTOM_PATTERNS:
        raise ValueError("custom_patterns must be a bounded array")
    required = {"name", "regex"}
    allowed = required | {"severity", "description"}
    for index, candidate in enumerate(custom):
        candidate = _object(candidate, f"custom_patterns[{index}]")
        if not required.issubset(candidate) or set(candidate) - allowed:
            raise ValueError(f"custom_patterns[{index}] has an invalid schema")
        name = _string(candidate["name"], f"custom_patterns[{index}].name", 128)
        regex = _validate_regex(candidate["regex"])
        severity = _severity(candidate.get("severity", "medium"), f"custom_patterns[{index}].severity")
        description = _string(candidate.get("description", name), f"custom_patterns[{index}].description")
        digest = hashlib.sha256(f"{name}\0{regex}".encode("utf-8")).hexdigest()[:12].upper()
        config.custom_patterns.append(SecretPattern(
            name, regex, severity, description,
            f"CRT-CUSTOM-{digest}", "secret", "medium",
            "Review the custom match, remove real credentials, and rotate them if exposed.", "secret",
        ))

    files = _object(data.get("file_patterns", {}), "file_patterns")
    file_keys = {"high_severity_paths", "medium_severity_paths", "ignore_paths"}
    if set(files) - file_keys:
        raise ValueError("file_patterns contains unknown fields")
    config.high_severity_paths = _string_list(
        files.get("high_severity_paths", []), "file_patterns.high_severity_paths"
    )
    config.medium_severity_paths = _string_list(
        files.get("medium_severity_paths", []), "file_patterns.medium_severity_paths"
    )
    config.ignore_paths = _string_list(files.get("ignore_paths", []), "file_patterns.ignore_paths")
    return config


def apply_overrides(patterns: list[SecretPattern], config: ScannerConfig) -> list[SecretPattern]:
    result = []
    for pattern in patterns:
        override = config.pattern_overrides.get(pattern.name)
        result.append(replace(pattern, severity=override.get("severity", pattern.severity)) if override else pattern)
    result.extend(config.custom_patterns)
    return result


def default_config() -> ScannerConfig:
    return ScannerConfig()
