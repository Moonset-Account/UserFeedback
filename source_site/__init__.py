"""本地源站包：虚构标本数据生成 + 目录页服务。

与 collector（采集器）完全分离：
* collector 只允许通过 HTTP / 浏览器访问本包提供的公开页面；
* 本包生成的"独立真值"（data/golden/*.json）仅供测试核对，
  collector 不导入、不读取本包任何模块（tests/test_isolation.py 强制）。
所有数据均为虚构，不对应任何真实物种、地点或个人。
"""

__all__ = ["generator", "server"]
