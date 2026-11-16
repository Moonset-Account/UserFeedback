"""Secret and config detection patterns for the Secret/Config Diff Scanner."""

from dataclasses import dataclass
import fnmatch
import re
from typing import Callable, Optional


@dataclass
class DetectionRule:
    """A content detection rule with stable export metadata."""
    name: str
    regex: str
    severity: str
    description: str
    rule_id: str = "CRT-SEC-000"
    category: str = "secret"
    confidence: str = "medium"
    remediation: str = "Remove the value from source and rotate the affected credential."
    kind: str = "secret"
    file_globs: tuple[str, ...] = ()
    flags: int = re.IGNORECASE
    unicode_boundaries: bool = False
    validator: Optional[Callable[[re.Match], bool]] = None

    @property
    def compiled(self) -> re.Pattern:
        regex = rf"(?<!\w)(?:{self.regex})(?!\w)" if self.unicode_boundaries else self.regex
        return re.compile(regex, self.flags)


SecretPattern = DetectionRule


def _confluent_secret_checksum(match: re.Match) -> bool:
    import base64
    import zlib
    payload, checksum = match.group(1), match.group(2)
    expected = base64.b64encode(zlib.crc32(payload.encode("ascii")).to_bytes(4, "little"))[:6].decode("ascii")
    return checksum == expected


def _match_is_valid(rule: DetectionRule, match: re.Match) -> bool:
    return rule.validator is None or rule.validator(match)


_GENERIC_ASSIGNMENT_RULES = {
    "JWT_SECRET", "PASSWORD_LITERAL", "API_KEY_LITERAL", "TOKEN_LITERAL",
    "SECRET_LITERAL", "CONNECTION_STRING", "BASE64_SECRET", "GENERIC_CREDENTIAL",
}
_EXPLICIT_PLACEHOLDER_VALUE = re.compile(
    r"^(?:REDACTED|MASKED|PLACEHOLDER)(?:[_-][A-Z0-9]+)+$", re.IGNORECASE,
)


def is_explicit_placeholder(rule: DetectionRule, matched_text: str) -> bool:
    """Return true only for canonical placeholder values in generic assignment rules."""
    if rule.name not in _GENERIC_ASSIGNMENT_RULES:
        return False
    parts = re.split(r"[=:]", matched_text, maxsplit=1)
    if len(parts) != 2:
        return False
    value = parts[1].strip().strip("'\"")
    return bool(_EXPLICIT_PLACEHOLDER_VALUE.fullmatch(value))


@dataclass(frozen=True)
class ContextDetectionRule:
    """A bounded multi-line detection rule with stable export metadata."""
    name: str
    required_regexes: tuple[str, ...]
    max_line_span: int
    severity: str
    description: str
    rule_id: str
    category: str
    confidence: str
    remediation: str
    kind: str = "policy"
    file_globs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextMatch:
    rule: ContextDetectionRule
    line_number: int
    matched_text: str
    line_content: str
    component_line_numbers: tuple[int, ...] = ()



# --- Default secret detection patterns ---

DEFAULT_SECRET_PATTERNS: list[SecretPattern] = [
    SecretPattern(
        name="AWS_ACCESS_KEY",
        regex=r"AKIA[0-9A-Z]{16}",
        severity="critical",
        description="AWS Access Key ID",
    ),
    SecretPattern(
        name="AWS_SECRET_KEY",
        regex=r"(?:AWS_SECRET_ACCESS_KEY|aws_secret)\s*[=:]\s*['\"]?[A-Za-z0-9/+=]{40}['\"]?",
        severity="critical",
        description="AWS Secret Access Key",
    ),
    SecretPattern(
        name="GITHUB_TOKEN",
        regex=r"gh[pousr]_[A-Za-z0-9_]{36,255}",
        severity="critical",
        description="GitHub Personal Access Token",
    ),
    SecretPattern(
        name="GITHUB_OAUTH",
        regex=r"(?:github_oauth_token|GITHUB_OAUTH)\s*[=:]\s*['\"]?[a-f0-9]{40}['\"]?",
        severity="critical",
        description="GitHub OAuth Token",
    ),
    SecretPattern(
        name="SLACK_TOKEN",
        regex=r"xox[baprs]-[0-9]{10,13}-[0-9]{10,13}-[a-zA-Z0-9]{24,34}",
        severity="high",
        description="Slack Token",
    ),
    SecretPattern(
        name="STRIPE_KEY",
        regex=r"sk_(?:live|test)_[A-Za-z0-9]{24,}",
        severity="critical",
        description="Stripe Secret Key",
    ),
    SecretPattern(
        name="STRIPE_PUBLISHABLE",
        regex=r"pk_(?:live|test)_[A-Za-z0-9]{24,}",
        severity="medium",
        description="Stripe Publishable Key",
    ),
    SecretPattern(
        name="HEROKU_API_KEY",
        regex=r"(?:HEROKU_API_KEY|heroku_api_key)\s*[=:]\s*['\"]?[a-f0-9-]{36}['\"]?",
        severity="high",
        description="Heroku API Key",
    ),
    SecretPattern(
        name="GOOGLE_API_KEY",
        regex=r"AIza[A-Za-z0-9_\\-]{35}",
        severity="medium",
        description="Google API Key",
    ),
    SecretPattern(
        name="GOOGLE_OAUTH",
        regex=r"(?:GOOGLE_OAUTH|google_oauth)\s*[=:]\s*['\"]?[0-9]+-[a-z0-9]{32}@[a-z.]+['\"]?",
        severity="high",
        description="Google OAuth Token",
    ),
    SecretPattern(
        name="DATABASE_URL",
        regex=r"(?:postgres(?:ql)?|mysql|mongodb|redis)://[^\s'\"}{)]+",
        severity="critical",
        description="Database Connection String",
    ),
    SecretPattern(
        name="PRIVATE_KEY",
        regex=r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----",
        severity="critical",
        description="PEM Private Key Block",
    ),
    SecretPattern(
        name="JWT_SECRET",
        regex=r"(?:JWT_SECRET|jwt_secret|jwt\.secret)\s*[=:]\s*['\"]?[^'\"\s,}]{16,}['\"]?",
        severity="high",
        description="JWT Secret/Signing Key",
    ),
    SecretPattern(
        name="PASSWORD_LITERAL",
        regex=r"(?:password|passwd|pwd)\s*[=:]\s*['\"]?[^'\"\s,}]{4,}['\"]?",
        severity="high",
        description="Password Assignment",
    ),
    SecretPattern(
        name="API_KEY_LITERAL",
        regex=r"(?:api_?key|apikey)\s*[=:]\s*['\"]?[^'\"\s,}]{8,}['\"]?",
        severity="high",
        description="API Key Assignment",
    ),
    SecretPattern(
        name="TOKEN_LITERAL",
        regex=r"(?:token|access_token|auth_token)\s*[=:]\s*['\"]?[^'\"\s,}]{16,}['\"]?",
        severity="medium",
        description="Token Assignment",
    ),
    SecretPattern(
        name="SECRET_LITERAL",
        regex=r"(?:secret|app_secret|client_secret|secret_key)\s*[=:]\s*['\"]?[^'\"\s,}]{8,}['\"]?",
        severity="medium",
        description="Secret Assignment",
    ),
    SecretPattern(
        name="CONNECTION_STRING",
        regex=r"(?:connection_string|conn_str|connectionstring)\s*[=:]\s*['\"]?[^'\"\s,}]{16,}['\"]?",
        severity="high",
        description="Connection String Assignment",
    ),
    SecretPattern(
        name="BASE64_SECRET",
        regex=r"(?:secret|key|token|password|credential)\s*[=:]\s*['\"]?[A-Za-z0-9+/]{40,}={0,2}['\"]?",
        severity="low",
        description="Potential Base64-encoded Secret",
    ),
    SecretPattern(
        name="GENERIC_CREDENTIAL",
        regex=r"(?:credential|credentials)\s*[=:]\s*['\"]?[^'\"\s,}]{8,}['\"]?",
        severity="medium",
        description="Generic Credential Assignment",
    ),
]

for _index, _rule in enumerate(DEFAULT_SECRET_PATTERNS, 1):
    _rule.rule_id = f"CRT-SEC-{_index:03d}"
    _rule.category = "secret"
    _rule.confidence = "medium" if _rule.severity in {"low", "medium"} else "high"
    _rule.remediation = "Remove the value from source, rotate it, and load it from an approved secret store."
    _rule.kind = "secret"


