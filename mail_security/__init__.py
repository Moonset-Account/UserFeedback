"""邮件安全网关投递决策模块。

对外公开的数据模型与决策入口：

- :class:`~mail_security.models.Message` / :class:`~mail_security.models.GatewayConfig`
- :func:`~mail_security.engine.evaluate`
"""

from mail_security.engine import evaluate
from mail_security.models import (
    AlignmentMode,
    AuthEvidence,
    AuthMethod,
    AuthResult,
    Decision,
    Disposition,
    DomainPolicy,
    EvidenceSource,
    ForwardHop,
    GatewayConfig,
    Message,
    PolicyAction,
    domains_align,
    organizational_domain,
)

__all__ = [
    "evaluate",
    "AlignmentMode",
    "AuthEvidence",
    "AuthMethod",
    "AuthResult",
    "Decision",
    "Disposition",
    "DomainPolicy",
    "EvidenceSource",
    "ForwardHop",
    "GatewayConfig",
    "Message",
    "PolicyAction",
    "domains_align",
    "organizational_domain",
]

__version__ = "1.0.0"
