"""IIIF Presentation API 3.0 子集解析器。

支持的协议子集（见 README「支持的协议子集」）：
- 顶层 Manifest（@context / id / type=Manifest / label / items / structures）
- Canvas（id/type/label/width/height/items，items 为 AnnotationPage）
- Painting Annotation（motivation=painting，body 为 Image 资源）
- Choice 资源（type=Choice，items 中全部候选图像均采集，首个为默认）
- Range（type=Range，items 中 Canvas / 具体资源 / Range 三类引用，支持嵌套）
- 图像 service 描述（仅记录 ImageService3 的 id/type/profile，不做服务端解引用）

明确的设计约束：
- Canvas 标识、图像（body）标识、Range 章节引用分属三类 ID，绝不混用；
- 同一图像 ID 出现在多个 Canvas 时各自保留为独立页面的图像，不合并页面；
- 无法解析的部分记录带 JSON 路径的 Issue，其余页面继续采集。
"""
from __future__ import annotations

from .models import CanvasPage, ImageDescription, Issue, RangeNode

IIIF_CONTEXT_3 = "http://iiif.io/api/presentation/3/context.json"


def _label_of(node: dict) -> str | None:
    """Presentation 3 的 label 为语言映射，取第一个可读值。"""
    label = node.get("label")
    if isinstance(label, dict):
        for values in label.values():
            if isinstance(values, list) and values:
                return str(values[0])
        return None
    if isinstance(label, str) and label.strip():
        return label
    return None


def _first_service(body: dict) -> tuple[str | None, str | None, str | None]:
    services = body.get("service")
    if isinstance(services, list) and services:
        svc = services[0]
    elif isinstance(services, dict):
        svc = services
    else:
        return None, None, None
    if not isinstance(svc, dict):
        return None, None, None
    profile = svc.get("profile")
    if isinstance(profile, list):
        profile = profile[0] if profile else None
    return svc.get("id"), svc.get("type"), profile


def _thumbnail_url(body: dict) -> str | None:
    thumbs = body.get("thumbnail")
    if isinstance(thumbs, list) and thumbs and isinstance(thumbs[0], dict):
        return thumbs[0].get("id")
    if isinstance(thumbs, dict):
        return thumbs.get("id")
    return None


def _build_image(body: dict, source_path: str, is_alternative: bool) -> tuple[ImageDescription | None, list[Issue]]:
    issues: list[Issue] = []
    if not isinstance(body, dict):
        issues.append(Issue("INVALID_IMAGE_BODY", "error", "图像 body 不是 JSON 对象", source_path))
        return None, issues
    if body.get("type") != "Image":
        issues.append(
            Issue(
                "UNSUPPORTED_BODY_TYPE",
                "warning",
                f"仅采集 Image 类型 body，遇到 {body.get('type')!r} 已跳过",
                source_path,
            )
        )
        return None, issues

    image_id = body.get("id")
    if not image_id or not isinstance(image_id, str):
        issues.append(Issue("MISSING_IMAGE_ID", "error", "图像资源缺少 id 标识", source_path))
        image_id = None

    label = _label_of(body)
    if label is None:
        # 验收点：缺失图像描述
        issues.append(Issue("MISSING_IMAGE_LABEL", "warning", "图像资源缺少 label 描述", source_path))

    svc_id, svc_type, svc_profile = _first_service(body)
    width = body.get("width") if isinstance(body.get("width"), int) else None
    height = body.get("height") if isinstance(body.get("height"), int) else None

    img = ImageDescription(
        image_id=image_id,
        type="Image",
        label=label,
        format=body.get("format") if isinstance(body.get("format"), str) else None,
        width=width,
        height=height,
        service_id=svc_id,
        service_type=svc_type,
        service_profile=svc_profile,
        thumbnail_url=_thumbnail_url(body),
        is_choice_alternative=is_alternative,
        source_path=source_path,
        issues=issues,
    )
    return img, []


