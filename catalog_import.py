"""供应商目录 XML 安全导入模块（仅 Python 3 标准库）。

设计目标：可审计的本地导入。
- 输入为分段（chunked）读取的 XML 字节流、允许的命名空间 URI 列表和允许的字段列表；
- DTD（<!DOCTYPE>）、实体声明、外部引用不属于目录格式，解析时一律拒绝；
- 嵌套深度、节点总数、单字段文本长度分别设限；
- 命名空间按 URI 比对，与前缀无关；
- 有效记录输出标准化对象；结构不合法的整份文档给出所在位置（行/列）和错误分类。
"""

import xml.parsers.expat as expat

# ---------------------------------------------------------------- 常量与默认值

ROOT_ELEMENT = "catalog"
RECORD_ELEMENT = "supplier"
RECORD_ID_ATTRIBUTE = "id"

DEFAULT_ALLOWED_NAMESPACES = ("urn:example:catalog:v1",)
DEFAULT_ALLOWED_FIELDS = ("name", "email", "phone")

DEFAULT_MAX_DEPTH = 16
DEFAULT_MAX_NODES = 10000
DEFAULT_MAX_FIELD_LENGTH = 4096
CHUNK_SIZE = 4096

# 错误分类（稳定字符串，供调用方与测试断言）
CATEGORY_MALFORMED_XML = "malformed_xml"
CATEGORY_DTD_FORBIDDEN = "dtd_forbidden"
CATEGORY_ENTITY_FORBIDDEN = "entity_forbidden"
CATEGORY_EXTERNAL_REFERENCE_FORBIDDEN = "external_reference_forbidden"
CATEGORY_DEPTH_EXCEEDED = "depth_exceeded"
CATEGORY_NODE_COUNT_EXCEEDED = "node_count_exceeded"
CATEGORY_FIELD_LENGTH_EXCEEDED = "field_length_exceeded"
CATEGORY_NAMESPACE_NOT_ALLOWED = "namespace_not_allowed"
CATEGORY_FIELD_NOT_ALLOWED = "field_not_allowed"
CATEGORY_STRUCTURE_INVALID = "structure_invalid"

_NAMESPACE_SEPARATOR = " "
_XML_NAMESPACE_URI = "http://www.w3.org/XML/1998/namespace"


class CatalogImportError(Exception):
    """导入失败：携带错误分类与文档内位置（行/列，1 起始）。"""

    def __init__(self, category, message, line=None, column=None):
        super().__init__(message)
        self.category = category
        self.message = message
        self.line = line
        self.column = column

    def to_dict(self):
        return {
            "category": self.category,
            "message": self.message,
            "line": self.line,
            "column": self.column,
        }


class ImportConfig:
    """导入配置：允许的命名空间 URI、允许的字段、三类上限。"""

    def __init__(
        self,
        allowed_namespaces=DEFAULT_ALLOWED_NAMESPACES,
        allowed_fields=DEFAULT_ALLOWED_FIELDS,
        max_depth=DEFAULT_MAX_DEPTH,
        max_nodes=DEFAULT_MAX_NODES,
        max_field_length=DEFAULT_MAX_FIELD_LENGTH,
    ):
        self.allowed_namespaces = frozenset(allowed_namespaces)
        self.allowed_fields = frozenset(allowed_fields)
        self.max_depth = int(max_depth)
        self.max_nodes = int(max_nodes)
        self.max_field_length = int(max_field_length)
        if self.max_depth < 1:
            raise ValueError("max_depth 必须 >= 1")
        if self.max_nodes < 1:
            raise ValueError("max_nodes 必须 >= 1")
        if self.max_field_length < 1:
            raise ValueError("max_field_length 必须 >= 1")

    @classmethod
    def from_dict(cls, data):
        return cls(
            allowed_namespaces=data.get("allowed_namespaces", DEFAULT_ALLOWED_NAMESPACES),
            allowed_fields=data.get("allowed_fields", DEFAULT_ALLOWED_FIELDS),
            max_depth=data.get("max_depth", DEFAULT_MAX_DEPTH),
            max_nodes=data.get("max_nodes", DEFAULT_MAX_NODES),
            max_field_length=data.get("max_field_length", DEFAULT_MAX_FIELD_LENGTH),
        )


