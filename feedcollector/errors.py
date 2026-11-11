"""采集器自定义异常。"""


class FeedCollectorError(Exception):
    """所有采集器异常的基类。"""


class FetchError(FeedCollectorError):
    """抓取订阅源或附件失败（网络、超时、HTTP 状态、文件不存在）。"""


class FeedParseError(FeedCollectorError):
    """订阅源文档不是受支持的协议子集或 XML 不合法。"""