def _parse_annotation(annotation: dict, ann_path: str) -> tuple[list[ImageDescription], list[Issue]]:
    images: list[ImageDescription] = []
    issues: list[Issue] = []

    motivation = annotation.get("motivation")
    if motivation != "painting":
        # 非绘制标注（如评论、链接）不属于图像采集范围
        issues.append(
            Issue(
                "SKIP_NON_PAINTING",
                "warning",
                f"非 painting 标注（motivation={motivation!r}）已跳过",
                ann_path,
            )
        )
        return images, issues

    body = annotation.get("body")
    if not isinstance(body, dict):
        issues.append(Issue("MISSING_BODY", "error", "painting 标注缺少 body", ann_path))
        return images, issues

    if body.get("type") == "Choice":
        choice_path = f"{ann_path}/body"
        items = body.get("items")
        if not isinstance(items, list) or not items:
            issues.append(Issue("EMPTY_CHOICE", "error", "Choice 资源没有候选 items", choice_path))
            return images, issues
        for idx, candidate in enumerate(items):
            img, img_issues = _build_image(candidate, f"{choice_path}/items/{idx}", idx > 0)
            issues.extend(img_issues)
            if img is not None:
                images.append(img)
                issues.extend(img.issues)  # 图像级问题（如缺描述）同步到清单总问题
        return images, issues

    img, img_issues = _build_image(body, f"{ann_path}/body", False)
    issues.extend(img_issues)
    if img is not None:
        images.append(img)
        issues.extend(img.issues)
    return images, issues


def _parse_canvas(canvas: dict, canvas_idx: int) -> tuple[CanvasPage | None, list[Issue]]:
    issues: list[Issue] = []
    canvas_path = f"/items/{canvas_idx}"
    if not isinstance(canvas, dict):
        issues.append(Issue("INVALID_CANVAS", "error", "Canvas 不是 JSON 对象", canvas_path))
        return None, issues
    if canvas.get("type") != "Canvas":
        issues.append(
            Issue("UNSUPPORTED_ITEM_TYPE", "warning", f"items 中出现非 Canvas 类型 {canvas.get('type')!r}，已跳过", canvas_path)
        )
        return None, issues

    canvas_id = canvas.get("id")
    if not isinstance(canvas_id, str) or not canvas_id:
        issues.append(Issue("MISSING_CANVAS_ID", "error", "Canvas 缺少 id 标识", canvas_path))
        canvas_id = f"__unknown_canvas_{canvas_idx}__"

    page = CanvasPage(
        canvas_id=canvas_id,
        label=_label_of(canvas),
        width=canvas.get("width") if isinstance(canvas.get("width"), int) else None,
        height=canvas.get("height") if isinstance(canvas.get("height"), int) else None,
        source_path=canvas_path,
    )

    pages = canvas.get("items")
    if not isinstance(pages, list):
        issues.append(Issue("NO_ANNOTATION_PAGES", "warning", "Canvas 没有 AnnotationPage（items）", canvas_path))
        page.issues.extend(issues)
        return page, issues

    for p_idx, ann_page in enumerate(pages):
        page_path = f"{canvas_path}/items/{p_idx}"
        if not isinstance(ann_page, dict) or ann_page.get("type") != "AnnotationPage":
            issues.append(Issue("INVALID_ANNOTATION_PAGE", "warning", "AnnotationPage 结构无效，已跳过", page_path))
            continue
        annotations = ann_page.get("items")
        if not isinstance(annotations, list):
            issues.append(Issue("NO_ANNOTATIONS", "warning", "AnnotationPage 没有标注 items", page_path))
            continue
        for a_idx, annotation in enumerate(annotations):
            ann_path = f"{page_path}/items/{a_idx}"
            if not isinstance(annotation, dict):
                issues.append(Issue("INVALID_ANNOTATION", "error", "Annotation 不是 JSON 对象", ann_path))
                continue
            imgs, ann_issues = _parse_annotation(annotation, ann_path)
            page.images.extend(imgs)
            issues.extend(ann_issues)

    if not page.images:
        issues.append(Issue("CANVAS_WITHOUT_IMAGE", "warning", "Canvas 未解析到任何图像资源", canvas_path))
    page.issues.extend(issues)
    return page, issues


