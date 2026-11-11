"""限定版 OpenAPI 3.0 文档加载与子集校验。

加载阶段即拒绝：非 OpenAPI 3.x、非 GET 操作、cookie/header 参数、
未支持的 style/explode 组合、深层嵌套 schema、请求体等。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .exceptions import SpecError, UnsupportedCombinationError

_SUPPORTED_QUERY_STYLES = {"form", "spaceDelimited", "pipeDelimited", "deepObject"}
_SUPPORTED_PATH_STYLES = {"simple", "label", "matrix"}
_SCALAR_TYPES = {"string", "integer", "boolean"}
_DEFAULT_EXPLODE = {"form": True}  # 其余 style 默认 False


@dataclass
class Parameter:
    name: str
    location: str  # query / path
    required: bool
    schema: dict
    style: str
    explode: bool
    allow_reserved: bool
    raw: dict = field(repr=False, default_factory=dict)


@dataclass
class Operation:
    operation_id: str
    method: str
    path_template: str  # 例如 /experiments/{exp_id}
    parameters: list[Parameter]
    path_params: list[Parameter]
    query_params: list[Parameter]
    raw: dict = field(repr=False, default_factory=dict)


@dataclass
class OpenAPISpec:
    title: str
    version: str
    server_path: str  # 例如 /v1
    operations: dict[str, Operation]
    paths: dict[str, Operation]
    raw: dict = field(repr=False, default_factory=dict)

    def operation(self, operation_id: str) -> Operation:
        try:
            return self.operations[operation_id]
        except KeyError:
            raise SpecError(f"文档中不存在 operationId={operation_id!r}") from None


def _validate_schema(schema: dict, *, location: str, name: str, depth: int = 0) -> None:
    if not isinstance(schema, dict) or "type" not in schema:
        raise SpecError(f"[{location}:{name}] schema 必须声明 type")
    t = schema["type"]
    if t in _SCALAR_TYPES:
        return
    if t == "array":
        if depth >= 1:
            raise UnsupportedCombinationError(
                "数组嵌套（对象数组/多维数组）不在支持子集内",
                location=location, parameter=name,
            )
        items = schema.get("items")
        if not isinstance(items, dict):
            raise SpecError(f"[{location}:{name}] array 必须声明 items")
        _validate_schema(items, location=location, name=name, depth=depth + 1)
        return
    if t == "object":
        if depth >= 1:
            raise UnsupportedCombinationError(
                "多层嵌套对象不在支持子集内", location=location, parameter=name,
            )
        props = schema.get("properties")
        if not isinstance(props, dict) or not props:
            raise SpecError(f"[{location}:{name}] object 必须显式声明一层 properties")
        for prop_name, prop_schema in props.items():
            if not isinstance(prop_schema, dict) or prop_schema.get("type") not in _SCALAR_TYPES:
                raise UnsupportedCombinationError(
                    f"对象属性 {prop_name!r} 必须是标量；嵌套结构不在支持子集内",
                    location=location, parameter=name,
                )
        return
    raise UnsupportedCombinationError(
        f"不支持的 schema 类型 {t!r}", location=location, parameter=name,
    )


def _build_parameter(raw: dict) -> Parameter:
    name = raw.get("name")
    location = raw.get("in")
    if not name or location not in ("query", "path"):
        raise UnsupportedCombinationError(
            f"参数 {name!r} 的位置 {location!r} 不在支持子集（仅 query/path）",
            parameter=name,
        )
    if "content" in raw:
        raise UnsupportedCombinationError(
            "content 形式的参数不在支持子集内", location=location, parameter=name,
        )
    schema = raw.get("schema")
    if not isinstance(schema, dict):
        raise SpecError(f"[{location}:{name}] 缺少 schema")
    _validate_schema(schema, location=location, name=name)

    json_type = schema["type"]
    style = raw.get("style", "form" if location == "query" else "simple")
    explode = raw.get("explode", _DEFAULT_EXPLODE.get(style, False))

    if location == "query":
        if style not in _SUPPORTED_QUERY_STYLES:
            raise UnsupportedCombinationError(
                f"query 参数不支持 style={style!r}", location="query", parameter=name,
            )
        if style in ("spaceDelimited", "pipeDelimited", "deepObject") and explode:
            raise SpecError(
                f"[{location}:{name}] {style} 仅允许 explode=false"
            )
        if style == "deepObject" and json_type != "object":
            raise SpecError(f"[{location}:{name}] deepObject 只能用于 object")
        if style in ("spaceDelimited", "pipeDelimited") and json_type != "array":
            raise SpecError(f"[{location}:{name}] {style} 只能用于 array")
    else:
        if style not in _SUPPORTED_PATH_STYLES:
            raise UnsupportedCombinationError(
                f"path 参数不支持 style={style!r}", location="path", parameter=name,
            )
        if raw.get("allowReserved"):
            raise SpecError(f"[path:{name}] allowReserved 仅适用于 query 参数")

    if json_type in _SCALAR_TYPES and style not in ("form", "simple", "label", "matrix"):
        raise SpecError(f"[{location}:{name}] 标量参数不可使用 {style}")
    if json_type == "array" and style == "deepObject":
        raise SpecError(f"[{location}:{name}] deepObject 不能用于数组")

    return Parameter(
        name=name,
        location=location,
        required=bool(raw.get("required", False)),
        schema=schema,
        style=style,
        explode=explode,
        allow_reserved=bool(raw.get("allowReserved", False)),
        raw=raw,
    )


def load_spec(path: str | Path) -> OpenAPISpec:
    """从 YAML 文件加载并校验限定版 OpenAPI 文档。"""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SpecError(f"OpenAPI 文档不存在: {path}") from None
    except yaml.YAMLError as exc:
        raise SpecError(f"OpenAPI 文档 YAML 解析失败: {exc}") from None

    if not isinstance(raw, dict) or not str(raw.get("openapi", "")).startswith("3."):
        raise SpecError("仅支持 OpenAPI 3.x 文档")
    info = raw.get("info") or {}
    title = info.get("title", "")
    version = info.get("version", "")

    servers = raw.get("servers") or []
    if not servers or "url" not in servers[0]:
        raise SpecError("文档必须声明 servers[0].url")
    server_url = servers[0]["url"]
    if not server_url.startswith("/"):
        raise SpecError("限定子集仅接受相对路径 server（例如 /v1）")
    server_path = server_url.rstrip("/")

    operations: dict[str, Operation] = {}
    by_path: dict[str, Operation] = {}

    for path_template, path_item in (raw.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        path_level_params = path_item.get("parameters", [])
        for method, op_raw in path_item.items():
            if method.lower() not in ("get", "head"):
                if method.lower() in ("post", "put", "patch", "delete", "options", "trace"):
                    # 文档中出现写操作即拒绝：构造器只做只读采集
                    raise UnsupportedCombinationError(
                        f"路径 {path_template} 声明了 {method.upper()} 写操作，"
                        "只读采集器拒绝加载该文档",
                    )
                continue
            if method.lower() != "get":
                raise UnsupportedCombinationError(
                    f"仅支持 GET，发现 {method.upper()} {path_template}"
                )
            if op_raw.get("requestBody"):
                raise UnsupportedCombinationError(
                    f"{path_template} 含 requestBody，只读子集不允许"
                )
            params_raw = list(path_level_params) + list(op_raw.get("parameters", []))
            parameters = [_build_parameter(p) for p in params_raw]
            names = [p.name for p in parameters]
            if len(names) != len(set(names)):
                raise SpecError(f"{path_template} 存在重复参数名")
            op_id = op_raw.get("operationId")
            if not op_id:
                raise SpecError(f"{path_template} GET 缺少 operationId")
            operation = Operation(
                operation_id=op_id,
                method="GET",
                path_template=path_template,
                parameters=parameters,
                path_params=[p for p in parameters if p.location == "path"],
                query_params=[p for p in parameters if p.location == "query"],
                raw=op_raw,
            )
            operations[op_id] = operation
            by_path[path_template] = operation

    if not operations:
        raise SpecError("文档中没有任何可用操作")

    return OpenAPISpec(
        title=title, version=version, server_path=server_path,
        operations=operations, paths=by_path, raw=raw,
    )
