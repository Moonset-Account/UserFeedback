"""独立预期真值（仅测试使用）。

本文件由人工依据样例清单的设计意图编写，采集器（iiif_collector/）
与 Web 应用（webapp/）任何代码路径都不得 import 本模块。
测试通过 BASE 占位符隔离端口差异，保证真值与采集结果相互独立。
"""

BASE = "{BASE}"  # 测试运行时替换为实际源站基础 URL

EXPECTED = {
    "demo-normal": {
        "manifest_label": "示例标本册 A（正常场景）",
        "status": "ok",
        "canvas_count": 4,
        "image_count": 8,  # c1=3（含共用标签卡） c2=2（Choice） c3=2 c4=1
        "range_count": 3,
        "missing_image_label_count": 0,
        "choice_alternative_count": 1,
        "shared_image_id_count": 1,  # shared-label-card 同时属于 c1 与 c4
        "cyclic_range_count": 0,
        "error_count": 0,
        # Canvas 标识与图像标识严格区分
        "canvas_ids": [
            f"{BASE}/sample/manifest/demo-normal/canvas/c1",
            f"{BASE}/sample/manifest/demo-normal/canvas/c2",
            f"{BASE}/sample/manifest/demo-normal/canvas/c3",
            f"{BASE}/sample/manifest/demo-normal/canvas/c4",
        ],
        # 每个 Canvas 的图像数量（同一图像多处使用时页面不合并）
        "images_per_canvas": [3, 2, 2, 1],
        # 章节复用 Canvas：r1 与 r2 都引用 c2
        "range_canvas_refs": {
            f"{BASE}/sample/manifest/demo-normal/range/r1": [
                f"{BASE}/sample/manifest/demo-normal/canvas/c1",
                f"{BASE}/sample/manifest/demo-normal/canvas/c2",
            ],
            f"{BASE}/sample/manifest/demo-normal/range/r2": [
                f"{BASE}/sample/manifest/demo-normal/canvas/c2",
                f"{BASE}/sample/manifest/demo-normal/canvas/c3",
                f"{BASE}/sample/manifest/demo-normal/canvas/c4",
            ],
        },
        "root_range_children": [
            f"{BASE}/sample/manifest/demo-normal/range/r1",
            f"{BASE}/sample/manifest/demo-normal/range/r2",
        ],
        # 共享图像出现在两个不同 Canvas 上，且各自保留
        "shared_image_id": f"{BASE}/sample/image-service/shared-label-card/full/full/0/default.jpg",
        "shared_image_canvases": [
            f"{BASE}/sample/manifest/demo-normal/canvas/c1",
            f"{BASE}/sample/manifest/demo-normal/canvas/c4",
        ],
        "issue_codes": [],
    },
    "demo-boundary": {
        "manifest_label": "示例标本册 B（边界场景）",
        "status": "partial",
        "canvas_count": 3,
        "image_count": 4,  # b1=3 b2=1 b3=0（commenting 被跳过）
        "range_count": 3,
        "missing_image_label_count": 1,  # moss-201-scan 无 label
        "choice_alternative_count": 0,
        "shared_image_id_count": 1,  # shared-scale-card 属于 b1、b2
        "cyclic_range_count": 2,  # loop-a / loop-b
        # 错误：循环 2 + 悬空 Canvas 1 + 悬空 Range 1；警告：非标准 context、跳过 commenting、缺描述、无图像页、共享图像
        "error_code_set": {"CYCLIC_RANGE_REF", "DANGLING_CANVAS_REF", "DANGLING_RANGE_REF"},
        "warning_code_set": {
            "UNEXPECTED_CONTEXT", "SKIP_NON_PAINTING", "MISSING_IMAGE_LABEL",
            "CANVAS_WITHOUT_IMAGE", "SHARED_IMAGE_ID",
        },
        "cyclic_range_ids": [
            f"{BASE}/sample/manifest/demo-boundary/range/loop-a",
            f"{BASE}/sample/manifest/demo-boundary/range/loop-b",
        ],
        "dangling_canvas_ref": f"{BASE}/sample/manifest/demo-boundary/canvas/ghost",
        "dangling_range_ref": f"{BASE}/sample/manifest/demo-boundary/range/ghost-range",
        "missing_label_image_id": f"{BASE}/sample/image-service/moss-201-scan/full/full/0/default.jpg",
        "empty_canvas_id": f"{BASE}/sample/manifest/demo-boundary/canvas/b3",
        # 即使存在循环与悬空引用，可用页面仍完整保留
        "usable_canvas_ids": [
            f"{BASE}/sample/manifest/demo-boundary/canvas/b1",
            f"{BASE}/sample/manifest/demo-boundary/canvas/b2",
            f"{BASE}/sample/manifest/demo-boundary/canvas/b3",
        ],
    },
    "full-acceptance": {
        "manifest_label": "示例标本册 C（完整验收场景）",
        "status": "partial",  # 含自循环 Range
        "canvas_count": 6,
        "image_count": 10,  # f1=3 f2=2(Choice) f3=2 f4=2 f5=0 f6=1
        "range_count": 5,
        "missing_image_label_count": 1,  # shell-118-ruler
        "choice_alternative_count": 1,
        "shared_image_id_count": 2,  # shared-color-card（f1/f3）、rock-301（f1/f6）
        "cyclic_range_count": 1,  # loop-x 自循环
        "images_per_canvas": [3, 2, 2, 2, 0, 1],
        "self_cycle_range_id": f"{BASE}/sample/manifest/full-acceptance/range/loop-x",
        "root_range_id": f"{BASE}/sample/manifest/full-acceptance/range/root",
        "root_children_count": 4,
        "empty_canvas_id": f"{BASE}/sample/manifest/full-acceptance/canvas/f5",
    },
}


def render(scenario: str, base_url: str) -> dict:
    """返回把 BASE 占位符替换为实际源站地址后的真值副本（支持集合等结构）。"""
    import copy

    def _walk(node):
        if isinstance(node, str):
            return node.replace(BASE, base_url)
        if isinstance(node, dict):
            return {_walk(k): _walk(v) for k, v in node.items()}
        if isinstance(node, (list, tuple, set)):
            return type(node)(_walk(v) for v in node)
        return node

    return _walk(copy.deepcopy(EXPECTED[scenario]))
