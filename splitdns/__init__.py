"""splitdns — 可加载配置的分支机构域名解析分流。

模块划分:
- normalize: 域名规范化(去末尾根点、按标签比较、忽略大小写、IDN<->ASCII)
- config:    配置加载与校验(分支清单、内部后缀表、受管解析器列表)
- engine:    按分支生成分流规则 + 试算查询入口
"""

from .normalize import normalize_name, split_labels, suffix_matches
from .config import ConfigError, load_config, validate_config
from .engine import Engine

__all__ = [
    "normalize_name",
    "split_labels",
    "suffix_matches",
    "ConfigError",
    "load_config",
    "validate_config",
    "Engine",
]
