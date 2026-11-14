"""公开错误码登记配置。

登记配置决定哪些内部错误码可以向客户端暴露固定文案；
未登记的错误一律使用通用文案，避免泄露内部细节。
"""

import json


class ErrorRegistry:
    """公开错误码登记表。

    mappings 结构::

        {
            "<内部错误码>": {
                "client_code": "<对外错误码>",
                "client_message": "<固定对外文案>"
            },
            ...
        }
    """

    def __init__(self, mappings=None, generic_message="系统繁忙，请稍后重试",
                 unknown_code="INTERNAL_ERROR"):
        self._mappings = dict(mappings or {})
        self.generic_message = generic_message
        self.unknown_code = unknown_code

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError("registry config must be a JSON object")
        mappings = data.get("mappings", {})
        if not isinstance(mappings, dict):
            raise ValueError("registry 'mappings' must be an object")
        for code, entry in mappings.items():
            if not isinstance(entry, dict) or "client_message" not in entry:
                raise ValueError(
                    "registry mapping for %r must contain 'client_message'" % code)
        return cls(
            mappings=mappings,
            generic_message=data.get("generic_message", "系统繁忙，请稍后重试"),
            unknown_code=data.get("unknown_code", "INTERNAL_ERROR"),
        )

    @classmethod
    def load(cls, path):
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    def lookup(self, code):
        """返回已登记错误码的对外配置，未登记返回 None。"""
        if code is None:
            return None
        return self._mappings.get(str(code))
