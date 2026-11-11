"""本地虚构源站：星港资料馆（全部数据为虚构，不对应任何真实机构）。

职责边界：
- 本包负责生成样例页面、提供 HTTP 路由，并写出独立真值（供测试专用）；
- collector 与 classifier 禁止 import 本包（由 tests/test_architecture.py 强制），
  判定只能基于公开 HTTP / 文件 / 浏览器交互得到的 HTML。
"""
