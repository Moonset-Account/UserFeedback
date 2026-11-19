"""联合登录断言的条件核验与一次性消费。

公共入口：
    - :class:`federated_login.service.AssertionConsumerService`
    - :meth:`federated_login.service.AssertionConsumerService.consume`

本包只处理“验签之后”的条件核验与消费登记，签名/XML 解析不在范围内。
"""

from .errors import (
    AssertionAlreadyConsumedError,
    AudienceRestrictionError,
    FederationError,
    SubjectConfirmationError,
)
from .models import (
    Assertion,
    AudienceRestriction,
    ConsumptionRecord,
    LoginRequest,
    LoginResult,
    MatchedConfirmation,
    SubjectConfirmation,
)
from .service import AssertionConsumerService

__all__ = [
    "Assertion",
    "AssertionAlreadyConsumedError",
    "AssertionConsumerService",
    "AudienceRestriction",
    "AudienceRestrictionError",
    "ConsumptionRecord",
    "FederationError",
    "LoginRequest",
    "LoginResult",
    "MatchedConfirmation",
    "SubjectConfirmation",
    "SubjectConfirmationError",
]
