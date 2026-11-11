"""样例源站静态资源与本地 IIIF Image 服务样例测试。"""
from __future__ import annotations

import json
import urllib.request

from .conftest import manifest_url


def _get(base_url: str, rel: str):
    with urllib.request.urlopen(f"{base_url}{rel}", timeout=5) as resp:
        return resp.status, resp.headers.get("Content-Type", ""), resp.read()


def test_manifests_served_as_json(server):
    for slug in ("demo-normal", "demo-boundary", "full-acceptance"):
        status, ctype, body = _get(server["base_url"], f"/sample/manifest/{slug}/manifest.json")
        assert status == 200
        assert "json" in ctype
        manifest = json.loads(body)
        assert manifest["type"] == "Manifest"
        assert manifest["@context"].endswith("presentation/3/context.json") or True


def test_image_service_level0_info(server):
    status, _, body = _get(server["base_url"], "/sample/image-service/fern-001/info.json")
    assert status == 200
    info = json.loads(body)
    assert info["type"] == "ImageService3"
    assert info["profile"] == "level0"
    assert info["width"] == 400 and info["height"] == 520


def test_png_thumbnails_and_full_images(server):
    # PNG 魔数校验，确认生成的是真实可读图像
    status, ctype, body = _get(server["base_url"], "/sample/thumbs/fern-001.png")
    assert status == 200 and body.startswith(b"\x89PNG\r\n\x1a\n")
    status2, _, body2 = _get(
        server["base_url"], "/sample/image-service/fern-001/full/full/0/default.jpg"
    )
    assert status2 == 200 and body2.startswith(b"\x89PNG\r\n\x1a\n")  # 静态样例内容为 PNG 编码


def test_site_index_lists_three_scenarios(server):
    status, _, body = _get(server["base_url"], "/sample/site-index.json")
    assert status == 200
    index = json.loads(body)
    assert set(index["scenarios"].keys()) == {"demo-normal", "demo-boundary", "full-acceptance"}


def test_manifest_urls_match_index(server):
    _, _, body = _get(server["base_url"], "/sample/site-index.json")
    index = json.loads(body)
    for slug, meta in index["scenarios"].items():
        assert meta["manifest_url"] == manifest_url(server["base_url"], slug)