# ---------------------------------------------------------------- 事件处理器


class _CatalogHandler:
    """expat 事件处理器：执行安全策略并累积标准化记录。"""

    def __init__(self, config):
        self.config = config
        self.records = []
        self.node_count = 0
        # 栈元素：{"uri","local","kind","text_parts","record","fields_seen"}
        self.stack = []
        self.parser = None  # 由 parse_chunks 注入，用于取当前位置

    # -- 位置与失败 -----------------------------------------------------

    def _position(self):
        if self.parser is None:
            return (None, None)
        return (self.parser.CurrentLineNumber, self.parser.CurrentColumnNumber)

    def _fail(self, category, message):
        line, column = self._position()
        raise CatalogImportError(category, message, line, column)

    # -- DTD / 实体 / 外部引用拒绝（回调签名各异，统一 *args） ----------

    def reject_dtd(self, *args):
        self._fail(CATEGORY_DTD_FORBIDDEN, "目录格式不允许 DTD/<!DOCTYPE> 声明")

    def reject_entity_decl(self, *args):
        self._fail(CATEGORY_ENTITY_FORBIDDEN, "目录格式不允许实体声明")

    def reject_external_ref(self, *args):
        self._fail(
            CATEGORY_EXTERNAL_REFERENCE_FORBIDDEN,
            "目录格式不允许外部引用（外部 DTD/外部实体）",
        )

    def reject_skipped_entity(self, *args):
        self._fail(CATEGORY_ENTITY_FORBIDDEN, "目录格式不允许实体引用")

    # -- 元素事件 -------------------------------------------------------

    @staticmethod
    def _split_name(name):
        if _NAMESPACE_SEPARATOR in name:
            uri, local = name.split(_NAMESPACE_SEPARATOR, 1)
            return uri, local
        return None, name

    def start_element(self, name, attrs):
        config = self.config

        self.node_count += 1
        if self.node_count > config.max_nodes:
            self._fail(
                CATEGORY_NODE_COUNT_EXCEEDED,
                "节点总数超过上限 %d" % config.max_nodes,
            )

        depth = len(self.stack) + 1
        if depth > config.max_depth:
            self._fail(
                CATEGORY_DEPTH_EXCEEDED,
                "嵌套深度超过上限 %d" % config.max_depth,
            )

        uri, local = self._split_name(name)
        if uri not in config.allowed_namespaces:
            self._fail(
                CATEGORY_NAMESPACE_NOT_ALLOWED,
                "命名空间 %r 不在允许列表中" % (uri if uri is not None else "(无)"),
            )

        parent = self.stack[-1] if self.stack else None

        if parent is None:
            if local != ROOT_ELEMENT:
                self._fail(
                    CATEGORY_STRUCTURE_INVALID,
                    "根元素必须是 %s，实际为 %s" % (ROOT_ELEMENT, local),
                )
            kind = "root"
        elif parent["kind"] == "root":
            if local != RECORD_ELEMENT:
                self._fail(
                    CATEGORY_STRUCTURE_INVALID,
                    "%s 下只允许 %s 记录，实际为 %s" % (ROOT_ELEMENT, RECORD_ELEMENT, local),
                )
            kind = "record"
        elif parent["kind"] == "record":
            if local not in config.allowed_fields:
                self._fail(
                    CATEGORY_FIELD_NOT_ALLOWED,
                    "字段 %r 不在允许列表中" % local,
                )
            if local in parent["fields_seen"]:
                self._fail(
                    CATEGORY_STRUCTURE_INVALID,
                    "记录 %r 中字段 %r 重复" % (parent["record"]["id"], local),
                )
            parent["fields_seen"].add(local)
            kind = "field"
        else:
            self._fail(
                CATEGORY_STRUCTURE_INVALID,
                "字段 %r 不允许嵌套子元素" % parent["local"],
            )

        node = {
            "uri": uri,
            "local": local,
            "kind": kind,
            "text_parts": [],
            "record": None,
            "fields_seen": set(),
        }

        # 属性审计：记录元素仅允许无命名空间的 id 属性，其余元素不允许属性。
        for attr_name, attr_value in attrs.items():
            attr_uri, attr_local = self._split_name(attr_name)
            if kind == "record" and attr_uri is None and attr_local == RECORD_ID_ATTRIBUTE:
                record_id = attr_value.strip()
                if not record_id:
                    self._fail(CATEGORY_STRUCTURE_INVALID, "记录 id 属性不能为空")
                node["record"] = {"id": record_id, "fields": {}}
            elif attr_uri == _XML_NAMESPACE_URI:
                # xml:lang 等标准属性：忽略但不进入输出，保持可审计。
                continue
            else:
                self._fail(
                    CATEGORY_FIELD_NOT_ALLOWED,
                    "元素 %s 上不允许属性 %r" % (local, attr_name),
                )

        if kind == "record" and node["record"] is None:
            self._fail(CATEGORY_STRUCTURE_INVALID, "记录缺少必需的 id 属性")

        self.stack.append(node)

    def end_element(self, name):
        node = self.stack.pop()
        if node["kind"] == "field":
            value = "".join(node["text_parts"]).strip()
            record = self.stack[-1]["record"]
            record["fields"][node["local"]] = value
        elif node["kind"] == "record":
            self.records.append(node["record"])

    def char_data(self, data):
        if not self.stack:
            return  # 根元素之外的空白由 expat 校验，忽略
        top = self.stack[-1]
        if top["kind"] == "field":
            top["text_parts"].append(data)
            if sum(len(part) for part in top["text_parts"]) > self.config.max_field_length:
                self._fail(
                    CATEGORY_FIELD_LENGTH_EXCEEDED,
                    "字段 %r 文本长度超过上限 %d"
                    % (top["local"], self.config.max_field_length),
                )
        elif data.strip():
            self._fail(
                CATEGORY_STRUCTURE_INVALID,
                "元素 %s 内不允许出现文本内容" % top["local"],
            )


