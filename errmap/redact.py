"""内部诊断输出的字段脱敏与有界序列化。

规则：
1. 字段名命中默认秘密模式（password/token/secret/key/authorization 等，
   大小写不敏感）或出现在错误对象 ``secret_fields`` 列表中的字段，
   其值一律替换为 ``***REDACTED***``。
2. 循环引用替换为 ``{"__truncated__": "circular-reference", ...}``。
3. 嵌套深度超过 ``MAX_DEPTH`` 的部分替换为
   ``{"__truncated__": "max-depth-exceeded", ...}``。
4. 输出保证可被 ``json.dumps`` 序列化；不可序列化标量转为有界 repr。
"""

REDACTED = "***REDACTED***"
MAX_DEPTH = 8
MAX_REPR_LEN = 200

DEFAULT_SECRET_PATTERNS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "private_key",
    "session_id",
)

# 元数据字段：只记录被标记的字段名，本身不含秘密值，不按模式脱敏
SECRET_FIELDS_KEY = "secret_fields"


def _is_secret_key(key, explicit_keys):
    if not isinstance(key, str):
        return False
    if key == SECRET_FIELDS_KEY:
        return False
    if key in explicit_keys:
        return True
    lowered = key.lower()
    return any(pattern in lowered for pattern in DEFAULT_SECRET_PATTERNS)


def collect_secret_paths(error_obj, max_depth=MAX_DEPTH * 4):
    """从原始错误对象收集被显式标记为秘密的字段。

    返回 ``(explicit_keys, explicit_paths)``：
    - ``explicit_keys``：裸字段名集合（任意层级命中即脱敏）；
    - ``explicit_paths``：从错误对象根部出发的点分路径集合。

    每个节点可携带 ``secret_fields`` 列表，条目可以是裸字段名
    （如 ``"DB_PASSWORD"``）或相对该节点的点分路径
    （如 ``"env.DB_PASSWORD"``）。遍历有深度上限且对循环引用安全。
    """
    explicit_keys = set()
    explicit_paths = set()
    seen = set()

    def walk(node, path, depth):
        if depth > max_depth or not isinstance(node, (dict, list)):
            return
        oid = id(node)
        if oid in seen:
            return
        seen.add(oid)
        if isinstance(node, dict):
            declared = node.get("secret_fields")
            if isinstance(declared, (list, tuple)):
                for item in declared:
                    if not isinstance(item, str):
                        continue
                    explicit_keys.add(item)
                    joined = "%s.%s" % (path, item) if path else item
                    explicit_paths.add(joined)
                    # 相对路径的最后一段也按裸字段名处理，保证跨层级命中
                    explicit_keys.add(item.rsplit(".", 1)[-1])
            for key, value in node.items():
                child = "%s.%s" % (path, key) if path else str(key)
                walk(value, child, depth + 1)
        else:
            for index, value in enumerate(node):
                walk(value, "%s[%d]" % (path, index), depth + 1)

    walk(error_obj, "", 0)
    return explicit_keys, explicit_paths


def sanitize(obj, explicit=None, max_depth=MAX_DEPTH):
    """返回脱敏且有界的可 JSON 序列化副本，不修改原对象。

    ``explicit`` 为 ``collect_secret_paths`` 的返回值；缺省时现场收集。
    """
    if explicit is None:
        explicit = collect_secret_paths(obj)
    explicit_keys, explicit_paths = explicit
    seen = set()

    def walk(node, path, depth):
        if isinstance(node, (dict, list)):
            oid = id(node)
            if oid in seen:
                return {"__truncated__": "circular-reference",
                        "path": path or "$"}
            if depth >= max_depth:
                return {"__truncated__": "max-depth-exceeded",
                        "depth": depth}
            seen.add(oid)
            try:
                if isinstance(node, dict):
                    out = {}
                    for key, value in node.items():
                        child = "%s.%s" % (path, key) if path else str(key)
                        if _is_secret_key(key, explicit_keys) \
                                or child in explicit_paths:
                            out[key] = REDACTED
                        else:
                            out[key] = walk(value, child, depth + 1)
                    return out
                return [walk(value, "%s[%d]" % (path, index), depth + 1)
                        for index, value in enumerate(node)]
            finally:
                seen.discard(oid)
        if isinstance(node, (str, int, float, bool)) or node is None:
            return node
        text = repr(node)
        if len(text) > MAX_REPR_LEN:
            text = text[:MAX_REPR_LEN] + "..."
        return text

    return walk(obj, "", 0)
