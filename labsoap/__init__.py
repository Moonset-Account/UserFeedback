"""实验仪器 SOAP/MTOM 响应采集器（采集侧核心逻辑，不依赖样例生成器）。"""
from .collector import collect
from .errors import SUPPORTED_SUBSET, CollectorError, ERROR_MESSAGES_CN

__all__ = ["collect", "CollectorError", "SUPPORTED_SUBSET", "ERROR_MESSAGES_CN"]
