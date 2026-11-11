"""虚构样例生成器与本地源站。

本包与 feedcollector 完全分离：
- feedcollector 任何模块都不得 import samplegen（架构守卫测试强制保证）；
- 采集器只能通过 HTTP 公开接口或文件路径访问这里生成的样例；
- scenarios 中的 expected_* 真值仅供 tests 使用，采集器运行时不读取。
"""
