"""联合登录声明投影模块。

输入：
1. 已验证的身份属性（verified identity attributes）
2. 受众（relying party）允许的用途集合
3. 用户同意记录（带版本的完整快照）

输出：经用途授权与最小化处理后允许签发的声明投影。
"""

from .models import (
    Attribute,
    ClaimRequest,
    ConsentRecord,
    ProjectionResult,
)
from .projector import (
    ClaimProjectionError,
    build_projection,
    project_claims,
)
from .registry import (
    DEFAULT_ATTRIBUTE_PURPOSES,
    DEFAULT_CLAIM_REGISTRY,
)

__all__ = [
    "Attribute",
    "ClaimRequest",
    "ConsentRecord",
    "ProjectionResult",
    "ClaimProjectionError",
    "build_projection",
    "project_claims",
    "DEFAULT_ATTRIBUTE_PURPOSES",
    "DEFAULT_CLAIM_REGISTRY",
]
