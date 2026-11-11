"""轻量 SVG 字符串构建器（生成器专用，与采集器完全独立）。

只用标准库，输出可被浏览器与采集器同时读取的 SVG 1.1 文本。
"""

from __future__ import annotations

from html import escape


class SvgBuilder:
    def __init__(self, width: int, height: int, viewBox: str | None = None):
        self.width = width
        self.height = height
        self.viewBox = viewBox
        self._parts: list[str] = []

    def raw(self, text: str):
        self._parts.append(text)

    def open_group(self, **attrs):
        self._parts.append(
            f"  <g {self._fmt(attrs)}>"
        )

    def close_group(self):
        self._parts.append("  </g>")

    def line(self, x1, y1, x2, y2, **attrs):
        self._parts.append(
            f'  <line x1="{x1:g}" y1="{y1:g}" x2="{x2:g}" y2="{y2:g}" '
            f"{self._fmt(attrs)}/>"
        )

    def polyline(self, points, **attrs):
        pts = " ".join(f"{x:g},{y:g}" for x, y in points)
        self._parts.append(
            f'  <polyline points="{pts}" {self._fmt(attrs)}/>'
        )

    def path(self, d, **attrs):
        self._parts.append(f'  <path d="{d}" {self._fmt(attrs)}/>')

    def circle(self, cx, cy, r, **attrs):
        self._parts.append(
            f'  <circle cx="{cx:g}" cy="{cy:g}" r="{r:g}" '
            f"{self._fmt(attrs)}/>"
        )

    def rect(self, x, y, w, h, **attrs):
        self._parts.append(
            f'  <rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" '
            f"{self._fmt(attrs)}/>"
        )

    def text(self, x, y, content, **attrs):
        self._parts.append(
            f'  <text x="{x:g}" y="{y:g}" {self._fmt(attrs)}>'
            f"{escape(str(content))}</text>"
        )

    def clip_path(self, clip_id, x, y, w, h, **attrs):
        attr = self._fmt(attrs)
        self._parts.append(
            f'  <clipPath id="{clip_id}" {attr}>'
            f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}"/>'
            f"</clipPath>"
        )

    def use(self, href, x=0, y=0, **attrs):
        self._parts.append(
            f'  <use href="#{href}" x="{x:g}" y="{y:g}" '
            f"{self._fmt(attrs)}/>"
        )

    def build(self) -> str:
        vb = f' viewBox="{self.viewBox}"' if self.viewBox else ""
        head = (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{self.width}" height="{self.height}"{vb}>'
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            + head
            + "\n"
            + "\n".join(self._parts)
            + "\n</svg>\n"
        )

    @staticmethod
    def _fmt(attrs: dict) -> str:
        out = []
        for k, v in attrs.items():
            if v is None:
                continue
            k = k.replace("_", "-")
            out.append(f'{k}="{escape(str(v), quote=True)}"')
        return " ".join(out)


# 统一配色（虚构实验图风格）
STYLE = {
    "axis": {"stroke": "#333", "stroke-width": 1.5},
    "tick": {"stroke": "#333", "stroke-width": 1.2},
    "grid": {"stroke": "#ddd", "stroke-width": 0.8},
    "series": {"fill": "none", "stroke": "#1f6feb", "stroke-width": 2.4},
    "marker": {"fill": "#e11d48", "stroke": "#fff", "stroke-width": 1},
    "label": {"font-family": "sans-serif", "font-size": 13, "fill": "#222"},
    "title": {"font-family": "sans-serif", "font-size": 18, "fill": "#111"},
    "hidden": {"style": "display:none"},
}
