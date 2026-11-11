"""OpenAPI 3.0.3 参数序列化（限定子集）。

实现规范表（见 openapi/experiments.openapi.yaml 顶部声明）：

================  ===========  ==========================================
位置              style        支持的 explode
================  ===========  ==========================================
query             form         True / False（False 仅支持标量数组）
query             spaceDelimited / pipeDelimited  False（标量数组）
query             deepObject   固定 False（一层标量对象）
path              simple       True / False
path              label        True / False
path              matrix       True / False
================  ===========  ==========================================

编码规则：
* 默认按 RFC 3986 百分号编码，仅 unreserved 字符（A-Za-z0-9-_.~）原样保留；
* query 参数且 ``allowReserved=true`` 时，值中的保留字符
  ``, / ? : @ ! $ & ' ( ) * + ;`` 不编码，但结构分隔符 ``= % #`` 始终编码
  （否则会破坏 query 结构）；
* 任何落在子集之外的组合抛 :class:`UnsupportedCombinationError`，
  绝不静默改用逗号拼接等其它形式。
"""

from __future__ import annotations

from urllib.parse import quote_from_bytes

from .exceptions import UnsupportedCombinationError, ValidationError

# RFC 3986 unreserved 字符（永远不编码）
_UNRESERVED = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.~"
)
# allowReserved=true 时值中允许原样出现的保留字符（不含结构分隔符 = % #）
_RESERVED_VALUE_SAFE = set(",/?:@!$&'()*+;")

_QUERY_STYLES = {"form", "spaceDelimited", "pipeDelimited", "deepObject"}
_PATH_STYLES = {"simple", "label", "matrix"}
_SCALAR_TYPES = {"string", "integer", "boolean"}


def _encode(value: str, *, allow_reserved: bool) -> str:
    """按 RFC 3986 对字符串做百分号编码。

    ``allow_reserved`` 仅对 query 的值生效：保留字符（除结构分隔符外）原样。
    """
    safe = set(_UNRESERVED)
    if allow_reserved:
        safe |= _RESERVED_VALUE_SAFE
    out: list[str] = []
    for ch in value.encode("utf-8"):
        c = chr(ch)
        if c in safe:
            out.append(c)
        else:
            out.append("%{:02X}".format(ch))
    return "".join(out)


def _scalar_to_text(value: object, json_type: str) -> str:
    """把标量值转为文本形式，并做严格类型校验（不做隐式转换）。"""
    if json_type == "string":
        if not isinstance(value, str):
            raise ValidationError(f"期望 string，实际得到 {type(value).__name__}: {value!r}")
        return value
    if json_type == "integer":
        # bool 是 int 的子类，显式排除
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"期望 integer，实际得到 {type(value).__name__}: {value!r}")
        return str(value)
    if json_type == "boolean":
        if not isinstance(value, bool):
            raise ValidationError(f"期望 boolean，实际得到 {type(value).__name__}: {value!r}")
        return "true" if value else "false"
    raise ValidationError(f"不支持的标量类型: {json_type}")


def _check_array(schema: dict) -> dict:
    items = schema.get("items")
    if not isinstance(items, dict) or items.get("type") not in _SCALAR_TYPES:
        raise UnsupportedCombinationError(
            "仅支持元素为标量（string/integer/boolean）的数组",
        )
    return items


def _check_object(schema: dict) -> dict:
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        raise UnsupportedCombinationError("仅支持显式声明一层标量属性的对象")
    for prop_name, prop_schema in props.items():
        if not isinstance(prop_schema, dict) or prop_schema.get("type") not in _SCALAR_TYPES:
            raise UnsupportedCombinationError(
                f"对象属性 {prop_name!r} 必须是标量；嵌套对象/数组不在支持子集内",
            )
    return props


def _flatten_object(value: dict, props: dict, enc) -> list[str]:
    """按 [key, value, key, value...] 顺序输出编码后的扁平字符串列表。"""
    flat: list[str] = []
    for key, val in value.items():
        flat.append(enc(key))
        flat.append(enc(_scalar_to_text(val, props[key]["type"])))
    return flat


def _object_kv_list(value: dict, props: dict, enc) -> list[tuple[str, str]]:
    return [
        (enc(k), enc(_scalar_to_text(v, props[k]["type"])))
        for k, v in value.items()
    ]


