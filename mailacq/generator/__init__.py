"""样例生成器包。

职责边界：本包只负责把虚构场景写成 generated/<scenario>/ 下的邮件文件、
生成器清单和独立真值 ground_truth.json。采集器禁止读取本目录中的
ground_truth.json 与 manifest.json（见 tests/test_collector_isolation.py）。
"""
