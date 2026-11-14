"""错误响应映射层。

输入：结构化错误对象、接口名、请求标识、公开错误码登记配置。
输出：客户端视图（短文案 + 链路编号）与内部诊断视图（脱敏后的完整
错误树），两个视图的链路编号保持一致。
"""

from .redact import sanitize, collect_secret_paths


def map_error(error_obj, interface, request_id, registry):
    """把结构化错误对象映射为客户端/内部两类输出。

    返回::

        {
            "client": {
                "ok": False,
                "error": {"code": ..., "message": ..., "trace_id": ...},
            },
            "internal": {
                "trace_id": ...,
                "interface": ...,
                "error": <脱敏后的错误树>,
            },
        }
    """
    code = error_obj.get("code") if isinstance(error_obj, dict) else None
    entry = registry.lookup(code)
    if entry is not None:
        public_code = entry.get("client_code", str(code))
        public_message = entry["client_message"]
    else:
        public_code = registry.unknown_code
        public_message = registry.generic_message

    client = {
        "ok": False,
        "error": {
            "code": public_code,
            "message": public_message,
            "trace_id": request_id,
        },
    }

    explicit = collect_secret_paths(error_obj)
    internal = {
        "trace_id": request_id,
        "interface": interface,
        "error": sanitize(error_obj, explicit),
    }
    return {"client": client, "internal": internal}
