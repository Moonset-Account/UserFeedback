"""采集器包：只通过公开 HTTP 接口 / 浏览器交互获取数据。

本包刻意不依赖本地源站实现或独立真值数据，该隔离约束由测试套件强制检查
（见 tests/test_isolation.py）。
"""

__all__ = ["reservoir", "scanner", "parsers", "browser", "runner"]
