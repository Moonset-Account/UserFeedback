"""源站回显解析器：把原始 query string / path 按 OpenAPI 语义反向解析。

与 collector.serializer 是两份独立实现（一个编码、一个解码），
单元测试通过往返一致性互相校验。
"""

from __future__ import annotations

from urllib.parse import unquote


class ParseError(Exception):
    """源站无法按声明语义解析请求。"""


def _unquote(text: str) -> str:
    return unquote(text, encoding="utf-8", errors="replace")


def _coerce(text: str, json_type: str):
    if json_type == "string":
        return _unquote(text)
    if json_type == "integer":
        s = _unquote(text)
        if not s or not (s.lstrip("-").isdigit()):
            raise ParseError(f"无法解析为 integer: {text!r}")
        return int(s)
    if json_type == "boolean":
        s = _unquote(text)
        if s == "true":
            return True
        if s == "false":
            return False
        raise ParseError(f"无法解析为 boolean: {text!r}")
    raise ParseError(f"不支持的类型 {json_type}")


def split_raw_query(raw_query: str) -> list[tuple[str, str]]:
    """保留重复键与原始编码，按 & 切分，不做任何解码。"""
    pairs: list[tuple[str, str]] = []
    if not raw_query:
        return pairs
    for chunk in raw_query.split("&"):
        if chunk == "":
            continue
        if "=" in chunk:
            k, v = chunk.split("=", 1)
        else:
            k, v = chunk, ""
        pairs.append((k, v))
    return pairs


def _parse_scalar_query(raw_pairs, parameter) -> tuple[object, bool]:
    """返回 (值, 是否被省略)。"""
    matches = [(k, v) for k, v in raw_pairs if k == _enc_name(parameter.name)]
    if not matches:
        return None, True
    _, raw_value = matches[0]
    return _coerce(raw_value, parameter.schema["type"]), False


def _enc_name(name: str) -> str:
    from urllib.parse import quote
    return quote(name, safe="-_.~")


def parse_query(raw_pairs: list[tuple[str, str]], parameters: list) -> dict:
    """按操作声明的 query 参数逐个反向解析。"""
    result: dict = {}
    for parameter in parameters:
        name = parameter.name
        style = parameter.style
        explode = parameter.explode
        schema = parameter.schema
        json_type = schema["type"]
        ename = _enc_name(name)

        if json_type in ("string", "integer", "boolean"):
            value, omitted = _parse_scalar_query(raw_pairs, parameter)
            if not omitted:
                result[name] = value
            continue

        if json_type == "array":
            item_type = schema["items"]["type"]
            if style == "form" and explode:
                values = [_coerce(v, item_type) for k, v in raw_pairs if k == ename]
                # 键未出现 = 参数未传，省略；只有真实出现重复键时才返回数组
                if values:
                    result[name] = values
                continue
            # 单键形式：form(no explode) / spaceDelimited / pipeDelimited
            match = next(((k, v) for k, v in raw_pairs if k == ename), None)
            if match is None:
                continue
            _, raw_value = match
            if raw_value == "":
                result[name] = []
                continue
            if style == "form":
                pieces = raw_value.split(",")
            elif style == "spaceDelimited":
                # 空格在请求里以 %20 形式出现
                pieces = raw_value.split("%20")
            elif style == "pipeDelimited":
                pieces = raw_value.split("|")
            else:
                raise ParseError(f"数组不支持 style={style}")
            result[name] = [_coerce(p, item_type) for p in pieces]
            continue

        if json_type == "object":
            props = schema["properties"]
            if style == "deepObject":
                # 采集器把方括号编码为 %5B / %5D，这里按编码形式匹配
                prefix = ename + "%5B"
                suffix = "%5D"
                obj: dict = {}
                for k, v in raw_pairs:
                    if k.startswith(prefix) and k.endswith(suffix):
                        key = _unquote(k[len(prefix):-len(suffix)])
                        if key not in props:
                            raise ParseError(f"deepObject 出现未声明属性 {key!r}")
                        obj[key] = _coerce(v, props[key]["type"])
                if obj:
                    result[name] = obj
                continue
            if style == "form":
                if explode:
                    obj = {}
                    for k, v in raw_pairs:
                        key = _unquote(k)
                        if key in props:
                            obj[key] = _coerce(v, props[key]["type"])
                    if obj:
                        result[name] = obj
                    continue
                match = next(((k, v) for k, v in raw_pairs if k == ename), None)
                if match is None:
                    continue
                pieces = match[1].split(",")
                if len(pieces) % 2:
                    raise ParseError(f"对象 {name} 的扁平串必须成对出现: {match[1]!r}")
                obj = {}
                for i in range(0, len(pieces), 2):
                    key = _unquote(pieces[i])
                    if key not in props:
                        raise ParseError(f"对象 {name} 出现未声明属性 {key!r}")
                    obj[key] = _coerce(pieces[i + 1], props[key]["type"])
                result[name] = obj
                continue
            raise ParseError(f"对象不支持 style={style}")

    return result