V3_STAGE1_RULES: list[DetectionRule] = [
    DetectionRule("OPENAI_API_KEY", r"sk-(?!ant-)(?:proj-)?[A-Za-z0-9_-]{32,}", "critical", "OpenAI API key", "CRT-SEC-021", "secret", "high", "Revoke the key and load a replacement from a secret store.", "secret"),
    DetectionRule("ANTHROPIC_API_KEY", r"sk-ant-(?:api\d+-)?[A-Za-z0-9_-]{32,}", "critical", "Anthropic API key", "CRT-SEC-022", "secret", "high", "Revoke the key and load a replacement from a secret store.", "secret"),
    DetectionRule("HUGGINGFACE_TOKEN", r"hf_[A-Za-z0-9]{30,}", "critical", "Hugging Face access token", "CRT-SEC-023", "secret", "high", "Revoke the token and load a scoped replacement from a secret store.", "secret"),
    DetectionRule("GITLAB_TOKEN", r"glpat-[A-Za-z0-9_-]{20,}", "critical", "GitLab personal access token", "CRT-SEC-024", "secret", "high", "Revoke the token and create a least-privilege replacement.", "secret"),
    DetectionRule("TELEGRAM_BOT_TOKEN", r"\b[0-9]{8,12}:[A-Za-z0-9_-]{30,}\b", "critical", "Telegram bot token", "CRT-SEC-025", "secret", "high", "Rotate the bot token and store it outside source control.", "secret"),
    DetectionRule("DISCORD_WEBHOOK", r"https://discord(?:app)?\.com/api/webhooks/[0-9]{15,}/[A-Za-z0-9_-]{30,}", "critical", "Discord webhook credential", "CRT-SEC-026", "secret", "high", "Delete the webhook and keep its replacement URL in a secret store.", "secret"),
    DetectionRule("CI_WRITE_ALL", r"^\s*permissions\s*:\s*write-all\s*(?:#.*)?$", "high", "Workflow grants write access to all token scopes", "CRT-CI-001", "ci", "high", "Replace write-all with explicit least-privilege permissions.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_UNPINNED_ACTION", r"^\s*-?\s*uses\s*:\s*[^\s@]+@(main|master|latest)\s*(?:#.*)?$", "medium", "GitHub Action uses a mutable reference", "CRT-CI-002", "ci", "high", "Pin third-party actions to a reviewed full commit SHA.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CONTAINER_PRIVILEGED", r"^\s*privileged\s*:\s*true\s*(?:#.*)?$", "high", "Container runs in privileged mode", "CRT-IAC-001", "iac", "high", "Remove privileged mode and grant only required capabilities.", "policy", ("docker-compose.yml", "docker-compose.yaml", "**/docker-compose.yml", "**/docker-compose.yaml")),
    DetectionRule("DOCKER_LATEST_TAG", r"^\s*FROM\s+[^\s:]+:latest(?:\s|$)", "medium", "Docker base image uses mutable latest tag", "CRT-IAC-002", "iac", "high", "Pin the base image to an immutable version or digest.", "policy", ("Dockerfile", "**/Dockerfile", "Dockerfile.*", "**/Dockerfile.*")),
    DetectionRule("AGENT_REMOTE_EXEC", r"\bcurl\b[^|\n]*\|\s*(?:ba)?sh\b", "critical", "AI-agent instructions download and execute a remote script", "CRT-AI-001", "ai-agent", "high", "Require download, verification, review, then explicit execution.", "policy", ("AGENTS.md", "**/AGENTS.md", "CLAUDE.md", "**/CLAUDE.md", ".cursorrules", "**/.cursorrules", ".github/copilot-instructions.md")),
    DetectionRule("AGENT_DISABLE_SECURITY", r"\bdisable\s+(?:all\s+)?security\s+(?:checks?|controls?|validation)\b", "high", "AI-agent instructions disable security controls", "CRT-AI-002", "ai-agent", "high", "Keep security controls enabled and document narrowly scoped exceptions.", "policy", ("AGENTS.md", "**/AGENTS.md", "CLAUDE.md", "**/CLAUDE.md", ".cursorrules", "**/.cursorrules", ".github/copilot-instructions.md")),
    DetectionRule("REMOTE_SCRIPT_PIPE", r"\bcurl\b[^|\n]*\|\s*(?:ba)?sh\b", "high", "Remote script is piped directly to a shell", "CRT-SUP-001", "supply-chain", "high", "Download, verify, and review the script before execution.", "policy", ("*.sh", "**/*.sh", "Dockerfile", "**/Dockerfile", "Makefile", "**/Makefile")),
    DetectionRule("AZURE_STORAGE_ACCOUNT_KEY", r"\bAccountKey\s*=\s*[A-Za-z0-9+/]{40,}={0,2}", "critical", "Azure Storage account key", "CRT-SEC-027", "secret", "high", "Rotate the storage account key and load it from an approved secret store.", "secret"),
    DetectionRule("MONGODB_URI_CREDENTIAL", r"mongodb(?:\+srv)?://[^:\s/@]+:[^@\s]+@[^\s]+", "critical", "MongoDB URI with embedded credentials", "CRT-SEC-028", "secret", "high", "Rotate the database credential and use a secret-backed connection string.", "secret"),
    DetectionRule("REDIS_URI_CREDENTIAL", r"rediss?://[^:\s/@]+:[^@\s]+@[^\s]+", "critical", "Redis URI with embedded credentials", "CRT-SEC-029", "secret", "high", "Rotate the Redis credential and use a secret-backed connection string.", "secret"),
    DetectionRule("NPM_AUTH_TOKEN", r"_authToken\s*=\s*[A-Za-z0-9_.-]{20,}", "critical", "npm registry authentication token", "CRT-SEC-030", "secret", "high", "Revoke the npm token and inject a scoped replacement at runtime.", "secret", (".npmrc", "**/.npmrc")),
    DetectionRule("PYPI_API_TOKEN", r"pypi-[A-Za-z0-9_-]{50,}", "critical", "PyPI API token", "CRT-SEC-031", "secret", "high", "Revoke the PyPI token and use a scoped trusted-publishing or secret-store credential.", "secret"),
    DetectionRule("AZURE_SAS_SIGNATURE", r"\bsig\s*=\s*[A-Za-z0-9%_-]{40,}", "critical", "Azure SAS signature", "CRT-SEC-032", "secret", "high", "Revoke or expire the SAS and generate a least-privilege short-lived replacement.", "secret", ("*.env", "**/*.env", ".env", "**/.env", "*.conf", "**/*.conf", "*.config", "**/*.config")),
    DetectionRule("CI_PERSIST_CREDENTIALS", r"^\s*persist-credentials\s*:\s*true\s*(?:#.*)?$", "medium", "Checkout credentials remain persisted", "CRT-CI-003", "ci", "high", "Set persist-credentials to false unless later authenticated Git operations are required.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_INHERIT_ALL_SECRETS", r"^\s*secrets\s*:\s*inherit\s*(?:#.*)?$", "high", "Reusable workflow inherits all caller secrets", "CRT-CI-004", "ci", "high", "Pass only explicitly required secrets to the reusable workflow.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_SCRIPT_INJECTION_PR_TITLE", r"^\s*run\s*:.*\$\{\{\s*github\.event\.pull_request\.(?:title|body)\s*\}\}.*$", "high", "Workflow interpolates untrusted pull-request text directly into a shell command", "CRT-CI-010", "ci", "high", "Pass untrusted pull-request fields through a validated environment variable and quote shell inputs.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_SCRIPT_INJECTION_REF", r"^\s*run\s*:.*\$\{\{\s*github\.(?:head_ref|base_ref)\s*\}\}.*$", "high", "Workflow interpolates an attacker-influenced branch name directly into a shell command", "CRT-CI-011", "ci", "high", "Pass branch names through a safely quoted environment variable before shell use.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_SCRIPT_INJECTION_ISSUE_BODY", r"^\s*run\s*:.*\$\{\{\s*github\.event\.issue\.(?:title|body)\s*\}\}.*$", "high", "Workflow interpolates untrusted issue text directly into a shell command", "CRT-CI-012", "ci", "high", "Pass untrusted issue fields through a validated environment variable and quote shell inputs.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_SCRIPT_INJECTION_COMMENT_BODY", r"^\s*run\s*:.*\$\{\{\s*github\.event\.comment\.body\s*\}\}.*$", "high", "Workflow interpolates issue or PR comment body into a shell command", "CRT-CI-025", "ci", "high", "Move untrusted comment content through an environment variable and quote it before processing.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_SCRIPT_INJECTION_REVIEW_BODY", r"^\s*run\s*:.*\$\{\{\s*github\.event\.review\.body\s*\}\}.*$", "high", "Workflow interpolates pull request review body into a shell command", "CRT-CI-026", "ci", "high", "Move untrusted review content through an environment variable and quote it before processing.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_SCRIPT_INJECTION_REVIEW_COMMENT", r"^\s*run\s*:.*\$\{\{\s*github\.event\.pull_request_review_comment\.body\s*\}\}.*$", "high", "Workflow interpolates review comment body into a shell command", "CRT-CI-027", "ci", "high", "Move untrusted review comment content through an environment variable and quote it before processing.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_SCRIPT_INJECTION_DISCUSSION_BODY", r"^\s*run\s*:.*\$\{\{\s*github\.event\.discussion\.body\s*\}\}.*$", "high", "Workflow interpolates discussion body into a shell command", "CRT-CI-028", "ci", "high", "Move untrusted discussion content through an environment variable and quote it before processing.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_SCRIPT_INJECTION_HEAD_LABEL", r"^\s*run\s*:.*\$\{\{\s*github\.event\.pull_request\.head\.label\s*\}\}.*$", "high", "Workflow interpolates pull request head label into a shell command", "CRT-CI-029", "ci", "high", "Move untrusted pull request head metadata through an environment variable and quote it before processing.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_SCRIPT_INJECTION_WORKFLOW_HEAD_BRANCH", r"^\s*run\s*:.*\$\{\{\s*github\.event\.workflow_run\.head_branch\s*\}\}.*$", "high", "Workflow interpolates the triggering workflow head branch into a shell command", "CRT-CI-030", "ci", "high", "Move the untrusted workflow branch through an environment variable and quote it before processing.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_SCRIPT_INJECTION_WORKFLOW_TITLE", r"^\s*run\s*:.*\$\{\{\s*github\.event\.workflow_run\.display_title\s*\}\}.*$", "high", "Workflow interpolates the triggering workflow display title into a shell command", "CRT-CI-031", "ci", "high", "Move the untrusted workflow title through an environment variable and quote it before processing.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_SCRIPT_INJECTION_WORKFLOW_COMMIT_MESSAGE", r"^\s*run\s*:.*\$\{\{\s*github\.event\.workflow_run\.head_commit\.message\s*\}\}.*$", "high", "Workflow interpolates the triggering commit message into a shell command", "CRT-CI-032", "ci", "high", "Move the untrusted commit message through an environment variable and quote it before processing.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_SCRIPT_INJECTION_WORKFLOW_PR_HEAD_REF", r"^\s*run\s*:.*\$\{\{\s*github\.event\.workflow_run\.pull_requests\[\d+\]\.head\.ref\s*\}\}.*$", "high", "Workflow interpolates a triggering pull request head ref into a shell command", "CRT-CI-033", "ci", "high", "Move the untrusted pull request ref through an environment variable and quote it before processing.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_SCRIPT_INJECTION_EVENT_PR_HEAD_REF", r"^\s*run\s*:.*\$\{\{\s*github\.event\.pull_request\.head\.ref\s*\}\}.*$", "high", "Workflow interpolates a pull request head ref into a shell command", "CRT-CI-034", "ci", "high", "Move the untrusted pull request ref through an environment variable and quote it before processing.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_SCRIPT_INJECTION_CLIENT_PAYLOAD", r"^\s*run\s*:.*\$\{\{\s*github\.event\.client_payload\.[A-Za-z0-9_.-]+\s*\}\}.*$", "high", "Workflow interpolates repository_dispatch client payload into a shell command", "CRT-CI-035", "ci", "high", "Treat repository_dispatch payload fields as untrusted and pass them through a validated environment variable.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_SCRIPT_INJECTION_EVENT_INPUT", r"^\s*run\s*:.*\$\{\{\s*github\.event\.inputs\.[A-Za-z0-9_.-]+\s*\}\}.*$", "high", "Workflow interpolates workflow_dispatch input into a shell command", "CRT-CI-036", "ci", "high", "Treat workflow_dispatch input fields as untrusted and pass them through a validated environment variable.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_SCRIPT_INJECTION_WORKFLOW_INPUT", r"^\s*run\s*:.*\$\{\{\s*inputs\.[A-Za-z0-9_.-]+\s*\}\}.*$", "high", "Workflow interpolates a caller input into a shell command", "CRT-CI-037", "ci", "high", "Treat reusable-workflow and dispatch inputs as untrusted and pass them through a validated environment variable.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_SCRIPT_INJECTION_RELEASE_BODY", r"^\s*run\s*:.*\$\{\{\s*github\.event\.release\.body\s*\}\}.*$", "high", "Workflow interpolates release body into a shell command", "CRT-CI-038", "ci", "high", "Treat release body content as untrusted and pass it through a validated environment variable.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_SCRIPT_INJECTION_RELEASE_NAME", r"^\s*run\s*:.*\$\{\{\s*github\.event\.release\.name\s*\}\}.*$", "high", "Workflow interpolates release name into a shell command", "CRT-CI-039", "ci", "high", "Treat release name content as untrusted and pass it through a validated environment variable.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_SCRIPT_INJECTION_PAGE_NAME", r"^\s*run\s*:.*\$\{\{\s*github\.event\.pages\[\d+\]\.page_name\s*\}\}.*$", "high", "Workflow interpolates edited page name into a shell command", "CRT-CI-040", "ci", "high", "Treat page names as untrusted and pass them through a validated environment variable.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_SCRIPT_INJECTION_DEFAULT_BRANCH", r"^\s*run\s*:.*\$\{\{\s*github\.event\.repository\.default_branch\s*\}\}.*$", "high", "Workflow interpolates repository default branch into a shell command", "CRT-CI-041", "ci", "high", "Treat repository context values as untrusted shell input and pass them through a validated environment variable.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_SCRIPT_INJECTION_RELEASE_TAG", r"^\s*run\s*:.*\$\{\{\s*github\.event\.release\.tag_name\s*\}\}.*$", "high", "Workflow interpolates release tag name into a shell command", "CRT-CI-042", "ci", "high", "Treat release tag names as untrusted and pass them through a validated environment variable.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_SCRIPT_INJECTION_RELEASE_TARGET", r"^\s*run\s*:.*\$\{\{\s*github\.event\.release\.target_commitish\s*\}\}.*$", "high", "Workflow interpolates release target commitish into a shell command", "CRT-CI-043", "ci", "high", "Treat release target commitish as untrusted and pass it through a validated environment variable.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_SCRIPT_INJECTION_WORKFLOW_REPO_BRANCH", r"^\s*run\s*:.*\$\{\{\s*github\.event\.workflow_run\.head_repository\.default_branch\s*\}\}.*$", "high", "Workflow interpolates source workflow repository default branch into a shell command", "CRT-CI-044", "ci", "high", "Treat source workflow repository context as untrusted shell input and pass it through a validated environment variable.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_OIDC_TOKEN_PERMISSION", r"^\s*id-token\s*:\s*write\s*(?:#.*)?$", "medium", "Workflow grants permission to request an OIDC token", "CRT-CI-045", "ci", "high", "Use id-token write only for reviewed federated deployments and constrain the cloud trust policy to the intended workflow.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_DYNAMIC_ENVIRONMENT_INPUT", r"^\s*environment\s*:\s*\$\{\{\s*inputs\.[A-Za-z0-9_.-]+\s*\}\}\s*$", "high", "Deployment environment is selected from a caller-controlled workflow input", "CRT-CI-046", "ci", "high", "Map inputs to an explicit allowlist of protected environments instead of selecting an environment directly from caller input.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_DYNAMIC_ENVIRONMENT_REF", r"^\s*environment\s*:\s*\$\{\{\s*github\.(?:head_ref|ref)\s*\}\}\s*$", "high", "Deployment environment is selected from a branch or ref context", "CRT-CI-047", "ci", "high", "Map branch/ref values to an explicit allowlist of protected environments before deployment.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_GITHUB_TOKEN_IN_RUN", r"^\s*run\s*:.*\$\{\{\s*github\.token\s*\}\}.*$", "high", "Workflow interpolates the GitHub token directly into a shell command", "CRT-CI-048", "ci", "high", "Pass the token through a narrowly scoped environment variable only when required and avoid direct command-line interpolation or logging.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_DOCKER_ACTION_MUTABLE_TAG", r"^\s*uses\s*:\s*docker://[^\s@]+:latest\s*(?:#.*)?$", "medium", "Docker container action uses the mutable latest tag", "CRT-CI-049", "ci", "high", "Pin Docker container actions to an immutable digest or reviewed version rather than latest.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_ACTION_VERSION_TAG", r"^\s*uses\s*:\s*[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@v\d+(?:\.\d+){0,2}\s*(?:#.*)?$", "medium", "Third-party workflow action is pinned to a mutable version tag", "CRT-CI-050", "ci", "high", "Prefer a full-length commit SHA for third-party actions; use a tag only after reviewing and trusting its publisher.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_ACTION_PRERELEASE_TAG", r"^\s*uses\s*:\s*[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@v\d+(?:\.\d+){0,2}[-_.][A-Za-z0-9.-]+\s*(?:#.*)?$", "medium", "Third-party workflow action uses a mutable prerelease tag", "CRT-CI-051", "ci", "high", "Prefer a full-length commit SHA for prerelease actions; use a tag only after reviewing and trusting its publisher.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_ACTION_NAMED_FLOATING_REF", r"^\s*uses\s*:\s*[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@(stable|release|production|develop|dev|next|vnext)\s*(?:#.*)?$", "medium", "Third-party workflow action uses a named mutable reference", "CRT-CI-052", "ci", "high", "Prefer a full-length commit SHA for named action refs; use a named tag only after reviewing and trusting its publisher.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_ACTION_CHANNEL_FLOATING_REF", r"^\s*uses\s*:\s*[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@(canary|nightly|beta|alpha|edge|experimental)\s*(?:#.*)?$", "medium", "Third-party workflow action uses a prerelease channel reference", "CRT-CI-053", "ci", "high", "Prefer a full-length commit SHA for channel refs; use a channel tag only after reviewing and trusting its publisher.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_ACTION_DYNAMIC_REF", r"^\s*uses\s*:\s*[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@\$\{\{\s*[^}]+\}\}\s*(?:#.*)?$", "high", "Third-party workflow action ref is resolved dynamically from workflow context", "CRT-CI-054", "ci", "high", "Resolve third-party actions to a reviewed full commit SHA rather than a dynamic expression.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_CHECKOUT_CLEAN_DISABLED", r"^\s*clean\s*:\s*false\s*(?:#.*)?$", "medium", "Workflow disables checkout cleanup before a subsequent checkout", "CRT-CI-013", "ci", "high", "Keep checkout cleanup enabled on persistent runners unless the workspace is isolated and explicitly reviewed.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_REMOTE_SCRIPT_PIPE", r"^\s*run\s*:.*\b(?:curl|wget)\b[^|\n]*\|\s*(?:ba)?sh\b.*$", "high", "Workflow downloads and immediately pipes a remote script to a shell", "CRT-CI-015", "ci", "high", "Download, verify and review a pinned artifact before execution; do not pipe remote content directly to a shell.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_DOCKER_SOCKET_MOUNT", r"^\s*-?\s*/var/run/docker\.sock\s*:\s*/var/run/docker\.sock\s*(?:#.*)?$", "critical", "Workflow exposes the host Docker socket to a job or container", "CRT-CI-016", "ci", "high", "Avoid mounting the host Docker socket; use an isolated builder or narrowly scoped rootless service.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_MUTABLE_CONTAINER_TAG", r"^\s*(?:container|image)\s*:\s*(?:[^\s:#]+/)?[^\s:#]+:latest\s*(?:#.*)?$", "medium", "Workflow container uses the mutable latest tag", "CRT-CI-017", "ci", "high", "Pin the workflow container to an immutable version or digest and review updates explicitly.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_SECRET_INTERPOLATION", r"^\s*run\s*:.*\$\{\{\s*secrets\.[A-Za-z0-9_]+\s*\}\}.*$", "high", "Workflow interpolates a secret directly into a shell command", "CRT-CI-020", "ci", "high", "Pass secrets through the step environment and avoid direct command-line interpolation or log exposure.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("TF_WORLD_OPEN_CIDR", r"^\s*cidr_blocks\s*=\s*\[[^\]]*[\"']0\.0\.0\.0/0[\"'][^\]]*\]", "high", "Terraform ingress allows the entire IPv4 internet", "CRT-IAC-003", "iac", "high", "Restrict ingress CIDRs to approved networks.", "policy", ("*.tf", "**/*.tf")),
    DetectionRule("K8S_RUN_AS_ROOT", r"^\s*runAsUser\s*:\s*0\s*(?:#.*)?$", "high", "Kubernetes workload explicitly runs as UID 0", "CRT-IAC-004", "iac", "high", "Run the container as a dedicated non-root UID and enforce runAsNonRoot.", "policy", ("k8s/*.yml", "k8s/*.yaml", "k8s/**/*.yml", "k8s/**/*.yaml", "kubernetes/**/*.yml", "kubernetes/**/*.yaml", "manifests/*.yml", "manifests/*.yaml", "manifests/**/*.yml", "manifests/**/*.yaml")),
    DetectionRule("K8S_PRIVILEGE_ESCALATION", r"^\s*allowPrivilegeEscalation\s*:\s*true\s*(?:#.*)?$", "high", "Kubernetes container permits privilege escalation", "CRT-IAC-005", "iac", "high", "Set allowPrivilegeEscalation to false and drop unnecessary capabilities.", "policy", ("k8s/*.yml", "k8s/*.yaml", "k8s/**/*.yml", "k8s/**/*.yaml", "kubernetes/**/*.yml", "kubernetes/**/*.yaml", "manifests/*.yml", "manifests/*.yaml", "manifests/**/*.yml", "manifests/**/*.yaml")),
    DetectionRule("NGINX_PROXY_TLS_VERIFY_OFF", r"^\s*proxy_ssl_verify\s+off\s*;\s*(?:#.*)?$", "high", "nginx disables upstream TLS certificate verification", "CRT-IAC-006", "iac", "high", "Enable proxy_ssl_verify and configure a trusted CA bundle and server name.", "policy", ("nginx/*.conf", "nginx/**/*.conf", "**/nginx/*.conf", "**/nginx/**/*.conf", "*.nginx.conf", "**/*.nginx.conf")),
    DetectionRule("SYSTEMD_PROTECT_SYSTEM_OFF", r"^\s*ProtectSystem\s*=\s*(?:false|off|no)\s*(?:#.*)?$", "medium", "systemd filesystem protection is disabled", "CRT-IAC-007", "iac", "high", "Set ProtectSystem to full or strict and add narrow write exceptions.", "policy", ("*.service", "**/*.service", "*.socket", "**/*.socket")),
    DetectionRule("NPM_INSTALL_NONDETERMINISTIC", r"\bnpm\s+install\b", "medium", "npm install does not strictly enforce the lockfile", "CRT-SUP-002", "supply-chain", "high", "Use npm ci in CI and container builds to enforce the committed lockfile.", "policy", ("Dockerfile", "**/Dockerfile", "Dockerfile.*", "**/Dockerfile.*", ".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("PIP_MUTABLE_GIT_DEPENDENCY", r"\bpip(?:3)?\s+install\s+git\+https://[^\s@]+(?:\s|$)", "high", "pip installs a mutable Git dependency without a commit pin", "CRT-SUP-003", "supply-chain", "high", "Pin the Git dependency to a reviewed full commit hash and verify package integrity.", "policy", ("requirements*.txt", "**/requirements*.txt", "Dockerfile", "**/Dockerfile", "Dockerfile.*", "**/Dockerfile.*", ".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("AGENT_SKIP_PERMISSIONS", r"--dangerously-skip-permissions\b", "critical", "AI-agent instructions bypass permission checks", "CRT-AI-003", "ai-agent", "high", "Remove the bypass flag and require explicit permission boundaries.", "policy", ("AGENTS.md", "**/AGENTS.md", "CLAUDE.md", "**/CLAUDE.md", ".cursorrules", "**/.cursorrules", ".github/copilot-instructions.md")),
    DetectionRule("AGENT_WORLD_WRITABLE", r"\bchmod\s+(?:-R\s+)?777\b", "high", "AI-agent instructions make files world-writable", "CRT-AI-004", "ai-agent", "high", "Use least-privilege ownership and narrowly scoped file modes.", "policy", ("AGENTS.md", "**/AGENTS.md", "CLAUDE.md", "**/CLAUDE.md", ".cursorrules", "**/.cursorrules", ".github/copilot-instructions.md")),
    DetectionRule("SENDGRID_API_KEY", r"SG\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{30,}", "critical", "SendGrid API key", "CRT-SEC-033", "secret", "high", "Revoke the SendGrid key and issue a least-privilege replacement from a secret store.", "secret"),
    DetectionRule("TWILIO_API_KEY", r"\bSK[0-9a-fA-F]{32}\b", "critical", "Twilio API key", "CRT-SEC-034", "secret", "high", "Revoke the Twilio key and issue a scoped replacement.", "secret"),
    DetectionRule("SHOPIFY_ACCESS_TOKEN", r"\bshpat_[0-9a-fA-F]{32}\b", "critical", "Shopify access token", "CRT-SEC-035", "secret", "high", "Revoke the Shopify token and create a least-privilege replacement.", "secret"),
    DetectionRule("SHOPIFY_ACCESS_TOKEN_V2", r"\bshpat_[0-9a-fA-F]{38}\b", "critical", "Shopify access token v2", "CRT-SEC-185", "secret", "high", "Revoke the Shopify token and create a least-privilege replacement.", "secret"),
    DetectionRule("SHOPIFY_CUSTOM_TOKEN_V2", r"\bshpca_[0-9a-fA-F]{38}\b", "critical", "Shopify custom app access token v2", "CRT-SEC-186", "secret", "high", "Revoke the Shopify token and create a least-privilege replacement.", "secret"),
    DetectionRule("SHOPIFY_PRIVATE_TOKEN_V2", r"\bshppa_[0-9a-fA-F]{38}\b", "critical", "Shopify legacy private app token v2", "CRT-SEC-187", "secret", "high", "Revoke the Shopify token and create a least-privilege replacement.", "secret"),
    DetectionRule("SHOPIFY_SHARED_SECRET_V2", r"\bshpss_[0-9a-fA-F]{38}\b", "critical", "Shopify app secret key v2", "CRT-SEC-188", "secret", "high", "Revoke the Shopify app secret and create a least-privilege replacement.", "secret"),
    DetectionRule("DIGITALOCEAN_TOKEN", r"\bdop_v1_[0-9a-fA-F]{64}\b", "critical", "DigitalOcean personal access token", "CRT-SEC-036", "secret", "high", "Revoke the DigitalOcean token and use a scoped replacement.", "secret"),
    DetectionRule("DATADOG_API_KEY", r"\b(?:DD_API_KEY|DATADOG_API_KEY)\s*=\s*[0-9a-fA-F]{32}\b", "critical", "Datadog API key assignment", "CRT-SEC-037", "secret", "high", "Revoke the Datadog key and inject its replacement from a secret store.", "secret", ("*.env", "**/*.env", ".env", "**/.env", "*.conf", "**/*.conf", "*.config", "**/*.config")),
    DetectionRule("SUPABASE_SERVICE_ROLE_KEY", r"\bSUPABASE_SERVICE_ROLE_KEY\s*=\s*eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{20,}", "critical", "Supabase service-role JWT assignment", "CRT-SEC-038", "secret", "high", "Rotate the service-role key and keep it exclusively in a server-side secret store.", "secret", ("*.env", "**/*.env", ".env", "**/.env", "*.conf", "**/*.conf", "*.config", "**/*.config")),
    DetectionRule("CI_GIT_TLS_VERIFY_DISABLED", r"^\s*GIT_SSL_NO_VERIFY\s*:\s*[\"']?(?:true|1)[\"']?\s*(?:#.*)?$", "high", "CI disables Git TLS certificate verification", "CRT-CI-005", "ci", "high", "Remove GIT_SSL_NO_VERIFY and configure the correct trusted CA.", "policy", (".gitlab-ci.yml", ".gitlab-ci.yaml", ".circleci/config.yml", ".circleci/config.yaml", ".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_DIND_TLS_DISABLED", r"^\s*DOCKER_TLS_CERTDIR\s*:\s*[\"']\s*[\"']\s*(?:#.*)?$", "high", "CI disables Docker-in-Docker TLS certificates", "CRT-CI-006", "ci", "high", "Enable Docker-in-Docker TLS or use a daemonless isolated builder.", "policy", (".gitlab-ci.yml", ".gitlab-ci.yaml", ".circleci/config.yml", ".circleci/config.yaml", ".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("COMPOSE_CAP_ADD_ALL", r"^\s*cap_add\s*:\s*\[\s*[\"']?ALL[\"']?\s*\]\s*(?:#.*)?$", "high", "Docker Compose grants all Linux capabilities", "CRT-IAC-008", "iac", "high", "Grant only the individual Linux capabilities required by the service.", "policy", ("docker-compose.yml", "docker-compose.yaml", "**/docker-compose.yml", "**/docker-compose.yaml", "compose.yml", "compose.yaml", "**/compose.yml", "**/compose.yaml")),
    DetectionRule("K8S_HOST_NETWORK", r"^\s*hostNetwork\s*:\s*true\s*(?:#.*)?$", "high", "Kubernetes workload uses the host network namespace", "CRT-IAC-009", "iac", "high", "Disable hostNetwork and expose only required ports through Kubernetes networking.", "policy", ("k8s/*.yml", "k8s/*.yaml", "k8s/**/*.yml", "k8s/**/*.yaml", "kubernetes/**/*.yml", "kubernetes/**/*.yaml", "manifests/*.yml", "manifests/*.yaml", "manifests/**/*.yml", "manifests/**/*.yaml")),
    DetectionRule("K8S_HOST_PID", r"^\s*hostPID\s*:\s*true\s*(?:#.*)?$", "high", "Kubernetes workload uses the host PID namespace", "CRT-IAC-010", "iac", "high", "Disable hostPID unless a narrowly reviewed operational requirement exists.", "policy", ("k8s/*.yml", "k8s/*.yaml", "k8s/**/*.yml", "k8s/**/*.yaml", "kubernetes/**/*.yml", "kubernetes/**/*.yaml", "manifests/*.yml", "manifests/*.yaml", "manifests/**/*.yml", "manifests/**/*.yaml")),
    DetectionRule("TF_PUBLIC_NETWORK_ACCESS", r"^\s*public_network_access_enabled\s*=\s*true\s*(?:#.*)?$", "high", "Terraform explicitly enables public network access", "CRT-IAC-011", "iac", "high", "Disable public network access and use approved private endpoints.", "policy", ("*.tf", "**/*.tf")),
    DetectionRule("SYSTEMD_NO_NEW_PRIVILEGES_OFF", r"^\s*NoNewPrivileges\s*=\s*(?:false|off|no)\s*(?:#.*)?$", "high", "systemd permits acquisition of new privileges", "CRT-IAC-012", "iac", "high", "Set NoNewPrivileges=true unless explicitly incompatible with the service.", "policy", ("*.service", "**/*.service", "*.socket", "**/*.socket")),
    DetectionRule("NPM_FORCE_INSTALL_SCRIPTS", r"\bnpm\s+install\b[^\n]*--ignore-scripts(?:=|\s+)false\b", "high", "npm lifecycle scripts are explicitly forced on", "CRT-SUP-004", "supply-chain", "high", "Do not force lifecycle scripts on; review required scripts and run them explicitly.", "policy", ("Dockerfile", "**/Dockerfile", "Dockerfile.*", "**/Dockerfile.*", ".github/workflows/*.yml", ".github/workflows/*.yaml", ".gitlab-ci.yml", ".gitlab-ci.yaml", ".circleci/config.yml", ".circleci/config.yaml")),
    DetectionRule("PIP_TRUSTED_HOST", r"\bpip(?:3)?\s+install\b[^\n]*--trusted-host\s+[^\s]+", "high", "pip trusted-host bypasses TLS host verification", "CRT-SUP-005", "supply-chain", "high", "Remove trusted-host and configure a repository with a valid trusted certificate.", "policy", ("requirements*.txt", "**/requirements*.txt", "Dockerfile", "**/Dockerfile", "Dockerfile.*", "**/Dockerfile.*", ".github/workflows/*.yml", ".github/workflows/*.yaml", ".gitlab-ci.yml", ".gitlab-ci.yaml", ".circleci/config.yml", ".circleci/config.yaml")),
    DetectionRule("NPM_STRICT_SSL_DISABLED", r"(?:\bnpm\s+config\s+set\s+strict-ssl\s+false\b|^\s*strict-ssl\s*=\s*false\s*$)", "high", "npm registry TLS verification is disabled", "CRT-SUP-006", "supply-chain", "high", "Enable strict SSL and configure the correct trusted CA.", "policy", (".npmrc", "**/.npmrc", "Dockerfile", "**/Dockerfile", "Dockerfile.*", "**/Dockerfile.*", ".github/workflows/*.yml", ".github/workflows/*.yaml", ".gitlab-ci.yml", ".gitlab-ci.yaml", ".circleci/config.yml", ".circleci/config.yaml")),
    DetectionRule("AGENT_NO_SANDBOX", r"--no-sandbox\b", "high", "AI-agent instructions disable process or browser sandboxing", "CRT-AI-005", "ai-agent", "high", "Keep sandboxing enabled and document any narrowly scoped exception.", "policy", ("AGENTS.md", "**/AGENTS.md", "CLAUDE.md", "**/CLAUDE.md", ".cursorrules", "**/.cursorrules", ".github/copilot-instructions.md")),
    DetectionRule("AGENT_DESTRUCTIVE_GIT_RESET", r"\bgit\s+reset\s+--hard\b", "high", "AI-agent instructions perform destructive Git reset", "CRT-AI-006", "ai-agent", "high", "Use non-destructive Git inspection and require explicit human approval for destructive resets.", "policy", ("AGENTS.md", "**/AGENTS.md", "CLAUDE.md", "**/CLAUDE.md", ".cursorrules", "**/.cursorrules", ".github/copilot-instructions.md")),
    DetectionRule("CLOUDFLARE_API_TOKEN", r"\bCLOUDFLARE_API_TOKEN\s*=\s*[A-Za-z0-9_-]{40}\b", "critical", "Cloudflare API token assignment", "CRT-SEC-039", "secret", "high", "Revoke the Cloudflare token and issue a least-privilege replacement.", "secret", ("*.env", "**/*.env", ".env", "**/.env", "*.conf", "**/*.conf", "*.config", "**/*.config")),
    DetectionRule("SENTRY_DSN_CREDENTIAL", r"https://[0-9a-fA-F]{32}@[A-Za-z0-9.-]+/\d+", "critical", "Sentry DSN with embedded credential", "CRT-SEC-040", "secret", "high", "Rotate the Sentry DSN credential and inject it from an approved configuration store.", "secret"),
    DetectionRule("LINEAR_API_KEY", r"\blin_api_[A-Za-z0-9_-]{40}\b", "critical", "Linear API key", "CRT-SEC-041", "secret", "high", "Revoke the Linear key and create a scoped replacement.", "secret"),
    DetectionRule("NOTION_INTEGRATION_TOKEN", r"\bntn_[A-Za-z0-9_-]{40}\b", "critical", "Notion integration token", "CRT-SEC-042", "secret", "high", "Revoke the Notion token and create a least-privilege replacement.", "secret"),
    DetectionRule("MAILGUN_API_KEY", r"\bkey-[0-9a-fA-F]{32}\b", "critical", "Mailgun API key", "CRT-SEC-043", "secret", "high", "Revoke the Mailgun key and create a scoped replacement.", "secret"),
    DetectionRule("POSTMARK_SERVER_TOKEN", r"\bPOSTMARK_SERVER_TOKEN\s*=\s*[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b", "critical", "Postmark server token assignment", "CRT-SEC-044", "secret", "high", "Rotate the Postmark server token and inject it from a secret store.", "secret", ("*.env", "**/*.env", ".env", "**/.env", "*.conf", "**/*.conf", "*.config", "**/*.config")),
    DetectionRule("CI_ACTIONS_UNSECURE_COMMANDS", r"^\s*ACTIONS_ALLOW_UNSECURE_COMMANDS\s*:\s*[\"']?true[\"']?\s*(?:#.*)?$", "critical", "GitHub Actions insecure command protocol is enabled", "CRT-CI-007", "ci", "high", "Remove ACTIONS_ALLOW_UNSECURE_COMMANDS and use environment files for workflow commands.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("CI_NODE_TLS_DISABLED", r"^\s*NODE_TLS_REJECT_UNAUTHORIZED\s*:\s*[\"']?0[\"']?\s*(?:#.*)?$", "high", "CI disables Node.js TLS certificate verification", "CRT-CI-008", "ci", "high", "Restore TLS verification and configure the correct trusted CA.", "policy", (".gitlab-ci.yml", ".gitlab-ci.yaml", ".circleci/config.yml", ".circleci/config.yaml", ".github/workflows/*.yml", ".github/workflows/*.yaml")),
    DetectionRule("COMPOSE_SECCOMP_UNCONFINED", r"^\s*-?\s*seccomp\s*:\s*unconfined\s*(?:#.*)?$", "high", "Container seccomp confinement is disabled", "CRT-IAC-013", "iac", "high", "Use the runtime default or a narrowly tailored seccomp profile.", "policy", ("docker-compose.yml", "docker-compose.yaml", "**/docker-compose.yml", "**/docker-compose.yaml", "compose.yml", "compose.yaml", "**/compose.yml", "**/compose.yaml")),
    DetectionRule("COMPOSE_APPARMOR_UNCONFINED", r"^\s*-?\s*apparmor\s*:\s*unconfined\s*(?:#.*)?$", "high", "Container AppArmor confinement is disabled", "CRT-IAC-014", "iac", "high", "Apply the runtime default or an approved AppArmor profile.", "policy", ("docker-compose.yml", "docker-compose.yaml", "**/docker-compose.yml", "**/docker-compose.yaml", "compose.yml", "compose.yaml", "**/compose.yml", "**/compose.yaml")),
    DetectionRule("K8S_WRITABLE_ROOT_FS", r"^\s*readOnlyRootFilesystem\s*:\s*false\s*(?:#.*)?$", "medium", "Kubernetes container root filesystem is explicitly writable", "CRT-IAC-015", "iac", "high", "Set readOnlyRootFilesystem to true and mount only required writable paths.", "policy", ("k8s/*.yml", "k8s/*.yaml", "k8s/**/*.yml", "k8s/**/*.yaml", "kubernetes/**/*.yml", "kubernetes/**/*.yaml", "manifests/*.yml", "manifests/*.yaml", "manifests/**/*.yml", "manifests/**/*.yaml")),
    DetectionRule("K8S_AUTOMOUNT_SA_TOKEN", r"^\s*automountServiceAccountToken\s*:\s*true\s*(?:#.*)?$", "medium", "Kubernetes service-account token is explicitly automounted", "CRT-IAC-016", "iac", "high", "Disable token automount unless the workload explicitly needs Kubernetes API access.", "policy", ("k8s/*.yml", "k8s/*.yaml", "k8s/**/*.yml", "k8s/**/*.yaml", "kubernetes/**/*.yml", "kubernetes/**/*.yaml", "manifests/*.yml", "manifests/*.yaml", "manifests/**/*.yml", "manifests/**/*.yaml")),
    DetectionRule("TF_STORAGE_ENCRYPTION_OFF", r"^\s*storage_encrypted\s*=\s*false\s*(?:#.*)?$", "high", "Terraform explicitly disables storage encryption", "CRT-IAC-017", "iac", "high", "Enable storage encryption and use an approved key policy.", "policy", ("*.tf", "**/*.tf")),
    DetectionRule("PIP_HTTP_INDEX", r"\bpip(?:3)?\s+install\b[^\n]*--(?:extra-)?index-url\s+http://[^\s]+", "high", "pip uses an unencrypted package index", "CRT-SUP-007", "supply-chain", "high", "Use an authenticated HTTPS package index with a valid trusted certificate.", "policy", ("requirements*.txt", "**/requirements*.txt", "Dockerfile", "**/Dockerfile", "Dockerfile.*", "**/Dockerfile.*", ".github/workflows/*.yml", ".github/workflows/*.yaml", ".gitlab-ci.yml", ".gitlab-ci.yaml", ".circleci/config.yml", ".circleci/config.yaml")),
    DetectionRule("NPM_HTTP_REGISTRY", r"(?:\bnpm\s+config\s+set\s+registry\s+http://[^\s]+|^\s*registry\s*=\s*http://[^\s]+)", "high", "npm uses an unencrypted package registry", "CRT-SUP-008", "supply-chain", "high", "Use an authenticated HTTPS npm registry with a valid trusted certificate.", "policy", (".npmrc", "**/.npmrc", "Dockerfile", "**/Dockerfile", "Dockerfile.*", "**/Dockerfile.*", ".github/workflows/*.yml", ".github/workflows/*.yaml", ".gitlab-ci.yml", ".gitlab-ci.yaml", ".circleci/config.yml", ".circleci/config.yaml")),
    DetectionRule("GO_INSECURE_WILDCARD", r"\bGOINSECURE\s*=\s*[\"']?\*[\"']?\s*(?:#.*)?$", "high", "Go module security bypass applies to all domains", "CRT-SUP-009", "supply-chain", "high", "Remove the wildcard and use authenticated HTTPS module sources.", "policy", ("*.env", "**/*.env", ".env", "**/.env", "Dockerfile", "**/Dockerfile", "Dockerfile.*", "**/Dockerfile.*", ".github/workflows/*.yml", ".github/workflows/*.yaml", ".gitlab-ci.yml", ".gitlab-ci.yaml", ".circleci/config.yml", ".circleci/config.yaml")),
    DetectionRule("AGENT_DELETE_ROOT", r"\brm\s+-rf\s+/(?:\s|$)", "critical", "AI-agent instructions recursively delete the filesystem root", "CRT-AI-007", "ai-agent", "high", "Remove destructive root deletion and require narrowly scoped, reviewed cleanup paths.", "policy", ("AGENTS.md", "**/AGENTS.md", "CLAUDE.md", "**/CLAUDE.md", ".cursorrules", "**/.cursorrules", ".github/copilot-instructions.md")),
    DetectionRule("AGENT_DISABLE_AUDIT_LOGGING", r"\bdisable\s+(?:all\s+)?audit\s+log(?:ging|s)?\b", "high", "AI-agent instructions disable audit logging", "CRT-AI-008", "ai-agent", "high", "Keep audit logging enabled and document reviewed redaction requirements.", "policy", ("AGENTS.md", "**/AGENTS.md", "CLAUDE.md", "**/CLAUDE.md", ".cursorrules", "**/.cursorrules", ".github/copilot-instructions.md")),
]


V3_STAGE9_SECRET_RULES: list[DetectionRule] = [
    DetectionRule("GITHUB_FINE_GRAINED_PAT", r"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{82}(?![A-Za-z0-9_])", "critical", "GitHub fine-grained personal access token", "CRT-SEC-045", "secret", "high", "Revoke the fine-grained PAT and create a least-privilege replacement.", "secret"),
    DetectionRule("SLACK_APP_TOKEN", r"(?<![A-Za-z0-9_])xapp-[0-9]+-[A-Za-z0-9]+-[0-9]+-[A-Za-z0-9]{24,}(?![A-Za-z0-9_])", "critical", "Slack app-level token", "CRT-SEC-046", "secret", "high", "Revoke the Slack app token and load a replacement from a secret store.", "secret"),
    DetectionRule("STRIPE_RESTRICTED_KEY", r"(?<![A-Za-z0-9_])rk_live_[A-Za-z0-9]{24,}(?![A-Za-z0-9_])", "critical", "Stripe live restricted key", "CRT-SEC-047", "secret", "high", "Roll the restricted key and preserve only required Stripe permissions.", "secret"),
    DetectionRule("PULUMI_ACCESS_TOKEN", r"(?<![A-Za-z0-9_])pul-[a-f0-9]{40}(?![A-Za-z0-9_])", "critical", "Pulumi access token", "CRT-SEC-048", "secret", "high", "Revoke the Pulumi token and use a scoped replacement from a secret store.", "secret"),
    DetectionRule("DOPPLER_PERSONAL_TOKEN", r"(?<![A-Za-z0-9_])dp\.pt\.[a-z0-9]{43}(?![A-Za-z0-9_])", "critical", "Doppler personal token", "CRT-SEC-049", "secret", "high", "Revoke the Doppler personal token and issue a replacement.", "secret"),
    DetectionRule("REPLICATE_API_TOKEN", r"(?<![A-Za-z0-9_])r8_[A-Za-z0-9]{37}(?![A-Za-z0-9_])", "critical", "Replicate API token", "CRT-SEC-050", "secret", "high", "Revoke the Replicate token and load a replacement from a secret store.", "secret"),
    DetectionRule("GROQ_API_KEY", r"(?<![A-Za-z0-9_])gsk_[A-Za-z0-9]{52}(?![A-Za-z0-9_])", "critical", "Groq API key", "CRT-SEC-051", "secret", "high", "Revoke the Groq key and load a replacement from a secret store.", "secret"),
    DetectionRule("PERPLEXITY_API_KEY", r"(?<![A-Za-z0-9_])pplx-[A-Za-z0-9]{48}(?![A-Za-z0-9_])", "critical", "Perplexity API key", "CRT-SEC-052", "secret", "high", "Revoke the Perplexity key and load a replacement from a secret store.", "secret"),
    DetectionRule("LANGSMITH_API_KEY_LEGACY", r"(?<![A-Za-z0-9_])lsv2_pt_[A-Za-z0-9]{40}_[0-9a-f]{10}(?![A-Za-z0-9_])", "critical", "LangSmith API key", "CRT-SEC-053", "secret", "high", "Revoke the LangSmith key and issue a least-privilege replacement.", "secret"),
    DetectionRule("PINECONE_API_KEY", r"(?<![A-Za-z0-9_])pcsk_[A-Za-z0-9]{40}(?![A-Za-z0-9_])", "critical", "Pinecone API key", "CRT-SEC-054", "secret", "high", "Revoke the Pinecone key and load a scoped replacement from a secret store.", "secret"),
    DetectionRule("GRAFANA_SERVICE_ACCOUNT_TOKEN", r"(?<![A-Za-z0-9_])glsa_[A-Za-z0-9]{32}_[0-9a-f]{8}(?![A-Za-z0-9_])", "critical", "Grafana service-account token", "CRT-SEC-055", "secret", "high", "Revoke the Grafana service-account token and issue a scoped replacement.", "secret"),
    DetectionRule("SENTRY_ORG_AUTH_TOKEN", r"(?<![A-Za-z0-9_])sntrys_eyJ[A-Za-z0-9_-]{40,200}_[A-Za-z0-9_-]{43}(?![A-Za-z0-9_])", "critical", "Sentry organization authentication token", "CRT-SEC-056", "secret", "high", "Revoke the Sentry organization token and load a replacement from a secret store.", "secret"),
    DetectionRule("DATABRICKS_PAT", r"(?<![A-Za-z0-9_])dapi[0-9a-f]{32}(?:-[0-9])?(?![A-Za-z0-9_])", "critical", "Databricks personal access token", "CRT-SEC-057", "secret", "high", "Revoke the Databricks PAT and issue a least-privilege replacement.", "secret"),
    DetectionRule("TERRAFORM_CLOUD_TOKEN", r"(?<![A-Za-z0-9_])[A-Za-z0-9]{14}\.atlasv1\.[A-Za-z0-9_=.-]{60,70}(?![A-Za-z0-9_])", "critical", "Terraform Cloud or Enterprise token", "CRT-SEC-058", "secret", "high", "Revoke the Terraform token and use a scoped replacement from a secret store.", "secret"),
    DetectionRule("VAULT_SERVICE_TOKEN", r"(?<![A-Za-z0-9_])hvs\.[A-Za-z0-9_-]{90,120}(?![A-Za-z0-9_])", "critical", "HashiCorp Vault service token", "CRT-SEC-059", "secret", "high", "Revoke the Vault service token and replace it with a short-lived scoped token.", "secret"),
    DetectionRule("VAULT_BATCH_TOKEN", r"(?<![A-Za-z0-9_])hvb\.[A-Za-z0-9_-]{138,300}(?![A-Za-z0-9_])", "critical", "HashiCorp Vault batch token", "CRT-SEC-060", "secret", "high", "Revoke the Vault batch token and replace it with a short-lived scoped token.", "secret"),
    DetectionRule("NEW_RELIC_USER_KEY", r"(?<![A-Za-z0-9_])NRAK-[A-Z0-9]{27}(?![A-Za-z0-9_])", "critical", "New Relic user API key", "CRT-SEC-061", "secret", "high", "Revoke the New Relic key and issue a least-privilege replacement.", "secret"),
    DetectionRule("CIRCLECI_PERSONAL_TOKEN", r"(?<![A-Za-z0-9_])CCIPAT_[A-Za-z0-9]{40}(?![A-Za-z0-9_])", "critical", "CircleCI personal API token", "CRT-SEC-062", "secret", "high", "Revoke the CircleCI token and issue a least-privilege replacement.", "secret"),
]

for _rule in V3_STAGE9_SECRET_RULES:
    _rule.flags = 0


V3_STAGE10_SECRET_RULES: list[DetectionRule] = [
    DetectionRule("ONEPASSWORD_SECRET_KEY", r"(?<![A-Za-z0-9])A3-[A-Z0-9]{6}-(?:[A-Z0-9]{11}|[A-Z0-9]{6}-[A-Z0-9]{5})-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}(?![A-Za-z0-9])", "critical", "1Password account secret key", "CRT-SEC-063", "secret", "high", "Revoke affected sessions, rotate the 1Password Secret Key and review account access."),
    DetectionRule("ONEPASSWORD_SERVICE_ACCOUNT_TOKEN_JWT", r"(?<![A-Za-z0-9_])ops_eyJ[A-Za-z0-9+/]{250,}={0,3}(?![A-Za-z0-9+/=])", "critical", "1Password service-account token", "CRT-SEC-064", "secret", "high", "Revoke and recreate the 1Password service-account token, then move it to an approved secret store."),
    DetectionRule("AGE_SECRET_KEY", r"(?<![A-Z0-9-])AGE-SECRET-KEY-1[QPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L]{58}(?![QPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L])", "critical", "age private identity", "CRT-SEC-065", "secret", "high", "Remove and replace the exposed age identity, then re-encrypt data for a new recipient where required."),
    DetectionRule("AIRTABLE_PERSONAL_ACCESS_TOKEN", r"(?<![A-Za-z0-9])pat[A-Za-z0-9]{14}\.[a-f0-9]{64}(?![A-Za-z0-9])", "critical", "Airtable personal access token", "CRT-SEC-066", "secret", "high", "Revoke the Airtable personal access token, issue a scoped replacement and store it securely."),
    DetectionRule("CLICKHOUSE_CLOUD_API_SECRET", r"(?<![A-Za-z0-9])4b1d[A-Za-z0-9]{38}(?![A-Za-z0-9])", "critical", "ClickHouse Cloud API secret key", "CRT-SEC-067", "secret", "high", "Rotate the ClickHouse Cloud API key pair and store the replacement in a secret manager."),
    DetectionRule("CLOJARS_API_TOKEN", r"(?<![A-Za-z0-9_])CLOJARS_[a-f0-9]{60}(?![A-Za-z0-9])", "critical", "Clojars deployment token", "CRT-SEC-068", "secret", "high", "Revoke the Clojars token, create a replacement and update publishing automation securely."),
    DetectionRule("CLOUDFLARE_ORIGIN_CA_KEY", r"(?<![A-Za-z0-9])v1\.0-[a-f0-9]{24}-[a-f0-9]{146}(?![A-Za-z0-9])", "critical", "Cloudflare Origin CA private key", "CRT-SEC-069", "secret", "high", "Revoke the exposed Origin CA certificate and key, issue a replacement and update origin servers."),
    DetectionRule("DUFFEL_API_TOKEN", r"(?<![A-Za-z0-9_])duffel_(?:test|live)_[A-Za-z0-9_=-]{43}(?![A-Za-z0-9_=-])", "critical", "Duffel API token", "CRT-SEC-070", "secret", "high", "Revoke and rotate the Duffel API token and keep the replacement outside source control."),
    DetectionRule("DYNATRACE_API_TOKEN", r"(?<![A-Za-z0-9])dt0c01\.[A-Za-z0-9]{24}\.[A-Za-z0-9]{64}(?![A-Za-z0-9])", "critical", "Dynatrace API token", "CRT-SEC-071", "secret", "high", "Revoke the Dynatrace token, create a least-privilege replacement and store it securely."),
    DetectionRule("FRAMEIO_API_TOKEN", r"(?<![A-Za-z0-9-])fio-u-[A-Za-z0-9_=-]{64}(?![A-Za-z0-9_=-])", "critical", "Frame.io user API token", "CRT-SEC-072", "secret", "high", "Revoke the Frame.io token, issue a replacement and remove the exposed value from history."),
    DetectionRule("GITLAB_CICD_JOB_TOKEN", r"(?<![A-Za-z0-9_-])glcbt-[A-Za-z0-9]{1,5}_[A-Za-z0-9_-]{20}(?![A-Za-z0-9_-])", "critical", "GitLab CI/CD job token", "CRT-SEC-073", "secret", "high", "Cancel or invalidate the affected GitLab job token and review job and project access logs."),
    DetectionRule("GITLAB_DEPLOY_TOKEN", r"(?<![A-Za-z0-9_-])gldt-[A-Za-z0-9_-]{20}(?![A-Za-z0-9_-])", "critical", "GitLab deploy token", "CRT-SEC-074", "secret", "high", "Revoke the GitLab deploy token, create a scoped replacement and update deployment secrets."),
    DetectionRule("GITLAB_RUNNER_AUTH_TOKEN", r"(?<![A-Za-z0-9_])(?:glrt-[A-Za-z0-9_-]{20}|glrt-t\d_[A-Za-z0-9_-]{27,300}\.[a-z0-9]{2}[a-z0-9]{7})(?![A-Za-z0-9_-])", "critical", "GitLab runner authentication token", "CRT-SEC-075", "secret", "high", "Rotate the GitLab runner authentication token and verify the runner registration."),
    DetectionRule("HEROKU_API_KEY_V2", r"(?<![A-Za-z0-9_-])HRKU-AA[A-Za-z0-9_-]{58}(?![A-Za-z0-9_-])", "critical", "Heroku API key v2", "CRT-SEC-076", "secret", "high", "Revoke the Heroku API key, create a replacement and update integrations through secure configuration."),
    DetectionRule("INFRACOST_API_TOKEN", r"(?<![A-Za-z0-9-])ico-[A-Za-z0-9]{32}(?![A-Za-z0-9])", "critical", "Infracost API token", "CRT-SEC-077", "secret", "high", "Revoke and replace the Infracost API token and store it in the CI secret facility."),
    DetectionRule("POSTMAN_API_TOKEN", r"(?<![A-Za-z0-9-])PMAK-[a-f0-9]{24}-[a-f0-9]{34}(?![A-Za-z0-9])", "critical", "Postman API token", "CRT-SEC-078", "secret", "high", "Revoke the Postman API token, issue a replacement and review workspace activity."),
    DetectionRule("SENTRY_USER_AUTH_TOKEN", r"(?<![A-Za-z0-9_])sntryu_[a-f0-9]{64}(?![A-Za-z0-9])", "critical", "Sentry user authentication token", "CRT-SEC-079", "secret", "high", "Revoke the Sentry user token, create a scoped replacement and review user activity."),
    DetectionRule("PLANETSCALE_API_TOKEN", r"(?<![A-Za-z0-9_])pscale_tkn_[A-Za-z0-9+/]{32,64}={0,3}(?![A-Za-z0-9+/=])", "critical", "PlanetScale API token", "CRT-SEC-080", "secret", "high", "Revoke the PlanetScale token, create a scoped replacement and store it securely."),
]

for _rule in V3_STAGE10_SECRET_RULES:
    _rule.flags = 0
    _rule.unicode_boundaries = True


V3_STAGE11_SECRET_RULES: list[DetectionRule] = [
    DetectionRule("ADOBE_CLIENT_SECRET", r"p8e-[A-Za-z0-9]{32}", "critical", "Adobe client secret", "CRT-SEC-081", "secret", "high", "Rotate the Adobe client secret and update integrations through an approved secret store."),
    DetectionRule("FLYIO_ACCESS_TOKEN", r"(?:fo1_[A-Za-z0-9_-]{43}|fm1[ar]_[A-Za-z0-9+/]{100,}={0,3}|fm2_[A-Za-z0-9+/]{100,}={0,3})(?![A-Za-z0-9_+/=-])", "critical", "Fly.io access token", "CRT-SEC-082", "secret", "high", "Revoke the Fly.io token, issue a scoped replacement and review organization access."),
    DetectionRule("GRAFANA_CLOUD_API_TOKEN", r"glc_[A-Za-z0-9+/]{32,400}={0,3}(?![A-Za-z0-9+/=])", "critical", "Grafana Cloud API token", "CRT-SEC-083", "secret", "high", "Revoke and replace the Grafana Cloud token and store it securely."),
    DetectionRule("HARNESS_API_KEY", r"(?:pat|sat)\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9]{24}\.[A-Za-z0-9]{20}(?![A-Za-z0-9_.-])", "critical", "Harness personal or service-account API key", "CRT-SEC-084", "secret", "high", "Revoke the Harness key, create a least-privilege replacement and audit recent use."),
    DetectionRule("HUGGINGFACE_ORG_TOKEN", r"api_org_[A-Za-z]{34}", "critical", "Hugging Face organization token", "CRT-SEC-085", "secret", "high", "Revoke the organization token, issue a scoped replacement and update consumers securely."),
    DetectionRule("OCTOPUS_DEPLOY_API_KEY", r"API-[A-Z0-9]{26}", "critical", "Octopus Deploy API key", "CRT-SEC-086", "secret", "high", "Revoke the Octopus Deploy API key and create a least-privilege replacement."),
    DetectionRule("OPENSHIFT_USER_TOKEN", r"sha256~[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])", "critical", "OpenShift user access token", "CRT-SEC-087", "secret", "high", "Revoke the OpenShift user token and review cluster access activity."),
    DetectionRule("PREFECT_API_TOKEN", r"pnu_[A-Za-z0-9]{36}", "critical", "Prefect API token", "CRT-SEC-088", "secret", "high", "Revoke the Prefect token, issue a replacement and store it outside source control."),
    DetectionRule("README_API_TOKEN", r"rdme_[a-z0-9]{70}", "critical", "ReadMe API token", "CRT-SEC-089", "secret", "high", "Revoke and replace the ReadMe API token and review recent API activity."),
    DetectionRule("RUBYGEMS_API_TOKEN", r"rubygems_[a-f0-9]{48}", "critical", "RubyGems API token", "CRT-SEC-090", "secret", "high", "Revoke the RubyGems token, issue a scoped replacement and secure publishing automation."),
    DetectionRule("SCALINGO_API_TOKEN", r"tk-us-[A-Za-z0-9_-]{48}(?![A-Za-z0-9_-])", "critical", "Scalingo API token", "CRT-SEC-091", "secret", "high", "Revoke and replace the Scalingo token and review account activity."),
    DetectionRule("BREVO_API_TOKEN", r"xkeysib-[a-f0-9]{64}-[A-Za-z0-9]{16}", "critical", "Brevo API token", "CRT-SEC-092", "secret", "high", "Revoke the Brevo API token, create a replacement and update integrations securely."),
    DetectionRule("SHIPPO_API_TOKEN", r"shippo_(?:live|test)_[A-Fa-f0-9]{40}", "critical", "Shippo API token", "CRT-SEC-093", "secret", "high", "Revoke the Shippo token, issue a replacement and review shipping API activity."),
    DetectionRule("SOURCEGRAPH_ACCESS_TOKEN", r"sgp_(?:(?:[A-Fa-f0-9]{16}|local)_[A-Fa-f0-9]{40}|[A-Fa-f0-9]{40})", "critical", "Sourcegraph access token", "CRT-SEC-094", "secret", "high", "Revoke the Sourcegraph token, create a scoped replacement and audit recent use."),
    DetectionRule("SQUARE_ACCESS_TOKEN", r"(?:EAAA|sq0atp-)[A-Za-z0-9_]{22,60}(?![A-Za-z0-9_])", "critical", "Square access token", "CRT-SEC-095", "secret", "high", "Revoke the Square token, issue a replacement and review payment API activity."),
    DetectionRule("MAXMIND_LICENSE_KEY", r"[A-Za-z0-9]{6}_[A-Za-z0-9]{29}_mmk", "critical", "MaxMind license key", "CRT-SEC-096", "secret", "high", "Deactivate and replace the MaxMind license key and update authorized download clients."),
    DetectionRule("PLANETSCALE_OAUTH_TOKEN", r"pscale_oauth_[A-Za-z0-9+/]{32,64}={0,3}(?![A-Za-z0-9+/=])", "critical", "PlanetScale OAuth token", "CRT-SEC-097", "secret", "high", "Revoke the PlanetScale OAuth token, create a replacement and store it securely."),
    DetectionRule("SETTLEMINT_SERVICE_TOKEN", r"sm_sat_[A-Za-z0-9]{16}", "critical", "SettleMint service access token", "CRT-SEC-098", "secret", "high", "Revoke the SettleMint service token and issue a replacement through secure configuration."),
]

for _rule in V3_STAGE11_SECRET_RULES:
    _rule.flags = 0
    _rule.unicode_boundaries = True

V3_STAGE12_SECRET_RULES: list[DetectionRule] = [
    DetectionRule("ATLASSIAN_TOKEN", r"ATATT3[A-Za-z0-9_\-=]{186}(?![A-Za-z0-9_\-=])", "critical", "Atlassian API token", "CRT-SEC-099", "secret", "high", "Revoke the Atlassian token and issue a replacement through secure configuration."),
    DetectionRule("NPM_ACCESS_TOKEN", r"(?<![A-Za-z0-9_])npm_[a-z0-9]{36}(?![A-Za-z0-9_])", "critical", "npm access token", "CRT-SEC-100", "secret", "high", "Revoke the npm token and issue a replacement through secure configuration."),
    DetectionRule("GITLAB_FEATURE_FLAG_TOKEN", r"glffct-[A-Za-z0-9_-]{20}(?![A-Za-z0-9_-])", "critical", "GitLab feature flag client token", "CRT-SEC-101", "secret", "high", "Revoke the GitLab feature flag token and issue a replacement through secure configuration."),
    DetectionRule("DIGITALOCEAN_OAUTH", r"doo_v1_[a-f0-9]{64}(?![a-f0-9])", "critical", "DigitalOcean OAuth token", "CRT-SEC-102", "secret", "high", "Revoke the DigitalOcean OAuth token and issue a replacement through secure configuration."),
    DetectionRule("EASYPOST_API_KEY", r"EZAK[A-Za-z0-9]{54}(?![A-Za-z0-9])", "critical", "EasyPost API key", "CRT-SEC-103", "secret", "high", "Revoke the EasyPost API key and issue a replacement through secure configuration."),
    DetectionRule("EASYPOST_TEST_KEY", r"EZTK[A-Za-z0-9]{54}(?![A-Za-z0-9])", "critical", "EasyPost test key", "CRT-SEC-104", "secret", "high", "Revoke the EasyPost test key and issue a replacement through secure configuration."),
    DetectionRule("GOCARDLESS_TOKEN", r"(?<![A-Za-z0-9_])live_[A-Za-z0-9_\-=]{40}(?![A-Za-z0-9_\-=])", "critical", "GoCardless API token", "CRT-SEC-105", "secret", "high", "Revoke the GoCardless token and issue a replacement through secure configuration."),
    DetectionRule("LOB_API_KEY", r"(?<![A-Za-z0-9_])(?:live|test)_[a-f0-9]{35}(?![a-f0-9])", "critical", "Lob API key", "CRT-SEC-106", "secret", "high", "Revoke the Lob API key and issue a replacement through secure configuration."),
    DetectionRule("NEWRELIC_INSERT_KEY", r"NRII-[A-Za-z0-9-]{32}(?![A-Za-z0-9-])", "critical", "New Relic insert key", "CRT-SEC-107", "secret", "high", "Revoke the New Relic insert key and issue a replacement through secure configuration."),
    DetectionRule("NEWRELIC_BROWSER_TOKEN", r"NRJS-[a-f0-9]{19}(?![a-f0-9])", "critical", "New Relic browser API token", "CRT-SEC-108", "secret", "high", "Revoke the New Relic browser token and issue a replacement through secure configuration."),
    DetectionRule("DEFINED_NETWORKING_TOKEN", r"dnkey-[A-Za-z0-9=_-]{26}-[A-Za-z0-9=_-]{52}(?![A-Za-z0-9=_-])", "critical", "Defined Networking API token", "CRT-SEC-109", "secret", "high", "Revoke the Defined Networking token and issue a replacement through secure configuration."),
    DetectionRule("SONAR_TOKEN", r"(?<![A-Za-z0-9_])(?:squ|sqp|sqa)_[A-Za-z0-9=_-]{40}(?![A-Za-z0-9=_-])", "critical", "SonarQube/SonarCloud token", "CRT-SEC-110", "secret", "high", "Revoke the SonarQube token and issue a replacement through secure configuration."),
    DetectionRule("TYPEFORM_TOKEN", r"tfp_[A-Za-z0-9_.\-]{59}(?![A-Za-z0-9_.\-])", "critical", "Typeform API token", "CRT-SEC-111", "secret", "high", "Revoke the Typeform token and issue a replacement through secure configuration."),
    DetectionRule("META_ACCESS_TOKEN", r"EA[AM]C[A-Za-z0-9]{100,}(?![A-Za-z0-9])", "critical", "Meta/Facebook page access token", "CRT-SEC-112", "secret", "high", "Revoke the Meta access token and issue a replacement through secure configuration."),
    DetectionRule("ALIBABA_ACCESS_KEY", r"LTAI[A-Za-z0-9]{20}(?![A-Za-z0-9])", "critical", "Alibaba Cloud access key ID", "CRT-SEC-113", "secret", "high", "Revoke the Alibaba Cloud access key and issue a replacement through secure configuration."),
    DetectionRule("ARTIFACTORY_KEY", r"AKCp[A-Za-z0-9]{69}(?![A-Za-z0-9])", "critical", "JFrog Artifactory API key", "CRT-SEC-114", "secret", "high", "Revoke the Artifactory API key and issue a replacement through secure configuration."),
    DetectionRule("NOTION_API_TOKEN", r"ntn_[0-9]{11}[A-Za-z0-9]{35}(?![A-Za-z0-9])", "critical", "Notion API token", "CRT-SEC-115", "secret", "high", "Revoke the Notion API token and issue a replacement through secure configuration."),
    DetectionRule("FLUTTERWAVE_SECRET_KEY", r"FLWSECK_TEST-[A-Ha-h0-9]{32}-X(?![A-Za-z0-9])", "critical", "Flutterwave test secret key", "CRT-SEC-116", "secret", "high", "Revoke the Flutterwave secret key and issue a replacement through secure configuration."),
]

for _rule in V3_STAGE12_SECRET_RULES:
    _rule.flags = 0
    _rule.unicode_boundaries = True

for _rule in V3_STAGE12_SECRET_RULES:
    if _rule.rule_id in {"CRT-SEC-100", "CRT-SEC-105", "CRT-SEC-106", "CRT-SEC-107", "CRT-SEC-108", "CRT-SEC-109", "CRT-SEC-110", "CRT-SEC-111"}:
        _rule.flags = re.IGNORECASE


V3_STAGE13_SECRET_RULES: list[DetectionRule] = [
    DetectionRule("AUTHRESS_SERVICE_KEY", r"(?:sc|ext|scauth|authress)_[A-Za-z0-9]{5,30}\.[A-Za-z0-9]{4,6}\.acc[_-][A-Za-z0-9-]{10,32}\.[A-Za-z0-9+/_=-]{30,120}(?![A-Za-z0-9+/_=-])", "critical", "Authress service key", "CRT-SEC-117", "secret", "high", "Revoke and replace the Authress key."),
    DetectionRule("BEDROCK_SHORT_LIVED_KEY", r"bedrock-api-key-" + r"YmVkcm9jay5hbWF6b25hd3MuY29t", "critical", "Amazon Bedrock short-lived key", "CRT-SEC-118", "secret", "high", "Revoke and replace the Bedrock key."),
    DetectionRule("GITLAB_FEED_TOKEN", r"glft-[A-Za-z0-9_-]{20}(?![A-Za-z0-9_-])", "critical", "GitLab feed token", "CRT-SEC-119", "secret", "high", "Revoke and replace the GitLab token."),
    DetectionRule("GITLAB_INCOMING_MAIL_TOKEN", r"glimt-[A-Za-z0-9_-]{25}(?![A-Za-z0-9_-])", "critical", "GitLab incoming mail token", "CRT-SEC-120", "secret", "high", "Revoke and replace the GitLab token."),
    DetectionRule("GITLAB_AGENT_TOKEN", r"glagent-[A-Za-z0-9_-]{50}(?![A-Za-z0-9_-])", "critical", "GitLab Kubernetes agent token", "CRT-SEC-121", "secret", "high", "Revoke and replace the GitLab token."),
    DetectionRule("GITLAB_OAUTH_SECRET", r"gloas-[A-Za-z0-9_-]{64}(?![A-Za-z0-9_-])", "critical", "GitLab OAuth secret", "CRT-SEC-122", "secret", "high", "Revoke and replace the GitLab secret."),
    DetectionRule("GITLAB_PIPELINE_TRIGGER_TOKEN", r"glptt-[a-f0-9]{40}(?![a-f0-9])", "critical", "GitLab pipeline trigger token", "CRT-SEC-123", "secret", "high", "Revoke and replace the GitLab token."),
    DetectionRule("GITLAB_RRT", r"GR1348941[A-Za-z0-9_-]{20}(?![A-Za-z0-9_-])", "critical", "GitLab RRT token", "CRT-SEC-124", "secret", "high", "Revoke and replace the GitLab token."),
    DetectionRule("GITLAB_SCIM_TOKEN", r"glsoat-[A-Za-z0-9_-]{20}(?![A-Za-z0-9_-])", "critical", "GitLab SCIM token", "CRT-SEC-125", "secret", "high", "Revoke and replace the GitLab token."),
    DetectionRule("GITLAB_SESSION_COOKIE", r"_gitlab_session=[a-z0-9]{32}(?![a-z0-9])", "critical", "GitLab session cookie", "CRT-SEC-126", "secret", "high", "Invalidate the GitLab session."),
    DetectionRule("SHOPIFY_CUSTOM_TOKEN", r"shpca_[A-Fa-f0-9]{32}(?![A-Fa-f0-9])", "critical", "Shopify custom token", "CRT-SEC-127", "secret", "high", "Revoke and replace the Shopify token."),
    DetectionRule("SHOPIFY_PRIVATE_TOKEN", r"shppa_[A-Fa-f0-9]{32}(?![A-Fa-f0-9])", "critical", "Shopify private token", "CRT-SEC-128", "secret", "high", "Revoke and replace the Shopify token."),
    DetectionRule("SHOPIFY_SHARED_SECRET", r"shpss_[A-Fa-f0-9]{32}(?![A-Fa-f0-9])", "critical", "Shopify shared secret", "CRT-SEC-129", "secret", "high", "Rotate the Shopify shared secret."),
    DetectionRule("INTRA42_CLIENT_SECRET", r"s-s4t2(?:ud|af)-[A-Fa-f0-9]{64}(?![A-Fa-f0-9])", "critical", "42 client secret", "CRT-SEC-130", "secret", "high", "Revoke and replace the 42 client secret."),
    DetectionRule("SLACK_LEGACY_WORKSPACE_TOKEN", r"xox[ar]-(?:\d-)?[A-Za-z0-9]{8,48}(?![A-Za-z0-9])", "critical", "Slack legacy workspace token", "CRT-SEC-131", "secret", "high", "Revoke the legacy Slack token."),
    DetectionRule("SLACK_WEBHOOK_URL", r"(?:https?://)?hooks\.slack\.com/(?:services|workflows|triggers)/[A-Za-z0-9+/]{43,56}(?![A-Za-z0-9+/])", "critical", "Slack webhook URL", "CRT-SEC-132", "secret", "high", "Revoke and recreate the Slack webhook."),
    DetectionRule("SLACK_CONFIG_ACCESS_TOKEN", r"xoxe\.xox[bp]-\d-[A-Za-z0-9]{163,166}(?![A-Za-z0-9])", "critical", "Slack config access token", "CRT-SEC-133", "secret", "high", "Revoke and replace the Slack token."),
    DetectionRule("SLACK_CONFIG_REFRESH_TOKEN", r"xoxe-\d-[A-Za-z0-9]{146}(?![A-Za-z0-9])", "critical", "Slack config refresh token", "CRT-SEC-134", "secret", "high", "Revoke and replace the Slack token."),
]
for _rule in V3_STAGE13_SECRET_RULES:
    _rule.flags = 0
    _rule.unicode_boundaries = True
for _rule in V3_STAGE13_SECRET_RULES:
    if _rule.rule_id in {"CRT-SEC-133", "CRT-SEC-134"}:
        _rule.flags = re.IGNORECASE


V3_STAGE14_SECRET_RULES: list[DetectionRule] = [
    DetectionRule("FIGMA_PERSONAL_ACCESS_TOKEN", r"figd_[A-Za-z0-9_-]{40,200}(?![A-Za-z0-9_-])", "high", "Figma personal access token", "CRT-SEC-135", "secret", "high", "Revoke the Figma personal access token and replace it through a managed secret store."),
    DetectionRule("FIRECRAWL_API_KEY", r"fc-[A-Za-z0-9_-]{20,100}(?![A-Za-z0-9_-])", "high", "Firecrawl API key", "CRT-SEC-136", "secret", "high", "Revoke the Firecrawl API key and replace it through a managed secret store."),
    DetectionRule("TRIGGER_SECRET_KEY", r"tr_(?:dev|prod|stg|pat)_[A-Za-z0-9_-]{20,100}(?![A-Za-z0-9_-])", "high", "Trigger.dev secret key", "CRT-SEC-137", "secret", "high", "Revoke the Trigger.dev secret key and replace it through a managed secret store."),
    DetectionRule("TAVILY_API_KEY", r"tvly-[A-Za-z0-9_-]{20,100}(?![A-Za-z0-9_-])", "high", "Tavily API key", "CRT-SEC-138", "secret", "high", "Revoke the Tavily API key and replace it through a managed secret store."),
    DetectionRule("INNGEST_SIGNING_KEY", r"signkey-(?:prod|test|dev)-[a-f0-9]{64}(?![a-f0-9])", "high", "Inngest signing key", "CRT-SEC-139", "secret", "high", "Rotate the Inngest signing key and replace it through a managed secret store."),
    DetectionRule("POSTHOG_PERSONAL_API_KEY", r"phx_[A-Za-z0-9_-]{20,100}(?![A-Za-z0-9_-])", "high", "PostHog personal API key", "CRT-SEC-141", "secret", "high", "Revoke the PostHog personal API key and replace it through a managed secret store."),
    DetectionRule("SENTRY_AUTH_TOKEN_NONJWT", r"sntrys_(?!eyJ)[A-Za-z0-9_-]{20,200}(?![A-Za-z0-9_-])", "high", "Sentry auth token", "CRT-SEC-142", "secret", "high", "Revoke the Sentry auth token and replace it through a managed secret store."),
    DetectionRule("AIVEN_AUTH_HEADER_TOKEN", r"(?:Authorization|authorization):\s*aivenv1\s+[A-Za-z0-9_-]{20,200}(?![A-Za-z0-9_-])", "high", "Aiven API token in authorization header", "CRT-SEC-143", "secret", "high", "Revoke the Aiven API token and replace it through a managed secret store."),
    DetectionRule("FLYIO_AUTH_HEADER_TOKEN", r"(?:Authorization|authorization):\s*FlyV1\s+[A-Za-z0-9_+/=-]{20,300}(?![A-Za-z0-9_+/=-])", "high", "Fly.io API token in authorization header", "CRT-SEC-144", "secret", "high", "Revoke the Fly.io API token and replace it through a managed secret store."),
    DetectionRule("DYNATRACE_API_TOKEN_S01", r"(?<![A-Za-z0-9])dt0s01\.[A-Za-z0-9]{24}\.[A-Za-z0-9]{64}(?![A-Za-z0-9])", "high", "Dynatrace dt0s01 API token", "CRT-SEC-145", "secret", "high", "Revoke the Dynatrace API token and replace it through a managed secret store."),
    DetectionRule("BITRISE_WORKSPACE_API_TOKEN", r"(?<![A-Za-z0-9_])bitwat_[A-Za-z0-9_-]{20,200}(?![A-Za-z0-9_-])", "high", "Bitrise workspace API token", "CRT-SEC-146", "secret", "high", "Revoke the Bitrise workspace API token and replace it through a managed secret store."),
    DetectionRule("AWS_BEDROCK_LONG_TERM_API_KEY", r"(?<![A-Za-z0-9])ABSK[A-Za-z0-9+/]{128}(?![A-Za-z0-9+/=])", "high", "AWS Bedrock long-term API key", "CRT-SEC-147", "secret", "high", "Revoke the AWS Bedrock API key and replace it through a managed secret store."),
    DetectionRule("CARTESIA_API_KEY", r"(?<![A-Za-z0-9_])sk_car_[A-Za-z0-9_-]{20,200}(?![A-Za-z0-9_-])", "high", "Cartesia API key", "CRT-SEC-148", "secret", "high", "Revoke the Cartesia API key and replace it through a managed secret store."),
    DetectionRule("HUME_API_KEY_HEADER", r"(?i:(?<![A-Za-z0-9-])X-Hume-Api-Key:\s*[A-Za-z0-9_-]{20,200}(?![A-Za-z0-9_-]))", "high", "Hume API key in header", "CRT-SEC-149", "secret", "high", "Revoke the Hume API key and replace it through a managed secret store."),
    DetectionRule("RUBYGEMS_OIDC_API_KEY_ROLE", r"(?<![A-Za-z0-9_])rg_oidc_akr_[A-Za-z0-9_-]{20,200}(?![A-Za-z0-9_-])", "high", "RubyGems OIDC API key role token", "CRT-SEC-150", "secret", "high", "Revoke the RubyGems OIDC API key role token and replace it through a managed secret store."),
    DetectionRule("ONEPASSWORD_SERVICE_ACCOUNT_TOKEN", r"(?<![A-Za-z0-9_])ops_[A-Za-z0-9_-]{20,500}\.[A-Za-z0-9_-]{10,500}\.[A-Za-z0-9_-]{10,500}(?![A-Za-z0-9_.-])", "high", "1Password service account token", "CRT-SEC-151", "secret", "high", "Revoke the 1Password service account token and replace it through a managed secret store."),
    DetectionRule("LANGSMITH_API_KEY", r"(?<![A-Za-z0-9_])lsv2_pt_[A-Za-z0-9_-]{20,200}(?![A-Za-z0-9_-])", "high", "LangSmith API key", "CRT-SEC-152", "secret", "high", "Revoke the LangSmith API key and replace it through a managed secret store."),
    DetectionRule("CLOUDFLARE_ACCOUNT_API_TOKEN", r"(?<![A-Za-z0-9_])cfat_[A-Za-z0-9]{40}[0-9a-f]{8}(?![A-Za-z0-9_])", "high", "Cloudflare account API token", "CRT-SEC-153", "secret", "high", "Revoke the Cloudflare account API token and replace it through a managed secret store."),
    DetectionRule("RESEND_API_KEY", r"(?<![A-Za-z0-9_])re_[A-Za-z0-9_-]{20,200}(?![A-Za-z0-9_-])", "high", "Resend API key", "CRT-SEC-154", "secret", "high", "Revoke the Resend API key and replace it through a managed secret store."),
    DetectionRule("FIREWORKS_API_KEY", r"(?<![A-Za-z0-9_])fw_[A-Za-z0-9_-]{20,200}(?![A-Za-z0-9_-])", "high", "Fireworks API key", "CRT-SEC-155", "secret", "high", "Revoke the Fireworks API key and replace it through a managed secret store."),
    DetectionRule("FIREWORKS_FIRE_PASS_KEY", r"(?<![A-Za-z0-9_])fpk_[A-Za-z0-9_-]{20,200}(?![A-Za-z0-9_-])", "high", "Fireworks Fire Pass key", "CRT-SEC-156", "secret", "high", "Revoke the Fireworks Fire Pass key and replace it through a managed secret store."),
    DetectionRule("SUPABASE_SECRET_KEY", r"(?<![A-Za-z0-9_])sb_secret_[A-Za-z0-9_-]{20,200}(?![A-Za-z0-9_-])", "high", "Supabase secret key", "CRT-SEC-157", "secret", "high", "Revoke the Supabase secret key and replace it through a managed secret store."),
    DetectionRule("STRIPE_WEBHOOK_SIGNING_SECRET", r"(?<![A-Za-z0-9_])whsec_[A-Za-z0-9]{20,200}(?![A-Za-z0-9_])", "high", "Stripe webhook signing secret", "CRT-SEC-158", "secret", "high", "Rotate the Stripe webhook endpoint secret and replace it through a managed secret store."),
    DetectionRule("POSTHOG_FEATURE_FLAGS_SECURE_KEY", r"(?<![A-Za-z0-9_])phs_[A-Za-z0-9_-]{20,200}(?![A-Za-z0-9_-])", "high", "PostHog feature flags secure API key", "CRT-SEC-159", "secret", "high", "Revoke the PostHog feature flags secure API key and replace it through a managed secret store."),
    DetectionRule("POSTHOG_OAUTH_ACCESS_TOKEN", r"(?<![A-Za-z0-9_])pha_[A-Za-z0-9_-]{20,200}(?![A-Za-z0-9_-])", "high", "PostHog OAuth access token", "CRT-SEC-160", "secret", "high", "Revoke the PostHog OAuth access token and replace it through a managed secret store."),
    DetectionRule("OKTA_API_TOKEN", r"(?<![A-Za-z0-9_])SSWS[A-Za-z0-9_-]{20,200}(?![A-Za-z0-9_-])", "high", "Okta API token", "CRT-SEC-161", "secret", "high", "Revoke the Okta API token and replace it through a managed secret store."),
    DetectionRule("DOPPLER_SERVICE_TOKEN", r"(?<![A-Za-z0-9_])dp\.st\.[a-z0-9]{43}(?![A-Za-z0-9_])", "high", "Doppler service token", "CRT-SEC-162", "secret", "high", "Revoke the Doppler service token and replace it through a managed secret store."),
    DetectionRule("DOPPLER_CONFIG_TOKEN", r"(?<![A-Za-z0-9_])dp\.ct\.[a-z0-9]{43}(?![A-Za-z0-9_])", "high", "Doppler config token", "CRT-SEC-163", "secret", "high", "Revoke the Doppler config token and replace it through a managed secret store."),
    DetectionRule("DOPPLER_SERVICE_ACCOUNT_TOKEN", r"(?<![A-Za-z0-9_])dp\.sa\.[A-Za-z0-9]{43}(?![A-Za-z0-9_])", "high", "Doppler service account token", "CRT-SEC-164", "secret", "high", "Revoke the Doppler service account token and replace it through a managed secret store."),
    DetectionRule("DOPPLER_SCIM_TOKEN", r"(?<![A-Za-z0-9_])dp\.scim\.[A-Za-z0-9]{43}(?![A-Za-z0-9_])", "high", "Doppler SCIM token", "CRT-SEC-165", "secret", "high", "Revoke the Doppler SCIM token and replace it through a managed secret store."),
    DetectionRule("DOPPLER_AUDIT_TOKEN", r"(?<![A-Za-z0-9_])dp\.audit\.[A-Za-z0-9]{43}(?![A-Za-z0-9_])", "high", "Doppler audit token", "CRT-SEC-166", "secret", "high", "Revoke the Doppler audit token and replace it through a managed secret store."),
    DetectionRule("TAILSCALE_KEY", r"(?<![A-Za-z0-9_])tskey-(?:api|auth|oauth)-[A-Za-z0-9_]{1,50}-[A-Za-z0-9_]{1,50}(?![A-Za-z0-9_])", "high", "Tailscale API, auth, or OAuth key", "CRT-SEC-167", "secret", "high", "Revoke the Tailscale key and replace it through a managed secret store."),
    DetectionRule("TAILSCALE_SCIM_KEY", r"(?<![A-Za-z0-9_])tskey-scim-[A-Za-z0-9_]{1,50}-[A-Za-z0-9_]{1,50}(?![A-Za-z0-9_])", "high", "Tailscale SCIM key", "CRT-SEC-168", "secret", "high", "Revoke the Tailscale SCIM key and replace it through a managed secret store."),
    DetectionRule("TAILSCALE_CLIENT_KEY", r"(?<![A-Za-z0-9_])tskey-client-[A-Za-z0-9_]{1,50}-[A-Za-z0-9_]{1,50}(?![A-Za-z0-9_])", "high", "Tailscale client key", "CRT-SEC-169", "secret", "high", "Revoke the Tailscale client key and replace it through a managed secret store."),
    DetectionRule("TAILSCALE_WEBHOOK_KEY", r"(?<![A-Za-z0-9_])tskey-webhook-[A-Za-z0-9_]{1,50}-[A-Za-z0-9_]{1,50}(?![A-Za-z0-9_])", "high", "Tailscale webhook key", "CRT-SEC-170", "secret", "high", "Revoke the Tailscale webhook key and replace it through a managed secret store."),
    DetectionRule("VAULT_RECOVERY_TOKEN", r"(?<![A-Za-z0-9_])hvr\.[A-Za-z0-9_-]{24,300}(?![A-Za-z0-9_])", "critical", "HashiCorp Vault recovery token", "CRT-SEC-171", "secret", "high", "Revoke the Vault recovery token and replace it through a managed secret store."),
    DetectionRule("HONEYCOMB_INGEST_KEY", r"(?<![A-Za-z0-9_])hcaik_[A-Za-z0-9]{20,300}(?![A-Za-z0-9])", "high", "Honeycomb ingest API key", "CRT-SEC-172", "secret", "high", "Revoke the Honeycomb ingest API key and replace it through a managed secret store."),
    DetectionRule("ONE_PASSWORD_SERVICE_ACCOUNT_TOKEN", r"(?<![A-Za-z0-9_])ops_[A-Za-z0-9_-]{20,500}(?![A-Za-z0-9_-])", "high", "1Password service account token", "CRT-SEC-173", "secret", "high", "Revoke the 1Password service account token and replace it through a managed secret store."),
    DetectionRule("MAPBOX_SECRET_TOKEN", r"(?<![A-Za-z0-9_])sk\.[A-Za-z0-9_-]{40,500}\.[A-Za-z0-9_-]{40,500}\.[A-Za-z0-9_-]{40,500}(?![A-Za-z0-9_-])", "high", "Mapbox secret access token", "CRT-SEC-174", "secret", "high", "Revoke the Mapbox secret token and create a new one with URL restrictions."),
    DetectionRule("CONFLUENT_CLOUD_API_KEY", r"(?<![A-Za-z0-9])(?:[A-Z0-9]{16,20}\.[A-Z0-9]{16,20})(?![A-Za-z0-9])", "high", "Confluent Cloud API key (key/secret pair)", "CRT-SEC-175", "secret", "medium", "Revoke the Confluent Cloud API key and secret and replace them through a managed secret store."),
    DetectionRule("SENTRY_USER_APP_AUTH_TOKEN", r"(?<![A-Za-z0-9_])sntrya_[A-Za-z0-9_-]{20,300}(?![A-Za-z0-9_-])", "high", "Sentry user app auth token", "CRT-SEC-176", "secret", "high", "Revoke the Sentry user app auth token and replace it through a managed secret store."),
    DetectionRule("HONEYCOMB_ENVIRONMENT_KEY", r"(?<![A-Za-z0-9_])hcxik_[A-Za-z0-9]{20,300}(?![A-Za-z0-9])", "high", "Honeycomb environment API key", "CRT-SEC-177", "secret", "high", "Revoke the Honeycomb environment API key and replace it through a managed secret store."),
    DetectionRule("DIGITALOCEAN_PERSONAL_ACCESS_TOKEN", r"(?<![A-Za-z0-9_])dop_v1_[A-Za-z0-9_-]{20,300}(?![A-Za-z0-9_-])", "high", "DigitalOcean personal access token", "CRT-SEC-178", "secret", "high", "Revoke the DigitalOcean personal access token and replace it through a managed secret store."),
    DetectionRule("DIGITALOCEAN_OAUTH_TOKEN", r"(?<![A-Za-z0-9_])doo_v1_[A-Za-z0-9_-]{20,300}(?![A-Za-z0-9_-])", "high", "DigitalOcean OAuth token", "CRT-SEC-179", "secret", "high", "Revoke the DigitalOcean OAuth token and replace it through a managed secret store."),
    DetectionRule("PADDLE_API_KEY", r"(?<![A-Za-z0-9_])pdl_(?:live|sdbx)_apikey_[a-z\d]{26}_[a-zA-Z\d]{22}_[a-zA-Z\d]{3}(?![A-Za-z0-9_])", "critical", "Paddle API key", "CRT-SEC-180", "secret", "high", "Revoke the Paddle API key and replace it through a managed secret store."),
    DetectionRule("PADDLE_WEBHOOK_ENDPOINT_SECRET", r"(?<![A-Za-z0-9_])pdl_ntfset_[a-zA-Z0-9]{26}_[a-zA-Z0-9]{32}(?![A-Za-z0-9_])", "critical", "Paddle webhook endpoint secret", "CRT-SEC-181", "secret", "high", "Rotate the Paddle notification endpoint secret and replace it through a managed secret store."),
    DetectionRule("CLOUDINARY_API_CREDENTIAL_URL", r"(?<![A-Za-z0-9_])cloudinary://[0-9]{15}:[A-Za-z0-9]{27}@[A-Za-z0-9][A-Za-z0-9_-]{0,127}(?![A-Za-z0-9_])", "critical", "Cloudinary API credential URL", "CRT-SEC-182", "secret", "high", "Rotate the Cloudinary API key and secret and replace the credential URL through a managed secret store."),
    DetectionRule("AZURE_DEVOPS_PERSONAL_ACCESS_TOKEN_V2", r"(?<![A-Za-z0-9_])[A-Za-z0-9]{76}AZDO[A-Za-z0-9]{4}(?![A-Za-z0-9_])", "critical", "Azure DevOps personal access token (84-character format)", "CRT-SEC-183", "secret", "high", "Revoke the Azure DevOps personal access token and replace it through a managed secret store."),
    DetectionRule("CONFLUENT_CLOUD_API_SECRET_V2", r"(?<![A-Za-z0-9_])cflt([A-Za-z0-9+/]{54})([A-Za-z0-9+/]{6})(?![A-Za-z0-9_])", "critical", "Confluent Cloud API secret (checksum format)", "CRT-SEC-184", "secret", "high", "Revoke the Confluent Cloud API secret and replace it through a managed secret store.", validator=_confluent_secret_checksum),
]
for _rule in V3_STAGE14_SECRET_RULES:
    _rule.flags = 0
    _rule.unicode_boundaries = True


DEFAULT_DETECTION_RULES: list[DetectionRule] = DEFAULT_SECRET_PATTERNS + V3_STAGE1_RULES + V3_STAGE9_SECRET_RULES + V3_STAGE10_SECRET_RULES + V3_STAGE11_SECRET_RULES + V3_STAGE12_SECRET_RULES + V3_STAGE13_SECRET_RULES + V3_STAGE14_SECRET_RULES

K8S_CONTEXT_GLOBS = ("k8s/*.yml", "k8s/*.yaml", "k8s/**/*.yml", "k8s/**/*.yaml", "kubernetes/**/*.yml", "kubernetes/**/*.yaml", "manifests/*.yml", "manifests/*.yaml", "manifests/**/*.yml", "manifests/**/*.yaml")
AI_CONTEXT_GLOBS = ("AGENTS.md", "**/AGENTS.md", "CLAUDE.md", "**/CLAUDE.md", ".cursorrules", "**/.cursorrules", ".github/copilot-instructions.md")

DEFAULT_CONTEXT_RULES: list[ContextDetectionRule] = [
    ContextDetectionRule("GH_PR_TARGET_UNTRUSTED_CHECKOUT", (r"^\s*(?:on\s*:\s*)?pull_request_target\s*:", r"uses\s*:\s*actions/checkout@", r"ref\s*:\s*\$\{\{\s*github\.event\.pull_request\.head\.sha\s*\}\}"), 50, "critical", "pull_request_target workflow checks out untrusted pull-request code", "CRT-CI-009", "ci", "high", "Use pull_request for untrusted code or avoid checking out PR head code in privileged pull_request_target workflows.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    ContextDetectionRule("GH_PR_SELF_HOSTED_RUNNER", (r"^\s*pull_request\s*:", r"^\s*runs-on\s*:\s*self-hosted\s*$"), 50, "high", "pull_request workflow executes on a self-hosted runner", "CRT-CI-014", "ci", "high", "Use an isolated ephemeral runner for untrusted pull requests or require an explicit trusted-code boundary before scheduling on self-hosted infrastructure.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    ContextDetectionRule("GH_WORKFLOW_RUN_UNTRUSTED_CHECKOUT", (r"^\s*workflow_run\s*:", r"uses\s*:\s*actions/checkout@", r"ref\s*:\s*\$\{\{\s*github\.event\.workflow_run\.head_sha\s*\}\}"), 50, "critical", "workflow_run workflow checks out code from the untrusted triggering run", "CRT-CI-018", "ci", "high", "Do not check out untrusted workflow_run code in a privileged workflow; separate validation from privileged publication steps.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    ContextDetectionRule("GH_WORKFLOW_RUN_SELF_HOSTED_RUNNER", (r"^\s*workflow_run\s*:", r"^\s*runs-on\s*:\s*self-hosted\s*$"), 50, "high", "workflow_run workflow executes on a self-hosted runner", "CRT-CI-019", "ci", "high", "Use an isolated ephemeral runner and keep untrusted workflow_run data out of privileged self-hosted infrastructure.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    ContextDetectionRule("GH_PR_TARGET_HEAD_REPOSITORY", (r"^\s*pull_request_target\s*:", r"uses\s*:\s*actions/checkout@", r"repository\s*:\s*\$\{\{\s*github\.event\.pull_request\.head\.repo\.full_name\s*\}\}"), 50, "critical", "pull_request_target checks out the pull request head repository", "CRT-CI-021", "ci", "high", "Do not check out fork or pull request head repositories from a privileged pull_request_target workflow.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    ContextDetectionRule("GH_PR_TARGET_HEAD_REF_CHECKOUT", (r"^\s*pull_request_target\s*:", r"uses\s*:\s*actions/checkout@", r"ref\s*:\s*\$\{\{\s*github\.event\.pull_request\.head\.ref\s*\}\}"), 50, "critical", "pull_request_target checks out the pull request head branch", "CRT-CI-022", "ci", "high", "Use a trusted base ref in privileged workflows and never execute pull request head code under pull_request_target.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    ContextDetectionRule("GH_WORKFLOW_RUN_ARTIFACT_EXECUTION", (r"^\s*workflow_run\s*:", r"uses\s*:\s*actions/download-artifact@", r"^\s*run\s*:\s*(?:\./|/tmp/|(?:ba)?sh\s+)"), 50, "critical", "workflow_run downloads an artifact and executes a local file", "CRT-CI-023", "ci", "high", "Verify artifact provenance and integrity before execution, and keep privileged workflow_run steps separate from untrusted artifacts.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    ContextDetectionRule("GH_PR_WRITE_CONTENTS_PERMISSION", (r"^\s*pull_request\s*:", r"^\s*permissions\s*:", r"^\s*contents\s*:\s*write\s*$"), 50, "high", "pull_request workflow grants contents write permission", "CRT-CI-024", "ci", "high", "Use read-only permissions for pull request workflows and grant write access only in a separate trusted workflow.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    ContextDetectionRule("GH_CHECKOUT_PERSIST_CREDENTIALS", (r"^\s*-?\s*uses\s*:\s*actions/checkout@", r"^\s*persist-credentials\s*:\s*true\s*$"), 12, "high", "actions/checkout persists repository credentials in the local checkout", "CRT-CI-055", "ci", "high", "Set persist-credentials to false unless the workflow explicitly requires a reviewed authenticated git push.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    ContextDetectionRule("GH_UPLOAD_ARTIFACT_HIDDEN_FILES", (r"^\s*-?\s*uses\s*:\s*actions/upload-artifact@", r"^\s*include-hidden-files\s*:\s*true\s*$"), 12, "high", "actions/upload-artifact includes hidden files in a workflow artifact", "CRT-CI-056", "ci", "high", "Keep include-hidden-files false unless every hidden file in the upload path has been reviewed for sensitive content.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    ContextDetectionRule("GH_PR_TARGET_CACHE_USE", (r"^\s*(?:on\s*:\s*)?pull_request_target\s*:", r"^\s*-?\s*uses\s*:\s*actions/cache@"), 50, "high", "pull_request_target workflow uses a shared dependency cache", "CRT-CI-057", "ci", "high", "Avoid shared cache writes in privileged pull_request_target workflows or isolate cache scope and untrusted code explicitly.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    ContextDetectionRule("GH_PERMISSIONS_WRITE_ALL", (r"^permissions\s*:\s*$", r"^\s*write-all\s*:\s*(?:true)?\s*$"), 4, "high", "GitHub Actions workflow grants the token all write permissions", "CRT-CI-058", "ci", "high", "Declare only the minimal required GITHUB_TOKEN permissions instead of write-all.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    ContextDetectionRule("GH_PERMISSIONS_TOP_LEVEL_OIDC", (r"^permissions\s*:\s*$", r"^\s*id-token\s*:\s*write\s*$"), 4, "medium", "GitHub Actions workflow grants OIDC token minting at workflow scope", "CRT-CI-059", "ci", "high", "Scope id-token: write to the single deployment job that requires OIDC and constrain the cloud trust policy.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    ContextDetectionRule("GH_PERMISSIONS_TOP_LEVEL_PACKAGES", (r"^permissions\s*:\s*$", r"^\s*packages\s*:\s*write\s*$"), 4, "medium", "GitHub Actions workflow grants package publication at workflow scope", "CRT-CI-060", "ci", "high", "Scope packages: write to the single publishing job and keep package/repository access narrowly constrained.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    ContextDetectionRule("GH_PERMISSIONS_TOP_LEVEL_SECURITY_EVENTS", (r"^permissions\s*:\s*$", r"^\s*security-events\s*:\s*write\s*$"), 4, "medium", "GitHub Actions workflow grants code-scanning result write access at workflow scope", "CRT-CI-061", "ci", "high", "Scope security-events: write to the scan/upload job that needs it and keep other workflow jobs read-only.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    ContextDetectionRule("GH_PERMISSIONS_TOP_LEVEL_DEPLOYMENTS", (r"^permissions\s*:\s*$", r"^\s*deployments\s*:\s*write\s*$"), 4, "medium", "GitHub Actions workflow grants deployment write access at workflow scope", "CRT-CI-062", "ci", "high", "Scope deployments: write to the deployment job and keep unrelated workflow jobs on the minimum required token permissions.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    ContextDetectionRule("GH_PERMISSIONS_TOP_LEVEL_ISSUES", (r"^permissions\s*:\s*$", r"^\s*issues\s*:\s*write\s*$"), 4, "medium", "GitHub Actions workflow grants issue write access at workflow scope", "CRT-CI-063", "ci", "high", "Scope issues: write to the issue-management job and keep unrelated workflow jobs on minimum token permissions.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    ContextDetectionRule("GH_PERMISSIONS_TOP_LEVEL_PULL_REQUESTS", (r"^permissions\s*:\s*$", r"^\s*pull-requests\s*:\s*write\s*$"), 4, "medium", "GitHub Actions workflow grants pull request write access at workflow scope", "CRT-CI-064", "ci", "high", "Scope pull-requests: write to the pull-request automation job and keep fork-triggered workflows from receiving unnecessary write access.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    ContextDetectionRule("GH_PERMISSIONS_TOP_LEVEL_REPOSITORY_PROJECTS", (r"^permissions\s*:\s*$", r"^\s*repository-projects\s*:\s*write\s*$"), 4, "medium", "GitHub Actions workflow grants repository project write access at workflow scope", "CRT-CI-065", "ci", "high", "Scope repository-projects: write to the project-management job and ensure only the intended repository projects are reachable.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    ContextDetectionRule("GH_PERMISSIONS_TOP_LEVEL_STATUSES", (r"^permissions\s*:\s*$", r"^\s*statuses\s*:\s*write\s*$"), 4, "medium", "GitHub Actions workflow grants commit status write access at workflow scope", "CRT-CI-066", "ci", "high", "Scope statuses: write to the status-publishing job and keep unrelated workflow jobs on minimum token permissions.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    ContextDetectionRule("GH_PERMISSIONS_TOP_LEVEL_WORKFLOWS", (r"^permissions\s*:\s*$", r"^\s*workflows\s*:\s*write\s*$"), 4, "medium", "GitHub Actions workflow grants workflow-file write access at workflow scope", "CRT-CI-067", "ci", "high", "Scope workflows: write to the narrowly required workflow-management job and protect workflow definitions from unnecessary mutation.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    ContextDetectionRule("GH_PERMISSIONS_TOP_LEVEL_ATTESTATIONS", (r"^permissions\s*:\s*$", r"^\s*attestations\s*:\s*write\s*$"), 4, "medium", "GitHub Actions workflow grants artifact attestation write access at workflow scope", "CRT-CI-068", "ci", "high", "Scope attestations: write to the artifact-attestation job and retain only the permissions required for provenance generation.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    ContextDetectionRule("GH_PERMISSIONS_TOP_LEVEL_DISCUSSIONS", (r"^permissions\s*:\s*$", r"^\s*discussions\s*:\s*write\s*$"), 4, "medium", "GitHub Actions workflow grants discussion write access at workflow scope", "CRT-CI-069", "ci", "high", "Scope discussions: write to the discussion-management job and keep unrelated workflow jobs on minimum token permissions.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    ContextDetectionRule("GH_PERMISSIONS_TOP_LEVEL_PAGES", (r"^permissions\s*:\s*$", r"^\s*pages\s*:\s*write\s*$"), 4, "medium", "GitHub Actions workflow grants Pages deployment write access at workflow scope", "CRT-CI-070", "ci", "high", "Scope pages: write to the Pages deployment job and keep unrelated workflow jobs on minimum token permissions.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    ContextDetectionRule("GH_PERMISSIONS_TOP_LEVEL_ACTIONS", (r"^permissions\s*:\s*$", r"^\s*actions\s*:\s*write\s*$"), 4, "medium", "GitHub Actions workflow grants Actions-management write access at workflow scope", "CRT-CI-071", "ci", "high", "Scope actions: write to the narrowly required Actions-management job and keep unrelated workflow jobs on minimum token permissions.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    ContextDetectionRule("GH_PERMISSIONS_TOP_LEVEL_CHECKS", (r"^permissions\s*:\s*$", r"^\s*checks\s*:\s*write\s*$"), 4, "medium", "GitHub Actions workflow grants check-run write access at workflow scope", "CRT-CI-074", "ci", "high", "Scope checks: write to the narrowly required check-reporting job and keep unrelated workflow jobs on minimum token permissions.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    ContextDetectionRule("GH_PERMISSIONS_TOP_LEVEL_METADATA", (r"^permissions\s*:\s*$", r"^\s*artifact-metadata\s*:\s*write\s*$"), 4, "medium", "GitHub Actions workflow grants artifact metadata write access at workflow scope", "CRT-CI-075", "ci", "high", "Scope artifact-metadata: write to the narrowly required artifact-metadata job and keep unrelated workflow jobs on minimum token permissions.", "policy", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    ContextDetectionRule("TF_PUBLIC_SSH_ADMIN", (r"cidr_blocks\s*=.*(?:0\.0\.0\.0/0|::/0)", r"from_port\s*=\s*22\s*(?:#.*)?$", r"to_port\s*=\s*22\s*(?:#.*)?$"), 15, "critical", "Terraform exposes SSH administration to the public internet", "CRT-IAC-018", "iac", "high", "Restrict SSH ingress to approved private networks or a managed access gateway.", "policy", ("*.tf", "**/*.tf")),
    ContextDetectionRule("TF_PUBLIC_RDP_ADMIN", (r"cidr_blocks\s*=.*(?:0\.0\.0\.0/0|::/0)", r"from_port\s*=\s*3389\s*(?:#.*)?$", r"to_port\s*=\s*3389\s*(?:#.*)?$"), 15, "critical", "Terraform exposes RDP administration to the public internet", "CRT-IAC-021", "iac", "high", "Restrict RDP ingress to approved private networks or a managed access gateway.", "policy", ("*.tf", "**/*.tf")),
    ContextDetectionRule("K8S_CAP_ADD_ALL", (r"^\s*capabilities\s*:", r"^\s*add\s*:", r"^\s*-\s*[\"']?ALL[\"']?\s*$"), 6, "critical", "Kubernetes container adds all Linux capabilities", "CRT-IAC-019", "iac", "high", "Drop all capabilities and add only individually reviewed requirements.", "policy", K8S_CONTEXT_GLOBS),
    ContextDetectionRule("K8S_HOSTPATH_ROOT", (r"^\s*hostPath\s*:", r"^\s*path\s*:\s*/\s*$"), 4, "critical", "Kubernetes workload mounts the host filesystem root", "CRT-IAC-020", "iac", "high", "Remove the root hostPath mount and use narrowly scoped managed storage.", "policy", K8S_CONTEXT_GLOBS),
    ContextDetectionRule("AGENT_DOWNLOAD_EXECUTE", (r"\b(?:curl|wget)\b.*(?:https?://)", r"\bchmod\s+\+x\b", r"\b(?:execute|run)\s+(?:\./|/tmp/)[^\s]+"), 8, "critical", "AI-agent instructions download and execute an unverified binary", "CRT-AI-009", "ai-agent", "high", "Require pinned checksums or signatures and human approval before executing downloaded artifacts.", "policy", AI_CONTEXT_GLOBS),
]



def validate_rule_registry(rules: list[DetectionRule]) -> None:
    valid_severities = {"low", "medium", "high", "critical"}
    valid_categories = {"secret", "ci", "iac", "ai-agent", "supply-chain"}
    valid_confidence = {"low", "medium", "high"}
    valid_kinds = {"secret", "policy"}
    seen: set[str] = set()
    for rule in rules:
        if not re.fullmatch(r"CRT-(?:SEC|CI|IAC|AI|SUP)-\d{3}", rule.rule_id):
            raise ValueError(f"Invalid rule ID: {rule.rule_id}")
        if rule.rule_id in seen:
            raise ValueError(f"Duplicate rule ID: {rule.rule_id}")
        seen.add(rule.rule_id)
        if rule.severity not in valid_severities or rule.category not in valid_categories:
            raise ValueError(f"Invalid metadata for {rule.rule_id}")
        if rule.confidence not in valid_confidence or rule.kind not in valid_kinds or not rule.remediation.strip():
            raise ValueError(f"Invalid metadata for {rule.rule_id}")
        try:
            re.compile(rule.regex, rule.flags)
        except re.error as exc:
            raise ValueError(f"Invalid regex for {rule.rule_id}: {exc}") from exc


def _rule_path_matches(path: str, globs: tuple[str, ...]) -> bool:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.lower()
    return not globs or any(fnmatch.fnmatchcase(normalized, pattern.lower()) for pattern in globs)


def match_rules(line: str, filepath: str, rules: list[DetectionRule] | None = None) -> list[tuple[DetectionRule, re.Match]]:
    active = DEFAULT_DETECTION_RULES if rules is None else rules
    results = []
    for rule in active:
        if _rule_path_matches(filepath, rule.file_globs):
            match = rule.compiled.search(line)
            if match and _match_is_valid(rule, match):
                results.append((rule, match))
    provider_specific = any(rule.rule_id in {f"CRT-SEC-{index:03d}" for index in range(21, 135)} for rule, _ in results)
    if provider_specific:
        generic_names = {"PASSWORD_LITERAL", "API_KEY_LITERAL", "TOKEN_LITERAL", "SECRET_LITERAL", "DATABASE_URL", "PRIVATE_KEY", "JWT_TOKEN", "BASE64_SECRET", "GENERIC_CREDENTIAL"}
        results = [(rule, match) for rule, match in results if rule.name not in generic_names]
    return results


def _provider_specific(rule: DetectionRule) -> bool:
    return rule.kind == "secret" and not _generic_rule(rule)


def _generic_rule(rule: DetectionRule) -> bool:
    return rule.name in {"PASSWORD_LITERAL", "API_KEY_LITERAL", "TOKEN_LITERAL", "SECRET_LITERAL", "DATABASE_URL", "PRIVATE_KEY", "JWT_TOKEN", "BASE64_SECRET", "GENERIC_CREDENTIAL"}


def match_rules_all(line: str, filepath: str, rules: list[DetectionRule] | None = None, registry=None) -> list[tuple[DetectionRule, re.Match]]:
    """Return all non-overlapping, span-aware matches for the scanner evaluator."""
    if registry is not None:
        active_items = registry.plan(filepath, line).candidates
        candidates: list[tuple[int, DetectionRule, re.Match, int]] = []
        for item in active_items:
            for match in item.compiled.finditer(line):
                if _match_is_valid(item.rule, match):
                    candidates.append((item.order, item.rule, match, match.end() - match.start()))
    else:
        active = DEFAULT_DETECTION_RULES if rules is None else rules
        candidates = []
        for order, rule in enumerate(active):
            if not _rule_path_matches(filepath, rule.file_globs):
                continue
            for match in rule.compiled.finditer(line):
                if _match_is_valid(rule, match):
                    candidates.append((order, rule, match, match.end() - match.start()))

    providers = [(rule, match) for _order, rule, match, _length in candidates if _provider_specific(rule)]
    candidates = [
        item for item in candidates
        if not (_generic_rule(item[1]) and any(item[2].start() < provider_match.end() and provider_match.start() < item[2].end() for _provider, provider_match in providers))
    ]
    accepted: list[tuple[int, DetectionRule, re.Match, int]] = []
    for item in sorted(candidates, key=lambda value: (value[2].start(), -(value[3]), value[0])):
        if any(item[2].start() < other[2].end() and other[2].start() < item[2].end() for other in accepted):
            continue
        accepted.append(item)
    return [(order_rule[1], order_rule[2]) for order_rule in sorted(accepted, key=lambda value: (value[2].start(), value[0]))]

def validate_context_rule_registry(rules: list[ContextDetectionRule]) -> None:
    """Validate bounded context rules and their export metadata."""
    valid_severities = {"low", "medium", "high", "critical"}
    valid_categories = {"ci", "iac", "ai-agent", "supply-chain"}
    seen: set[str] = set()
    for rule in rules:
        if not re.fullmatch(r"CRT-(?:CI|IAC|AI|SUP)-\d{3}", rule.rule_id) or rule.rule_id in seen:
            raise ValueError(f"Invalid or duplicate context rule ID: {rule.rule_id}")
        seen.add(rule.rule_id)
        if rule.severity not in valid_severities or rule.category not in valid_categories or rule.confidence not in {"low", "medium", "high"}:
            raise ValueError(f"Invalid metadata for {rule.rule_id}")
        if rule.kind != "policy" or rule.max_line_span < 1 or not rule.required_regexes or not rule.remediation.strip():
            raise ValueError(f"Invalid context rule for {rule.rule_id}")
        for regex in rule.required_regexes:
            try:
                re.compile(regex, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"Invalid regex for {rule.rule_id}: {exc}") from exc


def match_context_rules(lines: list[tuple[int, str]], filepath: str, rules: list[ContextDetectionRule] | None = None) -> list[ContextMatch]:
    """Match ordered context components inside bounded real-line-number windows."""
    active = DEFAULT_CONTEXT_RULES if rules is None else rules
    ordered = sorted(lines, key=lambda item: item[0])
    results: list[ContextMatch] = []
    for rule in active:
        if not _rule_path_matches(filepath, rule.file_globs):
            continue
        compiled = [re.compile(regex, re.IGNORECASE) for regex in rule.required_regexes]
        seen_component_sequences: set[tuple[int, ...]] = set()
        for start_index, (start_number, start_content) in enumerate(ordered):
            anchor = compiled[0].search(start_content)
            if not anchor:
                continue
            cursor = start_index + 1
            matched_all = True
            component_indices: list[int] = []
            for regex in compiled[1:]:
                found_index = None
                for index in range(cursor, len(ordered)):
                    number, content = ordered[index]
                    if number - start_number > rule.max_line_span:
                        break
                    if regex.search(content):
                        found_index = index
                        break
                if found_index is None:
                    matched_all = False
                    break
                component_indices.append(found_index)
                cursor = found_index + 1
            sequence = tuple(component_indices)
            if matched_all and sequence not in seen_component_sequences:
                seen_component_sequences.add(sequence)
                results.append(ContextMatch(
                    rule, start_number, anchor.group(0), start_content,
                    (start_number,) + tuple(ordered[index][0] for index in component_indices),
                ))
    return results


@dataclass
class ConfigCategory:
    """A config change detection category."""
    name: str
    file_patterns: list[str]
    severity: str
    description: str


DEFAULT_CONFIG_CATEGORIES: list[ConfigCategory] = [
    ConfigCategory(
        name="ENV_CONFIG",
        file_patterns=[".env", ".env.local", ".env.production", ".env.staging", ".env.test"],
        severity="high",
        description="Environment config file modified",
    ),
    ConfigCategory(
        name="AUTH_CONFIG",
        file_patterns=["config.json", "config.yaml", "config.yml", "settings.py",
                       "application.properties", "application.yml"],
        severity="medium",
        description="Application config file modified",
    ),
    ConfigCategory(
        name="CI_CONFIG",
        file_patterns=[".github/workflows/", "Jenkinsfile", ".gitlab-ci.yml",
                       "circle.yml", ".circleci/"],
        severity="high",
        description="CI/CD configuration modified",
    ),
    ConfigCategory(
        name="INFRA_CONFIG",
        file_patterns=["docker-compose.yml", "docker-compose.yaml", "Dockerfile",
                       "terraform.tf", "kubernetes.yaml", "k8s.yaml"],
        severity="medium",
        description="Infrastructure config modified",
    ),
    ConfigCategory(
        name="SECURITY_CONFIG",
        file_patterns=[],
        severity="high",
        description="Security-related file modified (detected by path keywords)",
    ),
]

# Keywords that indicate security-sensitive paths
SECURITY_PATH_KEYWORDS = ["auth", "password", "session", "oauth", "token", "security",
                          "certificate", "ssl", "tls", "encrypt", "credential"]


def get_secret_patterns() -> list[SecretPattern]:
    """Return the default secret detection patterns."""
    return list(DEFAULT_SECRET_PATTERNS)


def get_config_categories() -> list[ConfigCategory]:
    """Return the default config change detection categories."""
    return list(DEFAULT_CONFIG_CATEGORIES)


def match_secret(line: str, patterns: list[SecretPattern] | None = None) -> list[tuple[SecretPattern, re.Match]]:
    """Match a line against secret patterns. Returns list of (pattern, match) tuples."""
    if patterns is None:
        patterns = DEFAULT_SECRET_PATTERNS
    results = []
    for pattern in patterns:
        m = pattern.compiled.search(line)
        if m and _match_is_valid(pattern, m):
            results.append((pattern, m))
    return results


def classify_config_file(filepath: str, categories: list[ConfigCategory] | None = None) -> list[ConfigCategory]:
    """Classify a file path into config change categories."""
    if categories is None:
        categories = DEFAULT_CONFIG_CATEGORIES
    results = []
    filepath_lower = filepath.lower()

    for cat in categories:
        if cat.name == "SECURITY_CONFIG":
            # Special: check for security keywords in path
            if any(kw in filepath_lower for kw in SECURITY_PATH_KEYWORDS):
                if cat not in results:
                    results.append(cat)
            continue

        for pattern in cat.file_patterns:
            if pattern.endswith("/"):
                # Directory prefix match
                if filepath_lower.startswith(pattern) or "/" + pattern in "/" + filepath_lower:
                    if cat not in results:
                        results.append(cat)
                    break
            else:
                # Filename match
                if filepath_lower.endswith(pattern) or filepath_lower == pattern:
                    if cat not in results:
                        results.append(cat)
                    break

    return results