def _collect_range_refs(
    range_node: dict,
    range_path: str,
    canvas_index: dict[str, CanvasPage],
    range_index: dict[str, RangeNode],
) -> tuple[list[str], list[str], list[Issue]]:
    """把一个 Range 的 items 拆成 Canvas 引用与 Range 引用，悬空引用报错。"""
    canvas_refs: list[str] = []
    range_refs: list[str] = []
    issues: list[Issue] = []
    items = range_node.get("items")
    if not isinstance(items, list):
        return canvas_refs, range_refs, issues

    for idx, item in enumerate(items):
        item_path = f"{range_path}/items/{idx}"
        if not isinstance(item, dict):
            issues.append(Issue("INVALID_RANGE_ITEM", "error", "Range 条目不是 JSON 对象", item_path))
            continue
        item_type = item.get("type")
        item_id = item.get("id")
        if item_type == "Canvas" or (
            item_type is None and isinstance(item_id, str) and item_id in canvas_index
        ):
            if not isinstance(item_id, str):
                issues.append(Issue("MISSING_CANVAS_ID", "error", "Range 中的 Canvas 引用缺少 id", item_path))
                continue
            if item_id not in canvas_index:
                issues.append(
                    Issue(
                        "DANGLING_CANVAS_REF",
                        "error",
                        "Range 引用了清单中不存在的 Canvas",
                        item_path,
                        ref=item_id,
                    )
                )
                continue
            if item_id not in canvas_refs:
                canvas_refs.append(item_id)
        elif item_type == "Range" or (
            item_type is None and isinstance(item_id, str) and item_id in range_index
        ):
            if not isinstance(item_id, str):
                issues.append(Issue("MISSING_RANGE_ID", "error", "Range 中的子 Range 引用缺少 id", item_path))
                continue
            if item_id not in range_index:
                issues.append(
                    Issue(
                        "DANGLING_RANGE_REF",
                        "error",
                        "Range 引用了 structures 中不存在的 Range",
                        item_path,
                        ref=item_id,
                    )
                )
                continue
            if item_id not in range_refs:
                range_refs.append(item_id)
        elif item_type in ("Annotation", "SpecificResource"):
            # 具体资源引用：解析其 target/source 指向的 Canvas
            target = item.get("source") or item.get("target")
            target_id = target.get("id") if isinstance(target, dict) else target
            if isinstance(target_id, str) and target_id in canvas_index:
                if target_id not in canvas_refs:
                    canvas_refs.append(target_id)
            else:
                issues.append(
                    Issue(
                        "DANGLING_CANVAS_REF",
                        "error",
                        f"{item_type} 引用无法解析到 Canvas",
                        item_path,
                        ref=target_id if isinstance(target_id, str) else None,
                    )
                )
        else:
            issues.append(
                Issue(
                    "UNSUPPORTED_RANGE_ITEM",
                    "warning",
                    f"Range 中不支持的条目类型 {item_type!r}，已跳过",
                    item_path,
                )
            )
    return canvas_refs, range_refs, issues


