"""IIIF Presentation 3 数字标本清单采集器。

采集器只通过公开接口（HTTP/HTTPS）或本地文件读取 IIIF 清单，
不读取样例生成器的内部结构或测试真值。
"""
from .collector import collect

__all__ = ["collect"]
__version__ = "1.0.0"
