"""组件自身的异常类型。"""


class PreflightConfigError(ValueError):
    """预检配置不合法（结构、引用、动作、地域声明等问题）。"""


class PreflightError(RuntimeError):
    """执行预检过程中发生的运行时错误。"""
