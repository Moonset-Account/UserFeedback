"""路由隔离审查模块的异常类型。"""

from __future__ import annotations


class RouteAuditError(Exception):
    """本模块所有异常的基类。"""


class InvalidPrefixError(RouteAuditError):
    """前缀字符串无法解析为合法 IP 前缀。"""


class UnknownDomainError(RouteAuditError):
    """引用了未在路由域清单中登记的租户/路由域。"""


class DuplicateRouteError(RouteAuditError):
    """同一域内插入了重复 route_id 的路由。"""


class RouteNotFoundError(RouteAuditError):
    """撤下路由时在指定域内找不到对应 route_id。"""


class ScenarioFormatError(RouteAuditError):
    """JSON / dict 场景数据结构不合法。"""
