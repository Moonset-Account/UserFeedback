"""本地样例源站生成器（与采集器完全分离）。

生成内容全部为虚构的数字标本数据：
- 三个 IIIF Presentation 3 清单场景（正常 / 边界 / 完整验收）
- 纯标准库生成的 PNG 图像与缩略图
- 本地 IIIF Image API 3.0 level 0 静态服务样例（info.json）

采集器不 import 本模块；网页/测试只通过公开 HTTP 接口或文件路径读取产物。
"""
from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

IIIF_CONTEXT_3 = "http://iiif.io/api/presentation/3/context.json"
IMAGE_API_3 = "ImageService3"
IMAGE_API_PROFILE = "level0"

# 虚构标本配色（R, G, B）
PALETTE = [
    (66, 133, 244),
    (219, 68, 55),
    (244, 180, 0),
    (52, 168, 83),
    (171, 71, 188),
    (0, 172, 193),
    (255, 112, 67),
    (94, 122, 153),
]


# --------------------------------------------------------------------------- #
# 纯标准库 PNG 生成
# --------------------------------------------------------------------------- #
def _png_chunk(tag: bytes, data: bytes) -> bytes:
    chunk = tag + data
    return struct.pack(">I", len(data)) + chunk + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)


def write_png(path: Path, width: int, height: int, seed: int) -> None:
    """生成带竖条纹与标本编号标记的 RGB PNG。"""
    base = PALETTE[seed % len(PALETTE)]
    accent = PALETTE[(seed + 3) % len(PALETTE)]
    rows = bytearray()
    stripe = max(16, width // 8)
    for y in range(height):
        row = bytearray([0])  # 每行 filter type 0
        for x in range(width):
            if (x // stripe) % 2 == 0:
                r, g, b = base
            else:
                r, g, b = tuple(int(c * 0.55) for c in base)
            # 中部画一条“标本主体”色带
            if height // 3 <= y <= height * 2 // 3 and width // 5 <= x <= width * 4 // 5:
                r, g, b = accent
            # 四角定位标记
            if (x < 12 and y < 12) or (x >= width - 12 and y >= height - 12):
                r, g, b = 245, 245, 245
            row.extend((r, g, b))
        rows.extend(row)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8bit RGB
    payload = b"\x89PNG\r\n\x1a\n"
    payload += _png_chunk(b"IHDR", ihdr)
    payload += _png_chunk(b"IDAT", zlib.compress(bytes(rows), 9))
    payload += _png_chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


# --------------------------------------------------------------------------- #
# IIIF 构造辅助
# --------------------------------------------------------------------------- #
def _label(text: str) -> dict:
    return {"zh": [text]}


def _image_service(base_url: str, image_slug: str) -> dict:
    return {
        "id": f"{base_url}/image-service/{image_slug}",
        "type": IMAGE_API_3,
        "profile": IMAGE_API_PROFILE,
    }


def _image_body(base_url: str, image_slug: str, label_text: str | None, width: int, height: int) -> dict:
    body = {
        "id": f"{base_url}/image-service/{image_slug}/full/full/0/default.jpg",
        "type": "Image",
        "format": "image/jpeg",
        "width": width,
        "height": height,
        "service": [_image_service(base_url, image_slug)],
        "thumbnail": [
            {
                "id": f"{base_url}/thumbs/{image_slug}.png",
                "type": "Image",
                "format": "image/png",
            }
        ],
    }
    if label_text is not None:
        body["label"] = _label(label_text)
    return body


def _painting_annotation(ann_id: str, body: dict) -> dict:
    return {
        "id": ann_id,
        "type": "Annotation",
        "motivation": "painting",
        "body": body,
        "target": ann_id.replace("/annotation/", "/canvas/"),
    }


def _canvas(canvas_id: str, label_text: str, width: int, height: int, annotations: list[dict]) -> dict:
    return {
        "id": canvas_id,
        "type": "Canvas",
        "label": _label(label_text),
        "width": width,
        "height": height,
        "items": [
            {
                "id": f"{canvas_id}/page/1",
                "type": "AnnotationPage",
                "items": annotations,
            }
        ],
    }


def _range(range_id: str, label_text: str, items: list[dict]) -> dict:
    return {"id": range_id, "type": "Range", "label": _label(label_text), "items": items}


def _canvas_ref(canvas_id: str) -> dict:
    return {"id": canvas_id, "type": "Canvas"}


def _range_ref(range_id: str) -> dict:
    return {"id": range_id, "type": "Range"}


def _manifest(manifest_id: str, label_text: str, canvases: list[dict], structures: list[dict],
              bad_context: bool = False) -> dict:
    return {
        "@context": "https://example.org/non-standard-context" if bad_context else IIIF_CONTEXT_3,
        "id": manifest_id,
        "type": "Manifest",
        "label": _label(label_text),
        "metadata": [
            {"label": _label("数据性质"), "value": _label("全部为虚构样例数据，不对应真实标本")},
        ],
        "items": canvases,
        "structures": structures,
    }


# --------------------------------------------------------------------------- #
# 三个场景
# --------------------------------------------------------------------------- #
def _scenario_demo_normal(base_url: str) -> dict:
    """正常场景：多 Canvas、多绘制标注、Choice、章节复用 Canvas、跨页共享图像。"""
    W, H = 400, 520
    img_fern = _image_body(base_url, "fern-001", "蕨类标本整株照", W, H)
    img_fern_detail = _image_body(base_url, "fern-001-detail", "蕨类孢子囊特写", W, H)
    img_beetle = _image_body(base_url, "beetle-014", "步甲标本背视图", W, H)
    img_beetle_ventral = _image_body(base_url, "beetle-014-ventral", "步甲标本腹视图", W, H)
    img_shared_label = _image_body(base_url, "shared-label-card", "馆藏标签卡（共用）", W, H)

    c1 = _canvas(
        f"{base_url}/manifest/demo-normal/canvas/c1", "第 1 页 · 蕨类标本", W, H,
        [
            _painting_annotation(f"{base_url}/manifest/demo-normal/annotation/a1", img_fern),
            _painting_annotation(f"{base_url}/manifest/demo-normal/annotation/a2", img_fern_detail),
        ],
    )
    # Choice：默认彩色照，候选为褪色历史照片（两候选各自独立图像 ID）
    img_choice_default = _image_body(base_url, "fern-001-color", "蕨类标准彩色照（默认）", W, H)
    img_choice_alt = _image_body(base_url, "fern-001-historical", "蕨类历史照片（候选）", W, H)
    choice_annotation = {
        "id": f"{base_url}/manifest/demo-normal/annotation/a3",
        "type": "Annotation",
        "motivation": "painting",
        "body": {
            "type": "Choice",
            "label": _label("蕨类图像版本选择"),
            "items": [img_choice_default, img_choice_alt],
        },
        "target": f"{base_url}/manifest/demo-normal/canvas/c2",
    }
    c2 = _canvas(
        f"{base_url}/manifest/demo-normal/canvas/c2", "第 2 页 · 蕨类图像版本", W, H, [choice_annotation]
    )
    c3 = _canvas(
        f"{base_url}/manifest/demo-normal/canvas/c3", "第 3 页 · 步甲标本", W, H,
        [
            _painting_annotation(f"{base_url}/manifest/demo-normal/annotation/a4", img_beetle),
            _painting_annotation(f"{base_url}/manifest/demo-normal/annotation/a5", img_beetle_ventral),
        ],
    )
    # 与第 1 页共用同一张标签卡图像：页面不得被合并
    c4 = _canvas(
        f"{base_url}/manifest/demo-normal/canvas/c4", "第 4 页 · 馆藏标签卡", W, H,
        [_painting_annotation(f"{base_url}/manifest/demo-normal/annotation/a6", img_shared_label)],
    )
    c1["items"][0]["items"].append(
        _painting_annotation(f"{base_url}/manifest/demo-normal/annotation/a7", img_shared_label)
    )

    structures = [
        # 第一章引用 c1、c2；第二章复用 c2（章节复用 Canvas）并引用 c3、c4
        _range(f"{base_url}/manifest/demo-normal/range/r1", "第一章 · 蕨类馆藏",
               [_canvas_ref(c1["id"]), _canvas_ref(c2["id"])]),
        _range(f"{base_url}/manifest/demo-normal/range/r2", "第二章 · 昆虫与标签",
               [_canvas_ref(c2["id"]), _canvas_ref(c3["id"]), _canvas_ref(c4["id"])]),
        _range(f"{base_url}/manifest/demo-normal/range/root", "标本册总目",
               [_range_ref(f"{base_url}/manifest/demo-normal/range/r1"),
                _range_ref(f"{base_url}/manifest/demo-normal/range/r2")]),
    ]
    return _manifest(f"{base_url}/manifest/demo-normal/manifest.json", "示例标本册 A（正常场景）",
                     [c1, c2, c3, c4], structures)


def _scenario_demo_boundary(base_url: str) -> dict:
    """边界场景：缺失图像描述、循环 Range、悬空引用、非标准 context、共享图像。"""
    W, H = 360, 480
    img_moss = _image_body(base_url, "moss-201", "苔藓标本照", W, H)
    img_no_label = _image_body(base_url, "moss-201-scan", None, W, H)  # 故意缺失 label
    img_shared = _image_body(base_url, "shared-scale-card", "比例尺卡（共用）", W, H)

    c1 = _canvas(
        f"{base_url}/manifest/demo-boundary/canvas/b1", "边界页 1 · 苔藓", W, H,
        [
            _painting_annotation(f"{base_url}/manifest/demo-boundary/annotation/n1", img_moss),
            _painting_annotation(f"{base_url}/manifest/demo-boundary/annotation/n2", img_no_label),
            _painting_annotation(f"{base_url}/manifest/demo-boundary/annotation/n3", img_shared),
        ],
    )
    c2 = _canvas(
        f"{base_url}/manifest/demo-boundary/canvas/b2", "边界页 2 · 比例尺", W, H,
        [_painting_annotation(f"{base_url}/manifest/demo-boundary/annotation/n4", img_shared)],
    )
    c3 = _canvas(
        f"{base_url}/manifest/demo-boundary/canvas/b3", "边界页 3 · 残损页", W, H,
        [
            # 非 painting 标注应被跳过
            {
                "id": f"{base_url}/manifest/demo-boundary/annotation/n5",
                "type": "Annotation",
                "motivation": "commenting",
                "body": {"type": "TextualBody", "value": "此页图像待补扫"},
                "target": f"{base_url}/manifest/demo-boundary/canvas/b3",
            }
        ],
    )

    r_loop_a = f"{base_url}/manifest/demo-boundary/range/loop-a"
    r_loop_b = f"{base_url}/manifest/demo-boundary/range/loop-b"
    structures = [
        # loop-a 与 loop-b 互相引用形成循环
        _range(r_loop_a, "循环章节 A",
               [_canvas_ref(c1["id"]), _range_ref(r_loop_b)]),
        _range(r_loop_b, "循环章节 B",
               [_canvas_ref(c2["id"]), _range_ref(r_loop_a)]),
        # 悬空 Range：引用不存在的 Canvas 与不存在的 Range
        _range(f"{base_url}/manifest/demo-boundary/range/dangling", "悬空章节",
               [
                   {"id": f"{base_url}/manifest/demo-boundary/canvas/ghost", "type": "Canvas"},
                   {"id": f"{base_url}/manifest/demo-boundary/range/ghost-range", "type": "Range"},
               ]),
    ]
    return _manifest(f"{base_url}/manifest/demo-boundary/manifest.json", "示例标本册 B（边界场景）",
                     [c1, c2, c3], structures, bad_context=True)


def _scenario_full_acceptance(base_url: str) -> dict:
    """完整验收：6 Canvas / 多图 / Choice / 共享图像 / 缺描述 / 层级与循环 Range。"""
    W, H = 420, 560
    bodies = {
        "rock": _image_body(base_url, "rock-301", "岩石切片单偏光", W, H),
        "rock_xpl": _image_body(base_url, "rock-301-xpl", "岩石切片正交偏光", W, H),
        "fish": _image_body(base_url, "fish-fossil-07", "鱼化石整体照", W, H),
        "fish_detail": _image_body(base_url, "fish-fossil-07-detail", "鱼化石头部细节", W, H),
        "fish_uv": _image_body(base_url, "fish-fossil-07-uv", "鱼化石紫外成像（候选）", W, H),
        "shell": _image_body(base_url, "shell-118", "贝壳标本照", W, H),
        "shell_nolabel": _image_body(base_url, "shell-118-ruler", None, W, H),
        "shared": _image_body(base_url, "shared-color-card", "色卡（跨页共用）", W, H),
    }

    def cid(slug: str) -> str:
        return f"{base_url}/manifest/full-acceptance/canvas/{slug}"

    def aid(slug: str) -> str:
        return f"{base_url}/manifest/full-acceptance/annotation/{slug}"

    c1 = _canvas(cid("f1"), "全验页 1 · 岩石切片", W, H, [
        _painting_annotation(aid("g1"), bodies["rock"]),
        _painting_annotation(aid("g2"), bodies["rock_xpl"]),
        _painting_annotation(aid("g3"), bodies["shared"]),
    ])
    c2 = _canvas(cid("f2"), "全验页 2 · 鱼化石（Choice）", W, H, [
        {
            "id": aid("g4"),
            "type": "Annotation",
            "motivation": "painting",
            "body": {
                "type": "Choice",
                "label": _label("鱼化石成像选择"),
                "items": [
                    _image_body(base_url, "fish-fossil-07-default", "鱼化石标准成像（默认）", W, H),
                    bodies["fish_uv"],
                ],
            },
            "target": cid("f2"),
        }
    ])
    c3 = _canvas(cid("f3"), "全验页 3 · 鱼化石细节", W, H, [
        _painting_annotation(aid("g5"), bodies["fish_detail"]),
        _painting_annotation(aid("g6"), bodies["shared"]),
    ])
    c4 = _canvas(cid("f4"), "全验页 4 · 贝壳标本", W, H, [
        _painting_annotation(aid("g7"), bodies["shell"]),
        _painting_annotation(aid("g8"), bodies["shell_nolabel"]),
    ])
    c5 = _canvas(cid("f5"), "全验页 5 · 无图像残页", W, H, [])
    c6 = _canvas(cid("f6"), "全验页 6 · 地质图版", W, H, [
        _painting_annotation(aid("g9"), bodies["rock"]),  # 与第 1 页共享图像
    ])

    def rid(slug: str) -> str:
        return f"{base_url}/manifest/full-acceptance/range/{slug}"

    structures = [
        _range(rid("geo"), "地质篇", [_canvas_ref(c1["id"]), _canvas_ref(c6["id"])]),
        _range(rid("paleo"), "古生物篇", [
            _canvas_ref(c2["id"]), _canvas_ref(c3["id"]), _canvas_ref(c4["id"])]),
        _range(rid("appendix"), "附录（复用地质篇页面）", [
            _canvas_ref(c6["id"]), _canvas_ref(c1["id"])]),
        _range(rid("root"), "完整验收总目", [_range_ref(rid("geo")), _range_ref(rid("paleo")),
                                            _range_ref(rid("appendix")), _range_ref(rid("loop-x"))]),
        # 自循环 Range
        _range(rid("loop-x"), "自循环章节", [_range_ref(rid("loop-x"))]),
    ]
    return _manifest(f"{base_url}/manifest/full-acceptance/manifest.json",
                     "示例标本册 C（完整验收场景）", [c1, c2, c3, c4, c5, c6], structures)


SCENARIOS = {
    "demo-normal": {
        "title": "正常场景：多 Canvas / 多标注 / Choice / 章节复用",
        "builder": _scenario_demo_normal,
        "images": [
            "fern-001", "fern-001-detail", "beetle-014", "beetle-014-ventral",
            "shared-label-card", "fern-001-color", "fern-001-historical",
        ],
    },
    "demo-boundary": {
        "title": "边界场景：缺描述 / 循环 Range / 悬空引用 / 非标准 context",
        "builder": _scenario_demo_boundary,
        "images": ["moss-201", "moss-201-scan", "shared-scale-card"],
    },
    "full-acceptance": {
        "title": "完整验收：6 Canvas / Choice / 共享图像 / 层级与循环 Range",
        "builder": _scenario_full_acceptance,
        "images": [
            "rock-301", "rock-301-xpl", "fish-fossil-07", "fish-fossil-07-detail",
            "fish-fossil-07-default", "fish-fossil-07-uv", "shell-118",
            "shell-118-ruler", "shared-color-card",
        ],
    },
}


def generate_site(out_dir: str | Path, base_url: str) -> dict:
    """在 out_dir 下生成完整样例源站，返回场景清单元数据。"""
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    index: dict = {"base_url": base_url, "scenarios": {}}

    for slug, spec in SCENARIOS.items():
        manifest = spec["builder"](base_url)
        manifest_dir = root / "manifest" / slug
        manifest_dir.mkdir(parents=True, exist_ok=True)
        (manifest_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        for i, image_slug in enumerate(spec["images"]):
            width, height = 400, 520
            # 本地 IIIF Image API 3 level0 静态服务样例
            info = {
                "@context": "http://iiif.io/api/image/3/context.json",
                "id": f"{base_url}/image-service/{image_slug}",
                "type": IMAGE_API_3,
                "profile": IMAGE_API_PROFILE,
                "protocol": "http://iiif.io/api/image",
                "width": width,
                "height": height,
                "tiles": [{"width": 256, "height": 256, "scaleFactors": [1, 2]}],
                "extraQualities": ["color"],
            }
            svc_dir = root / "image-service" / image_slug
            svc_dir.mkdir(parents=True, exist_ok=True)
            (svc_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
            # 全尺寸样例图（静态文件，不实现动态图像编辑参数）
            full_dir = svc_dir / "full" / "full" / "0"
            full_dir.mkdir(parents=True, exist_ok=True)
            write_png(full_dir / "default.jpg", width, height, i)
            # 缩略图
            thumb_dir = root / "thumbs"
            thumb_dir.mkdir(parents=True, exist_ok=True)
            write_png(thumb_dir / f"{image_slug}.png", 96, 128, i + 1)

        index["scenarios"][slug] = {
            "title": spec["title"],
            "manifest_url": f"{base_url}/manifest/{slug}/manifest.json",
            "manifest_path": str(manifest_dir / "manifest.json"),
            "image_count": len(spec["images"]),
        }

    (root / "site-index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return index


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="生成 IIIF 本地样例源站（虚构数据）")
    parser.add_argument("--out", default="sample_site/generated", help="输出目录")
    parser.add_argument("--base-url", default="http://127.0.0.1:5000/sample", help="清单 ID 使用的基础 URL")
    args = parser.parse_args()
    meta = generate_site(args.out, args.base_url.rstrip("/"))
    print(json.dumps(meta, ensure_ascii=False, indent=2))
