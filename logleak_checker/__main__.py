"""命令行入口：python -m logleak_checker <源码文件...> [--config config.json]

退出码：发现敏感数据流入日志（sensitive_in_log）时为 1，否则为 0；
待人工确认项仅打印，不影响退出码。
"""

from __future__ import annotations

import argparse
import sys

from .analyzer import FindingKind, analyze_file
from .config import AnalyzerConfig, default_config


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="logleak_checker",
        description="检查源码中完整邮箱/手机号是否未经脱敏流入日志",
    )
    parser.add_argument("paths", nargs="+", help="待检查的 .py 文件")
    parser.add_argument("--config", help="JSON 配置文件路径", default=None)
    args = parser.parse_args(argv)

    config = AnalyzerConfig.from_json(args.config) if args.config else default_config()

    violations = 0
    for path in args.paths:
        findings = analyze_file(path, config)
        if not findings:
            print(f"{path}: 未发现问题")
            continue
        for finding in findings:
            print(f"{path}:{finding.line}:{finding.col}: {finding}")
        violations += sum(1 for f in findings if f.kind is FindingKind.SENSITIVE_IN_LOG)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