def serialize_query(name: str, value: object, parameter: dict) -> tuple[str | None, list[str]]:
    """序列化一个 query 参数。

    返回 ``(query_string_fragment, omitted)``：空数组/空对象按 OpenAPI
    约定省略（返回 None 并在 omitted 中记录参数名），其余返回可直接拼到
    URL 上的原始 query 片段（已完成必要的百分号编码）。
    """
    style = parameter.get("style", "form")
    explode = parameter.get("explode", style == "form")
    allow_reserved = bool(parameter.get("allowReserved", False))
    schema = parameter.get("schema", {})
    json_type = schema.get("type")
    enc = lambda v: _encode(v, allow_reserved=allow_reserved)  # noqa: E731
    ename = _encode(name, allow_reserved=False)

    if style not in _QUERY_STYLES:
        raise UnsupportedCombinationError(
            f"query 参数不支持 style={style!r}（支持子集：{sorted(_QUERY_STYLES)}）",
            location="query", parameter=name,
        )

    # 标量
    if json_type in _SCALAR_TYPES:
        if style != "form":
            raise UnsupportedCombinationError(
                f"标量 query 参数仅支持 style=form，收到 {style!r}",
                location="query", parameter=name,
            )
        text = _scalar_to_text(value, json_type)
        return f"{ename}={enc(text)}", []

    # 数组
    if json_type == "array":
        if not isinstance(value, list):
            raise ValidationError(f"期望 array，实际得到 {type(value).__name__}: {value!r}")
        if len(value) == 0:
            return None, [name]
        item_schema = _check_array(schema)
        texts = [enc(_scalar_to_text(v, item_schema["type"])) for v in value]

        if style == "form":
            if explode:
                # 重复键：tags=a&tags=b
                return "&".join(f"{ename}={t}" for t in texts), []
            # 逗号拼接；值内的逗号已被编码为 %2C，不会与分隔符混淆
            return f"{ename}=" + ",".join(texts), []
        if style == "spaceDelimited":
            # 空格分隔符本身写作 %20（编码后的空格），值内空格同样为 %20
            return f"{ename}=" + "%20".join(texts), []
        if style == "pipeDelimited":
            return f"{ename}=" + "|".join(texts), []
        # deepObject 不接受数组
        raise UnsupportedCombinationError(
            "deepObject 仅可用于一层对象，不能用于数组",
            location="query", parameter=name,
        )

    # 对象
    if json_type == "object":
        if not isinstance(value, dict):
            raise ValidationError(f"期望 object，实际得到 {type(value).__name__}: {value!r}")
        if len(value) == 0:
            return None, [name]
        props = _check_object(schema)
        unknown = sorted(set(value) - set(props))
        if unknown:
            raise ValidationError(f"对象包含 schema 未声明的属性: {unknown}")
        for key, val in value.items():
            _scalar_to_text(val, props[key]["type"])  # 触发类型校验
        if style == "deepObject":
            # filter[status]=running&filter[min_score]=80
            # 方括号属于结构字符，按 RFC 3986 编码为 %5B / %5D
            frags = []
            for key, val in value.items():
                text = _scalar_to_text(val, props[key]["type"])
                frags.append(
                    f"{ename}%5B{_encode(key, allow_reserved=False)}%5D={enc(text)}"
                )
            return "&".join(frags), []
        if style == "form":
            if explode:
                # field=name&order=asc（键不带外层参数名）
                return "&".join(
                    f"{_encode(k, allow_reserved=False)}="
                    f"{enc(_scalar_to_text(v, props[k]['type']))}"
                    for k, v in value.items()
                ), []
            # sort=field,name,order,asc
            return f"{ename}=" + ",".join(_flatten_object(value, props, enc)), []
        raise UnsupportedCombinationError(
            f"对象 query 参数不支持 style={style!r}（仅 form/deepObject）",
            location="query", parameter=name,
        )

    raise UnsupportedCombinationError(
        f"不支持的 schema 类型 {json_type!r}", location="query", parameter=name,
    )


def serialize_path(name: str, value: object, parameter: dict) -> str:
    """序列化一个 path 参数，返回要替换进路径模板的原始片段。"""
    style = parameter.get("style", "simple")
    explode = parameter.get("explode", False)
    schema = parameter.get("schema", {})
    json_type = schema.get("type")
    # path 中不允许 allowReserved（OpenAPI 仅对 query 定义该关键字）
    if parameter.get("allowReserved"):
        raise UnsupportedCombinationError(
            "allowReserved 仅适用于 query 参数", location="path", parameter=name,
        )
    enc = lambda v: _encode(v, allow_reserved=False)  # noqa: E731

    if style not in _PATH_STYLES:
        raise UnsupportedCombinationError(
            f"path 参数不支持 style={style!r}（支持子集：{sorted(_PATH_STYLES)}）",
            location="path", parameter=name,
        )

    prefix = {"simple": "", "label": ".", "matrix": ";"}[style]

    if json_type in _SCALAR_TYPES:
        text = enc(_scalar_to_text(value, json_type))
        if style == "matrix":
            return f";{enc(name)}={text}"
        return f"{prefix}{text}"

    if json_type == "array":
        if not isinstance(value, list):
            raise ValidationError(f"期望 array，实际得到 {type(value).__name__}: {value!r}")
        if len(value) == 0:
            raise ValidationError(f"path 参数 {name!r} 不允许为空数组（路径片段无法省略）")
        item_schema = _check_array(schema)
        texts = [enc(_scalar_to_text(v, item_schema["type"])) for v in value]
        if style == "simple":
            return ",".join(texts)
        if style == "label":
            if explode:
                return "." + ".".join(texts)
            return "." + ",".join(texts)
        # matrix
        ename = enc(name)
        if explode:
            return "".join(f";{ename}={t}" for t in texts)
        return f";{ename}=" + ",".join(texts)

    if json_type == "object":
        if not isinstance(value, dict):
            raise ValidationError(f"期望 object，实际得到 {type(value).__name__}: {value!r}")
        if len(value) == 0:
            raise ValidationError(f"path 参数 {name!r} 不允许为空对象（路径片段无法省略）")
        props = _check_object(schema)
        unknown = sorted(set(value) - set(props))
        if unknown:
            raise ValidationError(f"对象包含 schema 未声明的属性: {unknown}")
        for key, val in value.items():
            _scalar_to_text(val, props[key]["type"])
        if style == "simple":
            if explode:
                return ",".join(f"{k}={v}" for k, v in _object_kv_list(value, props, enc))
            return ",".join(_flatten_object(value, props, enc))
        if style == "label":
            if explode:
                return "." + ".".join(
                    f"{k}={v}" for k, v in _object_kv_list(value, props, enc)
                )
            return "." + ",".join(_flatten_object(value, props, enc))
        # matrix
        ename = enc(name)
        if explode:
            return "".join(f";{k}={v}" for k, v in _object_kv_list(value, props, enc))
        return f";{ename}=" + ",".join(_flatten_object(value, props, enc))

    raise UnsupportedCombinationError(
        f"不支持的 schema 类型 {json_type!r}", location="path", parameter=name,
    )
