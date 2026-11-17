"""Pod 服务账号令牌可读性分析库。

公共 API：
    analyze(manifest)            -> dict  完整分析结果（JSON 可序列化）
    analyze_file(path)           -> dict  从 JSON 文件读取并分析
    resolve_automount(pod, sa)   -> tuple 自动挂载决策与依据
    AnalysisError                       输入/操作不合法时抛出
"""

from .analyzer import AnalysisError, analyze, analyze_file, resolve_automount

__all__ = ["AnalysisError", "analyze", "analyze_file", "resolve_automount"]