# ---------------------------------------------------------------- 解析入口


def _build_parser(handler):
    parser = expat.ParserCreate(None, _NAMESPACE_SEPARATOR)
    handler.parser = parser
    parser.StartElementHandler = handler.start_element
    parser.EndElementHandler = handler.end_element
    parser.CharacterDataHandler = handler.char_data
    # 安全策略：DTD、实体声明、外部引用一律拒绝。
    parser.StartDoctypeDeclHandler = handler.reject_dtd
    parser.EndDoctypeDeclHandler = handler.reject_dtd
    parser.EntityDeclHandler = handler.reject_entity_decl
    parser.UnparsedEntityDeclHandler = handler.reject_entity_decl
    parser.NotationDeclHandler = handler.reject_dtd
    parser.ExternalEntityRefHandler = handler.reject_external_ref
    parser.SkippedEntityHandler = handler.reject_skipped_entity
    return parser


def _classify_expat_error(error):
    message = expat.ErrorString(error.code) or ""
    if "entity" in message.lower():
        return CATEGORY_ENTITY_FORBIDDEN
    return CATEGORY_MALFORMED_XML


def parse_chunks(chunks, config):
    """从分段字节流解析目录，返回标准化记录列表。

    chunks: 可迭代的 bytes 段（分段读取的 XML 数据）。
    有效记录形如 {"id": "...", "fields": {"name": "...", ...}}。
    任何策略违规或结构错误都抛出 CatalogImportError（含位置与分类）。
    """
    handler = _CatalogHandler(config)
    parser = _build_parser(handler)
    try:
        for chunk in chunks:
            if not isinstance(chunk, (bytes, bytearray)):
                raise TypeError("分段输入必须是 bytes")
            parser.Parse(bytes(chunk), False)
        parser.Parse(b"", True)
    except CatalogImportError:
        raise
    except expat.ExpatError as error:
        raise CatalogImportError(
            _classify_expat_error(error),
            "XML 解析失败: %s" % expat.ErrorString(error.code),
            error.lineno,
            error.offset,
        )
    return handler.records


def read_chunks(fileobj, size=CHUNK_SIZE):
    """分段读取二进制流，模拟受限的本地导入通道。"""
    while True:
        chunk = fileobj.read(size)
        if not chunk:
            return
        yield chunk


def parse_file(path, config, chunk_size=CHUNK_SIZE):
    """分段读取本地文件并解析。"""
    with open(path, "rb") as fileobj:
        return parse_chunks(read_chunks(fileobj, chunk_size), config)
