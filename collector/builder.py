"""请求构造器：把结构化输入 + OpenAPI 操作定义组装成精确的 HTTP 请求。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .exceptions import ValidationError
from .loader import OpenAPISpec, Parameter
from .serializer import serialize_path, serialize_query

_PATH_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


@dataclass
class BuiltRequest:
    method: str
    server_path: str
    path: str                  # 已替换并编码的 path（含 server 前缀）
    query_string: str          # 原始 query string（无前置 ?），可能为空
    url: str                   # 完整相对 URL（path?query）
    omitted: list[str]         # 因空数组/空对象被省略的参数名
    parameter_plan: list[dict] = field(default_factory=list)

    @property
    def request_line(self) -> str:
        target = self.path + (f"?{self.query_string}" if self.query_string else "")
        return f"{self.method} {target} HTTP/1.1"


def _check_constraints(parameter: Parameter, value: object) -> None:
    """执行 schema 中的基础约束（minimum/maximum/enum）。"""
    schema = parameter.schema
    t = schema.get("type")
    if t == "integer" and isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValidationError(
                f"参数 {parameter.name!r}={value} 小于 minimum={schema['minimum']}"
            )
        if "maximum" in schema and value > schema["maximum"]:
            raise ValidationError(
                f"参数 {parameter.name!r}={value} 大于 maximum={schema['maximum']}"
            )
    if "enum" in schema and value not in schema["enum"]:
        raise ValidationError(
            f"参数 {parameter.name!r}={value!r} 不在 enum {schema['enum']} 内"
        )


class RequestBuilder:
    """依据文档把结构化参数构造成只读 GET 请求；不支持的组合直接报错。"""

    def __init__(self, spec: OpenAPISpec) -> None:
        self.spec = spec

    def build(self, operation_id: str, params: dict | None = None) -> BuiltRequest:
        params = dict(params or {})
        operation = self.spec.operation(operation_id)
        if operation.method != "GET":
            # 双保险：loader 已保证只加载 GET
            raise ValidationError("构造器仅允许 GET 只读请求")

        known = {p.name: p for p in operation.parameters}
        unknown = sorted(set(params) - set(known))
        if unknown:
            raise ValidationError(
                f"输入包含文档未声明的参数 {unknown}；不允许发送文档外参数"
            )

        plan: list[dict] = []
        omitted: list[str] = []

        # 1) path 参数
        path = operation.path_template
        placeholders = _PATH_PLACEHOLDER.findall(path)
        for parameter in operation.path_params:
            if parameter.name not in params:
                if parameter.required:
                    raise ValidationError(f"缺少必填 path 参数 {parameter.name!r}")
                continue
            value = params.pop(parameter.name)
            _check_constraints(parameter, value)
            fragment = serialize_path(parameter.name, value, parameter.raw)
            if parameter.name not in placeholders:
                raise ValidationError(
                    f"path 模板 {operation.path_template} 中没有占位符 {{{parameter.name}}}"
                )
            path = path.replace("{" + parameter.name + "}", fragment)
            plan.append({
                "name": parameter.name, "in": "path", "style": parameter.style,
                "explode": parameter.explode, "fragment": fragment,
            })
        missing_placeholders = [p for p in placeholders if "{" + p + "}" in path]
        if missing_placeholders:
            raise ValidationError(f"path 占位符未提供值: {missing_placeholders}")

        # 2) query 参数（保持文档声明顺序，保证期望串可精确复现）
        query_fragments: list[str] = []
        for parameter in operation.query_params:
            if parameter.name not in params:
                if parameter.required:
                    raise ValidationError(f"缺少必填 query 参数 {parameter.name!r}")
                continue
            value = params.pop(parameter.name)
            # 空字符串标量视为“不传”（避免生成 key= 这类语义模糊的请求）
            if parameter.schema.get("type") == "string" and value == "":
                plan.append({
                    "name": parameter.name, "in": "query", "style": parameter.style,
                    "explode": parameter.explode, "fragment": None,
                    "omitted": True, "reason": "空字符串标量视为不传并省略",
                })
                omitted.append(parameter.name)
                continue
            _check_constraints(parameter, value)
            fragment, was_omitted = serialize_query(
                parameter.name, value, parameter.raw
            )
            if was_omitted:
                omitted.extend(was_omitted)
                plan.append({
                    "name": parameter.name, "in": "query", "style": parameter.style,
                    "explode": parameter.explode, "fragment": None,
                    "omitted": True, "reason": "空数组/空对象按 OpenAPI 约定省略",
                })
                continue
            query_fragments.append(fragment)
            plan.append({
                "name": parameter.name, "in": "query", "style": parameter.style,
                "explode": parameter.explode, "fragment": fragment,
                "allowReserved": parameter.allow_reserved,
            })

        query_string = "&".join(query_fragments)
        full_path = self.spec.server_path + path
        url = full_path + (f"?{query_string}" if query_string else "")
        return BuiltRequest(
            method="GET",
            server_path=self.spec.server_path,
            path=full_path,
            query_string=query_string,
            url=url,
            omitted=omitted,
            parameter_plan=plan,
        )
