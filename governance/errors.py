"""治理裁决模块的错误类型。"""


class GovernanceInputError(ValueError):
    """裁决输入不满足结构或业务约束时抛出。

    继承自 ``ValueError``，调用方既可以按本类型捕获，也可以按
    ``ValueError`` 捕获。错误信息中携带出错的实体标识，便于定位。
    """
