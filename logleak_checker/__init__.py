"""logleak_checker —— 账号服务源码评审用日志敏感信息泄露检查组件。

基于 AST 的模块内静态污点分析：
- 配置标记敏感参数、日志函数、认可的掩码函数；
- 跟踪函数调用、局部变量、分支汇合与格式化字符串；
- 分支一侧脱敏、另一侧保留原值，汇合后仍给出从参数到日志行的证据链；
- 分析范围外的调用独立标为待人工确认；
- 只传入掩码值的日志调用判定为通过。
"""

from .config import AnalyzerConfig, default_config
from .analyzer import Finding, FindingKind, analyze_file, analyze_source

__all__ = [
    "AnalyzerConfig",
    "default_config",
    "Finding",
    "FindingKind",
    "analyze_file",
    "analyze_source",
]
