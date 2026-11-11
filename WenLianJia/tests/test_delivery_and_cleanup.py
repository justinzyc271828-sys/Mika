"""Delivery manifests, acceptance truth and two-phase cleanup tests."""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf
import pytest
from PIL import Image

from core.cleanup_manager import CleanupError, CleanupManager
from core.product_manager import ProductManager
from models.schemas import Product, Subject


def _pdf(path: Path, pages: int = 1) -> Path:
    document = pymupdf.open()
    for index in range(pages):
        page = document.new_page(width=595, height=842)
        page.insert_text((72, 100), f"Test page {index + 1}", fontsize=18)
    document.save(path)
    document.close()
    return path


def _images(folder: Path, count: int = 7) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    for index in range(count):
        path = folder / f"{index + 1:02d}_test.png"
        Image.new("RGB", (120, 160), (16, 42, 67)).save(path)
        files.append(path)
    return files


def _product(app) -> Product:
    product = Product(sku="TST001", title="交付记录测试", subject=Subject.MATHEMATICS)
    app.db.add_product(product)
    return product


def test_delivery_records_hash_real_files_and_never_fake_cloud(app, tmp_path: Path) -> None:
    product = _product(app)
    full = _pdf(tmp_path / "full.pdf", pages=2)
    quick = _pdf(tmp_path / "quick.pdf", pages=1)
    images = _images(tmp_path / "images")
    result = ProductManager(app.settings, app.db, app.paths).write_all(
        product, full, quick, images
    )
    assert result.info_json.is_file()
    assert result.info_markdown.is_file()
    assert result.file_list.is_file()
    assert result.delivery_text.is_file()
    assert result.acceptance.is_file()
    assert len(result.files) == 9

    manifest = json.loads(result.file_list.read_text(encoding="utf-8"))
    assert len(manifest["files"]) == 9
    assert all(item["sha256"] and item["size"] > 0 for item in manifest["files"])
    assert any(item["kind"] == "pdf_full" for item in manifest["files"])
    assert sum(item["kind"] == "product_image" for item in manifest["files"]) == 7

    acceptance = json.loads(result.acceptance.read_text(encoding="utf-8"))
    assert acceptance["overall"] == "pending"
    cloud = [item for item in acceptance["checks"] if "上传" in item["name"] or "分享" in item["name"]]
    assert cloud
    assert all(item["status"] == "pending" for item in cloud)


def test_cleanup_refuses_until_cloud_and_share_are_verified(app, tmp_path: Path) -> None:
    product = _product(app)
    full = _pdf(tmp_path / "full.pdf")
    quick = _pdf(tmp_path / "quick.pdf")
    ProductManager(app.settings, app.db, app.paths).write_all(
        product, full, quick, _images(tmp_path / "images")
    )
    result = CleanupManager(app.settings, app.db, app.paths).evaluate(product)
    assert result.eligible is False
    assert any("云端上传" in blocker for blocker in result.blockers)
    assert any("分享" in blocker for blocker in result.blockers)


def test_purge_requires_exact_confirmation_and_stays_under_staging(app) -> None:
    manager = CleanupManager(app.settings, app.db, app.paths)
    staging = manager.staging_root / "TST001_20260101_000000"
    staging.mkdir(parents=True)
    (staging / "cleanup_manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(CleanupError, match="确认删除"):
        manager.purge(staging, confirmation="删除")
    assert staging.is_dir()
    result = manager.purge(staging, confirmation="确认删除")
    assert result["files"] == 1
    assert not staging.exists()


def test_purge_rejects_path_outside_staging(app, tmp_path: Path) -> None:
    manager = CleanupManager(app.settings, app.db, app.paths)
    outside = tmp_path / "not-staging"
    outside.mkdir()
    with pytest.raises(CleanupError, match="待清理目录之外"):
        manager.purge(outside, confirmation="确认删除")