def _match_path_template(raw_path: str, template: str) -> dict[str, str] | None:
    """把原始（仍带百分号编码的）路径与模板逐段匹配，返回占位符->原始片段。"""
    raw_segments = [s for s in raw_path.split("/") if s != ""]
    tpl_segments = [s for s in template.split("/") if s != ""]
    if len(raw_segments) != len(tpl_segments):
        return None
    captured: dict[str, str] = {}
    for raw_seg, tpl_seg in zip(raw_segments, tpl_segments):
        if tpl_seg.startswith("{") and tpl_seg.endswith("}"):
            captured[tpl_seg[1:-1]] = raw_seg
        elif raw_seg != tpl_seg:
            return None
    return captured


def parse_path(raw_path: str, template: str, parameters: list) -> dict:
    captured = _match_path_template(raw_path, template)
    if captured is None:
        raise ParseError(f"路径 {raw_path!r} 与模板 {template!r} 不匹配")
    result: dict = {}
    for parameter in parameters:
        name = parameter.name
        if name not in captured:
            raise ParseError(f"模板缺少占位符 {name}")
        fragment = captured[name]
        result[name] = _parse_path_fragment(fragment, parameter)
    return result


def _parse_path_fragment(fragment: str, parameter) -> object:
    style = parameter.style
    explode = parameter.explode
    schema = parameter.schema
    json_type = schema["type"]

    def strip_prefix(text: str, marker: str) -> str:
        if not text.startswith(marker):
            raise ParseError(f"期望 {style} 片段以 {marker!r} 开头: {text!r}")
        return text[len(marker):]

    if json_type in ("string", "integer", "boolean"):
        if style == "simple":
            return _coerce(fragment, json_type)
        if style == "label":
            return _coerce(strip_prefix(fragment, "."), json_type)
        if style == "matrix":
            body = strip_prefix(fragment, ";")
            ename = _enc_name(parameter.name)
            prefix = ename + "="
            if not body.startswith(prefix):
                raise ParseError(f"matrix 片段期望 {prefix!r}: {fragment!r}")
            return _coerce(body[len(prefix):], json_type)

    if json_type == "array":
        item_type = schema["items"]["type"]
        if style == "simple":
            return [_coerce(p, item_type) for p in fragment.split(",")]
        if style == "label":
            body = strip_prefix(fragment, ".")
            if explode:
                return [_coerce(p, item_type) for p in body.split(".")]
            return [_coerce(p, item_type) for p in body.split(",")]
        if style == "matrix":
            ename = _enc_name(parameter.name)
            if explode:
                values = []
                for chunk in strip_prefix(fragment, ";").split(";"):
                    prefix = ename + "="
                    if not chunk.startswith(prefix):
                        raise ParseError(f"matrix explode 片段异常: {chunk!r}")
                    values.append(_coerce(chunk[len(prefix):], item_type))
                return values
            body = strip_prefix(fragment, ";")
            prefix = ename + "="
            if not body.startswith(prefix):
                raise ParseError(f"matrix 片段期望 {prefix!r}: {fragment!r}")
            return [_coerce(p, item_type)
                    for p in body[len(prefix):].split(",")]

    if json_type == "object":
        props = schema["properties"]

        def build_object(pairs: list[str]) -> dict:
            if len(pairs) % 2:
                raise ParseError("对象路径片段必须为偶数个元素")
            obj = {}
            for i in range(0, len(pairs), 2):
                key = _unquote(pairs[i])
                if key not in props:
                    raise ParseError(f"对象出现未声明属性 {key!r}")
                obj[key] = _coerce(pairs[i + 1], props[key]["type"])
            return obj

        if style == "simple":
            if explode:
                obj = {}
                for pair in fragment.split(","):
                    if "=" not in pair:
                        raise ParseError(f"simple explode 对象片段异常: {pair!r}")
                    k, v = pair.split("=", 1)
                    key = _unquote(k)
                    obj[key] = _coerce(v, props[key]["type"])
                return obj
            return build_object(fragment.split(","))
        if style == "label":
            body = strip_prefix(fragment, ".")
            if explode:
                obj = {}
                for pair in body.split("."):
                    if "=" not in pair:
                        raise ParseError(f"label explode 对象片段异常: {pair!r}")
                    k, v = pair.split("=", 1)
                    key = _unquote(k)
                    obj[key] = _coerce(v, props[key]["type"])
                return obj
            return build_object(body.split(","))
        if style == "matrix":
            body = strip_prefix(fragment, ";")
            if explode:
                obj = {}
                for chunk in body.split(";"):
                    if "=" not in chunk:
                        raise ParseError(f"matrix 对象片段异常: {chunk!r}")
                    k, v = chunk.split("=", 1)
                    key = _unquote(k)
                    if key not in props:
                        raise ParseError(f"对象出现未声明属性 {key!r}")
                    obj[key] = _coerce(v, props[key]["type"])
                return obj
            ename = _enc_name(parameter.name)
            prefix = ename + "="
            if not body.startswith(prefix):
                raise ParseError(f"matrix 片段期望 {prefix!r}: {fragment!r}")
            return build_object(body[len(prefix):].split(","))

    raise ParseError(f"不支持的 path 片段类型 {json_type}")
