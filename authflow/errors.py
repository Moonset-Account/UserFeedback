"""分析相关的异常类型。"""


class ProgramStructureError(ValueError):
    """输入程序（CFG / 节点 / 边）结构不合法，无法进行分析时抛出。"""
