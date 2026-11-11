"""采集请求构造器（collector）。

仅依据项目内限定版 OpenAPI 文档构造【只读】请求；不支持的参数组合在
发请求之前抛出 :class:`UnsupportedCombinationError`，绝不静默降级。
"""

from .exceptions import (
    CollectorError,
    SpecError,
    UnsupportedCombinationError,
    ValidationError,
)
from .loader import OpenAPISpec, load_spec
from .builder import BuiltRequest, RequestBuilder
from .client import CollectorClient, RunResult

__all__ = [
    "CollectorError",
    "SpecError",
    "UnsupportedCombinationError",
    "ValidationError",
    "OpenAPISpec",
    "load_spec",
    "BuiltRequest",
    "RequestBuilder",
    "CollectorClient",
    "RunResult",
]
