"""命令行入口。

用法::

    python -m mail_security --config config.json --message message.json
    python -m mail_security --payload payload.json   # 含 {"config": ..., "message": ...}

输出裁决结果 JSON（缩进 2）。输入文件无法解析等错误以非零退出码退出，
裁决本身（含拒收/隔离）退出码为 0，便于流水线用 JSON 字段判断处置。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Mapping

from mail_security.engine import evaluate
from mail_security.models import GatewayConfig, Message


def _load_json(path: str) -> Mapping[str, object]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, Mapping):
        raise ValueError(f"{path} 顶层必须是 JSON 对象")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="邮件安全网关投递决策模块")
    parser.add_argument("--config", help="网关配置 JSON 文件路径")
    parser.add_argument("--message", help="待裁决邮件 JSON 文件路径")
    parser.add_argument("--payload", help="合并载荷 JSON（含 config 与 message 两个键）")
    args = parser.parse_args(argv)

    try:
        if args.payload:
            payload = _load_json(args.payload)
            if "config" not in payload or "message" not in payload:
                raise ValueError("payload 必须包含 config 与 message 两个键")
            config = GatewayConfig.from_dict(payload["config"])
            message = Message.from_dict(payload["message"])
        elif args.config and args.message:
            config = GatewayConfig.from_dict(_load_json(args.config))
            message = Message.from_dict(_load_json(args.message))
        else:
            parser.error("必须提供 --payload，或同时提供 --config 与 --message")
            return 2  # pragma: no cover - argparse 会自行退出

        decision = evaluate(message, config)
    except (ValueError, json.JSONDecodeError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    print(json.dumps(decision.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
