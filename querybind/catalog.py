"""本地合成元数据目录：只保存测试用的小型行集与唯一键信息。"""

from dataclasses import dataclass, field


@dataclass
class TableData:
    name: str
    columns: list[str]
    rows: list[dict]
    # 唯一键集合，每个元素是一个列名元组（单列键为 ("id",)）
    unique_keys: list[tuple[str, ...]] = field(default_factory=list)


class Catalog:
    def __init__(self, tables=None):
        self._tables = {}
        for t in tables or []:
            self.add(t)

    def add(self, table_data: TableData):
        self._tables[table_data.name] = table_data

    def get(self, name) -> TableData:
        if name not in self._tables:
            raise KeyError(f"unknown base table: {name}")
        return self._tables[name]

    def names(self):
        return sorted(self._tables)
