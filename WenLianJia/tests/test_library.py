"""Chapter 26 product-library acceptance tests.

Covers the four-level hierarchy, the nine-state SKU lifecycle, shared
SHA-256 assets, versions, template inheritance, batch import/export,
backup/restore and 100-book / 300-SKU pagination.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.library.assets import AssetLibrary
from core.library.backup import BackupManager
from core.library.batch import BatchEngine, read_table, write_table
from core.library.service import LibraryService
from core.library.templates import DEFAULT_FAMILY_CODE, resolve_template, validate_overrides
from models.schemas import (
    BookVersionStatus,
    DeliveryStatus,
    Product,
    SkuItem,
    SkuLifecycle,
)


@pytest.fixture
def library(app) -> LibraryService:
    service = LibraryService(app.paths, app.settings, app.db)
    try:
        yield service
    finally:
        service.engine.dispose()


def make_product(app, book, label: str = "上册", code: str = "U", status: str = "completed"):
    product = Product(
        book_id=book.id,
        volume_label=label,
        volume_number=1,
        volume_code=code,
        title=f"{book.title} {label}",
        subject=book.subject,
        status=status,
        total_pages=120,
        exercise_count=40,
        solved_count=40,
        verified_count=40,
    )
    app.db.add_product(product)
    return product


def make_book(service, code: str = "T-001", title: str = "线性代数", **kwargs):
    return service.create_book(code=code, title=title, **kwargs)


def test_default_family_and_template_inheritance(library):
    family = library.ensure_default_family()
    assert family.code == DEFAULT_FAMILY_CODE
    assert family.name == "闲鱼教辅"
    assert library.default_family().id == family.id

    book = make_book(
        library,
        template_overrides={"watermark": {"opacity": 0.15}, "pricing": {"round_to": 50}},
    )
    resolved = resolve_template(family, book.template_overrides)
    assert resolved.get("watermark.opacity") == 0.15
    assert resolved.source_of("watermark.opacity") == "override"
    assert resolved.get("watermark.text") == "全网同名爱做题的大肥鱼"
    assert resolved.source_of("watermark.text") == "family"

    default = resolve_template(family, {})
    assert default.get("watermark.opacity") == 0.18
    assert validate_overrides({"watermark": {"opacity": 2}})

    with pytest.raises(Exception):
        library.create_book(code="T-BAD", title="坏模板", template_overrides={"watermark": {"opacity": 9}})


def test_four_level_hierarchy_versions_and_lifecycle(library, app):
    family = library.ensure_default_family()
    book = make_book(library)
    assert book.family_id == family.id
    product = make_product(app, book)

    version = library.create_version(
        book_id=book.id,
        version_label="v1.0",
        product_id=product.id,
        status=BookVersionStatus.ACTIVE,
    )
    sku = library.create_single_sku(
        book_id=book.id,
        product_id=product.id,
        name="线性代数上册 习题解析",
        price="19.90",
        original_price="29.90",
        version_id=version.id,
    )
    assert sku.kind == "single"
    assert sku.lifecycle == SkuLifecycle.DRAFT
    assert sku.version_id == version.id
    assert sku.primary_product_id == product.id
    assert sku.items[0].product_id == product.id

    library.transition_lifecycle(sku.id, SkuLifecycle.PENDING_PRODUCTION)
    library.transition_lifecycle(sku.id, SkuLifecycle.PRODUCING)
    library.transition_lifecycle(sku.id, SkuLifecycle.REVIEW_REQUIRED)
    ready = library.transition_lifecycle(sku.id, SkuLifecycle.READY)
    assert ready.lifecycle == SkuLifecycle.READY

    with pytest.raises(Exception):
        library.transition_lifecycle(sku.id, SkuLifecycle.DRAFT)

    library.set_delivery_status(sku.id, DeliveryStatus.UPLOADED)
    assert library.get_sku(sku.id).delivery_status == DeliveryStatus.UPLOADED

    with pytest.raises(Exception):
        library.create_single_sku(
            book_id=book.id,
            product_id=product.id,
            name="重复编号",
            price="10",
            sku_code=sku.sku_code,
        )


def test_bundle_reuses_products_and_respects_baseline(library, app):
    book = make_book(library, code="T-BUNDLE", title="高等数学", volumes=2)
    upper = make_product(app, book, "上册", "U")
    lower = make_product(app, book, "下册", "L")
    first = library.create_single_sku(
        book_id=book.id, product_id=upper.id, name="上册", price="19.90", sku_code="B-U"
    )
    second = library.create_single_sku(
        book_id=book.id, product_id=lower.id, name="下册", price="20.00", sku_code="B-L"
    )
    bundle = library.create_bundle(
        book_id=book.id,
        items=[
            SkuItem(product_id=upper.id, volume_label="上册"),
            SkuItem(product_id=lower.id, volume_label="下册"),
        ],
        name="上下册套装",
        price="35.00",
        sku_code="B-SET",
    )
    assert bundle.kind == "bundle"
    assert {item.product_id for item in bundle.items} == {upper.id, lower.id}
    assert len(library.db.list_productions(book.id)) == 2

    with pytest.raises(Exception):
        library.create_bundle(
            book_id=book.id,
            items=[SkuItem(product_id=upper.id), SkuItem(product_id=lower.id)],
            name="超额套装",
            price="45.00",
            sku_code="B-SET-2",
        )

    allowed = library.create_bundle(
        book_id=book.id,
        items=[SkuItem(product_id=upper.id), SkuItem(product_id=lower.id)],
        name="允许超额套装",
        price="45.00",
        sku_code="B-SET-3",
        allow_above_parts=True,
    )
    assert allowed.price_cents == 4500
    assert first.price_cents + second.price_cents == 3990


def test_asset_dedupe_shared_references_and_delete_protection(library, app, tmp_path):
    assets = AssetLibrary(library)
    path = tmp_path / "cover.png"
    path.write_bytes(b"same-cover-bytes")
    first, created = assets.add_file(path, license_source="test")
    second, created_again = assets.add_file(path, license_source="test")
    assert created is True and created_again is False
    assert first.id == second.id
    assert assets.by_sha(first.sha256).id == first.id

    book = make_book(library, code="T-ASSET", title="资产测试")
    assets.link(first.id, "book", book.id, role="cover")
    assert assets.ref_count(first.id) == 1
    assert assets.links_for_owner("book", book.id)[0]["asset_id"] == first.id

    with pytest.raises(Exception):
        assets.delete_asset(first.id, confirm=True)

    assets.unlink(first.id, "book", book.id, role="cover")
    assert assets.ref_count(first.id) == 0
    assets.delete_asset(first.id, confirm=True, delete_file=True)
    assert assets.get(first.id) is None
    assert not (app.paths.asset_dir(first.sha256) / f"{first.sha256}.png").exists()


def test_batch_preview_run_retry_export(library, tmp_path):
    engine = BatchEngine(library)
    rows = [
        {"教材编号": "BATCH-1", "书名": "批量一", "作者": "作者甲", "售价": "10.00"},
        {"教材编号": "BATCH-2", "书名": "批量二", "作者": "作者乙", "售价": "12.00"},
    ]
    job = engine.preview("import_books", rows, options={}, actor="tester")
    assert job.total == 2
    assert job.preview[0]["valid"] is True
    result = engine.run(job.id)
    assert result.status.value == "COMPLETED"
    assert library.db.get_book_by_code("BATCH-1") is not None

    bad = engine.preview(
        "update_price",
        [{"SKU编号": "NOPE", "售价": "1.00"}, {"SKU编号": "NOPE2", "售价": "2.00"}],
        options={},
        actor="tester",
    )
    assert bad.preview[0]["valid"] is False
    assert "不存在" in bad.preview[0]["message"]

    csv_path = tmp_path / "books.csv"
    engine.export_books(csv_path, actor="tester")
    assert csv_path.exists()
    exported = read_table(csv_path)
    assert len(exported) >= 2

    xlsx_path = tmp_path / "skus.xlsx"
    write_table(xlsx_path, ["SKU编号", "售价"], [{"SKU编号": "X", "售价": "1.00"}])
    assert read_table(xlsx_path)[0]["SKU编号"] == "X"


def test_backup_restore_and_integrity(library, app):
    book = make_book(library, code="T-BACKUP", title="备份测试")
    product = make_product(app, book)
    sku = library.create_single_sku(
        book_id=book.id, product_id=product.id, name="备份 SKU", price="19.90"
    )
    backups = BackupManager(library)
    manifest = backups.create_backup(label="test")
    path = Path(manifest["database"])
    assert path.exists()
    assert backups.verify_backup(path)["ok"] is True
    assert backups.integrity_check()["ok"] is True
    assert backups.create_auto_backup() is None

    library.set_price(sku.id, "99.00")
    assert library.get_sku(sku.id).price_cents == 9900
    result = backups.restore_backup(path, confirm=True)
    assert result["restored_from"] == str(path)
    assert library.get_sku(sku.id).price_cents == 1990
    assert backups.verify_backup(path)["ok"] is True


def test_seed_acceptance_pagination_and_search(library):
    from core.library.seed import seed_library

    report = seed_library(library, books=100, skus=300)
    assert report.books == 100
    assert report.skus == 300
    assert report.products >= 100

    page = library.list_books(page=1, page_size=10)
    assert page.total == 100
    assert len(page.items) == 10
    assert page.pages == 10

    second = library.list_books(page=2, page_size=10)
    assert {b.id for b in page.items}.isdisjoint({b.id for b in second.items})

    skus = library.list_skus(page=1, page_size=50)
    assert skus.total == 300
    assert len(skus.items) == 50

    found = library.list_books(q="SEED-001")
    assert found.total >= 1
    assert all("SEED-001" in book.code for book in found.items)

    lifecycle = library.list_skus(lifecycle=SkuLifecycle.READY, page_size=500)
    assert all(sku.lifecycle == SkuLifecycle.READY for sku in lifecycle.items)

    overview = library.overview()
    assert overview["books"] == 100
    assert overview["skus"] == 300
    assert sum(overview["lifecycle"].values()) == 300
