"""采集器异常类型。"""

from __future__ import annotations


class CollectorError(Exception):
    """采集器所有异常的基类。"""


class SpecError(CollectorError):
    """OpenAPI 文档本身不符合本构造器支持的限定子集。"""


class ValidationError(CollectorError):
    """输入值与文档声明的 schema 不匹配（类型错误、缺必填等）。"""


class UnsupportedCombinationError(CollectorError):
    """文档/输入组合落在声明支持的协议子集之外。

    构造器必须在发出任何 HTTP 请求之前抛出该异常，禁止静默换成
    其它序列化形式（例如逗号拼接）。
    """

    def __init__(self, reason: str, *, location: str | None = None,
                 parameter: str | None = None) -> None:
        self.reason = reason
        self.location = location
        self.parameter = parameter
        prefix = ""
        if location and parameter:
            prefix = f"[{location}:{parameter}] "
        elif parameter:
            prefix = f"[{parameter}] "
        super().__init__(prefix + reason)
