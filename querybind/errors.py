"""绑定阶段错误类型。"""


class BinderError(Exception):
    """所有绑定错误的基类。"""


class UnresolvedColumnError(BinderError):
    def __init__(self, message, qualifier=None, column=None, location=None):
        super().__init__(message)
        self.qualifier = qualifier
        self.column = column
        self.location = location


class AmbiguousColumnError(BinderError):
    """同层存在多个可见候选时抛出，candidates 给出全部候选及其位置。"""

    def __init__(self, message, candidates, location=None):
        super().__init__(message)
        # candidates: list[dict(qualifier, column, depth, sibling, position)]
        self.candidates = candidates
        self.location = location


class IllegalLateralError(BinderError):
    """非横向派生表引用了左邻表（只能引用自身与外层查询块）。"""

    def __init__(self, message, qualifier=None, column=None, location=None):
        super().__init__(message)
        self.qualifier = qualifier
        self.column = column
        self.location = location


class DuplicateAliasError(BinderError):
    def __init__(self, message, alias=None, location=None):
        super().__init__(message)
        self.alias = alias
        self.location = location