def parse_manifest(manifest: dict) -> tuple[list[CanvasPage], list[RangeNode], list[Issue], dict]:
    issues: list[Issue] = []

    context = manifest.get("@context")
    contexts = context if isinstance(context, list) else [context]
    if IIIF_CONTEXT_3 not in contexts:
        issues.append(
            Issue(
                "UNEXPECTED_CONTEXT",
                "warning",
                f"@context 未声明 IIIF Presentation 3（{IIIF_CONTEXT_3}），仍按 3.0 子集尝试解析",
                "/@context",
            )
        )

    manifest_type = manifest.get("type")
    if manifest_type != "Manifest":
        issues.append(
            Issue("UNEXPECTED_TYPE", "warning", f"顶层 type 为 {manifest_type!r}，预期 Manifest", "/type")
        )

    # ---------- Canvas / 图像采集 ----------
    raw_items = manifest.get("items")
    canvases: list[CanvasPage] = []
    if not isinstance(raw_items, list):
        issues.append(Issue("NO_CANVASES", "error", "Manifest 缺少 items（Canvas 列表）", "/items"))
        raw_items = []

    seen_canvas_ids: set[str] = set()
    image_id_to_canvas: dict[str, set[str]] = {}

    for idx, raw_canvas in enumerate(raw_items):
        page, canvas_issues = _parse_canvas(raw_canvas, idx)
        issues.extend(canvas_issues)
        if page is None:
            continue
        if page.canvas_id in seen_canvas_ids:
            issues.append(
                Issue(
                    "DUPLICATE_CANVAS_ID",
                    "error",
                    "出现重复 Canvas id，后一个已跳过",
                    page.source_path,
                    ref=page.canvas_id,
                )
            )
            continue
        seen_canvas_ids.add(page.canvas_id)
        canvases.append(page)
        for img in page.images:
            if img.image_id:
                image_id_to_canvas.setdefault(img.image_id, set()).add(page.canvas_id)

    # 同一图像用于多处：合法，不合并页面；仅给出可追溯提示
    for image_id, canvas_ids in image_id_to_canvas.items():
        if len(canvas_ids) > 1:
            issues.append(
                Issue(
                    "SHARED_IMAGE_ID",
                    "warning",
                    f"同一图像标识被 {len(canvas_ids)} 个 Canvas 引用，页面各自保留、不做合并",
                    "/items",
                    ref=image_id,
                )
            )

    canvas_index = {c.canvas_id: c for c in canvases}

    # ---------- Range / 章节采集 ----------
    raw_structures = manifest.get("structures")
    ranges: list[RangeNode] = []
    range_index: dict[str, RangeNode] = {}
    if not isinstance(raw_structures, list):
        raw_structures = []

    seen_range_ids: set[str] = set()
    for r_idx, raw_range in enumerate(raw_structures):
        range_path = f"/structures/{r_idx}"
        if not isinstance(raw_range, dict) or raw_range.get("type") != "Range":
            issues.append(Issue("INVALID_STRUCTURE", "warning", "structures 中存在非 Range 条目，已跳过", range_path))
            continue
        range_id = raw_range.get("id")
        if not isinstance(range_id, str) or not range_id:
            issues.append(Issue("MISSING_RANGE_ID", "error", "Range 缺少 id 标识", range_path))
            continue
        if range_id in seen_range_ids:
            issues.append(
                Issue("DUPLICATE_RANGE_ID", "error", "出现重复 Range id，后一个已跳过", range_path, ref=range_id)
            )
            continue
        seen_range_ids.add(range_id)
        node = RangeNode(range_id=range_id, label=_label_of(raw_range), source_path=range_path)
        ranges.append(node)
        range_index[range_id] = node

    # 解析引用（此时 range_index 已完整，可检测悬空与循环）
    for r_idx, raw_range in enumerate(raw_structures):
        if not isinstance(raw_range, dict) or raw_range.get("type") != "Range":
            continue
        range_id = raw_range.get("id")
        node = range_index.get(range_id)
        if node is None:
            continue
        canvas_refs, child_refs, ref_issues = _collect_range_refs(
            raw_range, node.source_path, canvas_index, range_index
        )
        node.referenced_canvas_ids = canvas_refs
        node.child_range_ids = child_refs
        node.issues.extend(ref_issues)
        issues.extend(ref_issues)

    # 父链接回填
    for node in ranges:
        for child_id in node.child_range_ids:
            child = range_index.get(child_id)
            if child is not None and node.range_id not in child.parent_range_ids:
                child.parent_range_ids.append(node.range_id)

    # 循环 Range 检测：DFS，按路径栈识别回边；回边只标记、不展开，避免死循环
    state: dict[str, int] = {}  # 0=未访问 1=在栈中 2=完成

    def dfs(node_id: str, stack: list[str]) -> None:
        state[node_id] = 1
        stack.append(node_id)
        node = range_index[node_id]
        for child_id in node.child_range_ids:
            child = range_index.get(child_id)
            if child is None:
                continue
            if state.get(child_id, 0) == 1:
                cycle_path = " -> ".join(stack[stack.index(child_id):] + [child_id])
                node.cyclic = True
                child.cyclic = True
                issue = Issue(
                    "CYCLIC_RANGE_REF",
                    "error",
                    f"检测到 Range 循环引用：{cycle_path}，回边已跳过",
                    node.source_path,
                    ref=child_id,
                )
                node.issues.append(issue)
                issues.append(issue)
            elif state.get(child_id, 0) == 0:
                dfs(child_id, stack)
        stack.pop()
        state[node_id] = 2

    for node in ranges:
        if state.get(node.range_id, 0) == 0:
            dfs(node.range_id, [])

    total_images = sum(len(c.images) for c in canvases)
    missing_labels = sum(
        1 for c in canvases for img in c.images if any(i.code == "MISSING_IMAGE_LABEL" for i in img.issues)
    )
    choice_images = sum(1 for c in canvases for img in c.images if img.is_choice_alternative)
    stats = {
        "canvas_count": len(canvases),
        "image_count": total_images,
        "range_count": len(ranges),
        "missing_image_label_count": missing_labels,
        "choice_alternative_count": choice_images,
        "shared_image_id_count": sum(1 for ids in image_id_to_canvas.values() if len(ids) > 1),
        "cyclic_range_count": sum(1 for r in ranges if r.cyclic),
        "error_count": sum(1 for i in issues if i.severity == "error"),
        "warning_count": sum(1 for i in issues if i.severity == "warning"),
    }
    return canvases, ranges, issues, stats
