"""测试用的假页存储：记录页被打开（读取）的顺序。"""

from collections.abc import Sequence

from geoindex import Site


class FakeStorage:
    """page_id -> 站点序列 的内存存储，read_page 可直接当回调。"""

    def __init__(self, pages: dict[int, Sequence[Site]]):
        self._pages = dict(pages)
        self.read_order: list[int] = []

    def read_page(self, page_id: int) -> Sequence[Site]:
        self.read_order.append(page_id)
        return self._pages[page_id]
