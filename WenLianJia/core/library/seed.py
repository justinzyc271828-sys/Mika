"""Deterministic acceptance data for the large product library.

The seed is intentionally self-contained: it uses the public service API so the
generated data also exercises the same validation paths as real edits.  It is
used by ``scripts/seed_library.py`` and by the acceptance tests.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core.library.common import LibraryError
from core.library.service import LibraryService
from models.schemas import (
    BookVersionStatus,
    DeliveryStatus,
    Product,
    SkuItem,
    SkuLifecycle,
    stable_hash,
)

SEED_PREFIX = "SEED"
SEED_CATEGORIES = [
    ("SEED-CAT-01", "高等数学"),
    ("SEED-CAT-02", "线性代数"),
    ("SEED-CAT-03", "概率统计"),
    ("SEED-CAT-04", "大学物理"),
    ("SEED-CAT-05", "电路原理"),
    ("SEED-CAT-06", "数据结构"),
    ("SEED-CAT-07", "有机化学"),
    ("SEED-CAT-08", "考研公共课"),
]
SEED_TAGS = [
    "模拟",
    "热销",
    "新版",
    "考研",
    "期末",
    "基础",
    "提高",
    "套装",
    "上册",
    "下册",
    "电子版",
    "限时",
]
SUBJECTS = ["mathematics", "physics", "chemistry", "computer_science", "other"]
PUBLISHERS = ["模拟教育出版社", "文链出版社", "高等教材出版社", "科技教材出版社"]
AUTHORS = ["模拟作者", "文链教研组", "高等数学编写组", "王老师"]


@dataclass
class SeedReport:
    books: int = 0
    products: int = 0
    versions: int = 0
    skus: int = 0
    singles: int = 0
    bundles: int = 0
    quick: int = 0
    assets: int = 0
    categories: int = 0
    tags: int = 0
    reset: bool = False
    skipped: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def seed_library(
    service: LibraryService,
    *,
    books: int = 100,
    skus: int = 300,
    reset: bool = False,
    assets: int = 12,
    actor: str = "seed",
) -> SeedReport:
    """Create at least ``books`` books and ``skus`` SKUs.

    The exact SKU count is ``books + 2 * two_volume + quick`` where
    ``two_volume = books // 5`` and ``quick`` fills the remainder.  For the
    acceptance defaults (100/300) this yields exactly 300 SKUs.
    """
    books = max(1, int(books))
    skus = max(1, int(skus))
    report = SeedReport(reset=reset)
    if reset:
        _reset_seed(service)

    family = service.ensure_default_family()
    categories = _seed_categories(service)
    tags = _seed_tags(service)
    report.categories = len(categories)
    report.tags = len(tags)

    two_volume = max(0, books // 5)
    product_total = books + two_volume
    base_sku_total = books + 2 * two_volume
    quick_total = max(0, skus - base_sku_total)

    book_models = []
    products_by_book: dict[str, list[Product]] = {}
    version_by_book: dict[str, str] = {}

    for index in range(1, books + 1):
        code = f"{SEED_PREFIX}-{index:03d}"
        subject = SUBJECTS[(index - 1) % len(SUBJECTS)]
        publisher = PUBLISHERS[(index - 1) % len(PUBLISHERS)]
        author = AUTHORS[(index - 1) % len(AUTHORS)]
        volume_count = 2 if index <= two_volume else 1
        try:
            book = service.create_book(
                code=code,
                title=f"模拟教材 {index:03d}",
                authors=[author],
                publisher=publisher,
                edition="第1版",
                publication_year=str(2020 + index % 5),
                isbn_13=f"9787{index:09d}",
                volumes=volume_count,
                subject=subject,
                category_id=categories[(index - 1) % len(categories)].id,
                metadata={"seed": True},
            )
        except LibraryError as exc:
            report.skipped.append(f"{code}: {exc}")
            continue
        book_models.append(book)

        products: list[Product] = []
        for volume in range(1, volume_count + 1):
            if volume_count == 1:
                volume_label, volume_code = "全一册", "S"
            else:
                volume_label = "上册" if volume == 1 else "下册"
                volume_code = "U" if volume == 1 else "L"
            product = Product(
                id=f"prd_seed_{index:03d}_{volume}",
                book_id=book.id,
                volume_label=volume_label,
                volume_number=volume,
                volume_code=volume_code,
                sku=f"{code}-{volume_code}",
                title=f"{book.title} {volume_label}",
                subject=book.subject,
                source_title=book.title,
                source_author=author,
                source_edition="seed",
                source_path="",
                source_sha256=stable_hash("seed", code, volume),
                total_pages=120 + volume * 10,
                chapter_count=6,
                exercise_count=40 + volume * 5,
                solved_count=40 + volume * 5,
                verified_count=40 + volume * 5,
                review_required=False,
                status="completed",
                version="1.0",
                notes="验收模拟数据",
                metadata={"seed": True},
            )
            service.db.add_product(product)
            products.append(product)
            report.products += 1
        products_by_book[book.id] = products

        try:
            version = service.create_version(
                book_id=book.id,
                version_label="v1.0",
                notes="初版习题解析",
                product_id=products[0].id,
                status=BookVersionStatus.ACTIVE,
                created_by=actor,
            )
            report.versions += 1
            if index % 10 == 0:
                newer = service.create_version(
                    book_id=book.id,
                    version_label="v1.1",
                    notes="修复三道题目的答案",
                    product_id=products[0].id,
                    status=BookVersionStatus.DRAFT,
                    created_by=actor,
                )
                service.activate_version(newer.id)
                report.versions += 1
                version = newer
            version_by_book[book.id] = version.id
        except LibraryError as exc:
            report.skipped.append(f"{code} version: {exc}")

        if index % 3 == 0:
            service.tag_entity("book", book.id, tags[0].id)
        if index % 5 == 0:
            service.tag_entity("book", book.id, tags[3].id)

    report.books = len(book_models)

    sku_index = 0
    lifecycle_cycle = list(SkuLifecycle)
    delivery_cycle = list(DeliveryStatus)

    def next_lifecycle() -> SkuLifecycle:
        nonlocal sku_index
        lifecycle = lifecycle_cycle[sku_index % len(lifecycle_cycle)]
        sku_index += 1
        return lifecycle

    def create_single(
        book,
        product: Product,
        *,
        edition: str,
        suffix: str,
        price: str,
        original: str,
        name_suffix: str,
    ) -> None:
        lifecycle = next_lifecycle()
        sku = service.create_single_sku(
            book_id=book.id,
            product_id=product.id,
            name=f"{book.title} {product.volume_label} {name_suffix}",
            price=price,
            original_price=original,
            edition=edition,
            sku_code=f"{book.code}-{product.volume_code}-{suffix}",
            enabled=lifecycle not in {SkuLifecycle.ARCHIVED, SkuLifecycle.PAUSED},
            lifecycle=lifecycle,
            version_id=version_by_book.get(book.id, ""),
            category_id=book.category_id,
        )
        _apply_delivery(service, sku.id, lifecycle, delivery_cycle)
        _tag_sku(service, sku.id, tags)
        report.skus += 1
        if edition == "quick":
            report.quick += 1
        else:
            report.singles += 1

    for book in book_models:
        products = products_by_book.get(book.id, [])
        for product in products:
            try:
                create_single(
                    book,
                    product,
                    edition="detailed",
                    suffix="D",
                    price="19.90",
                    original="29.90",
                    name_suffix="详细解析",
                )
            except LibraryError as exc:
                report.skipped.append(f"{book.code}/{product.volume_code}: {exc}")

    for book in book_models:
        products = products_by_book.get(book.id, [])
        if len(products) < 2:
            continue
        lifecycle = next_lifecycle()
        try:
            sku = service.create_bundle(
                book_id=book.id,
                items=[
                    SkuItem(product_id=product.id, volume_label=product.volume_label)
                    for product in products
                ],
                name=f"{book.title} 上下册套装",
                price="34.90",
                original_price="49.90",
                sku_code=f"{book.code}-B",
                enabled=lifecycle not in {SkuLifecycle.ARCHIVED, SkuLifecycle.PAUSED},
                lifecycle=lifecycle,
                version_id=version_by_book.get(book.id, ""),
                category_id=book.category_id,
            )
            _apply_delivery(service, sku.id, lifecycle, delivery_cycle)
            _tag_sku(service, sku.id, tags)
            report.bundles += 1
            report.skus += 1
        except LibraryError as exc:
            report.skipped.append(f"{book.code} bundle: {exc}")

    quick_candidates = [
        (book, product)
        for book in book_models
        for product in products_by_book.get(book.id, [])
    ]
    for position in range(quick_total):
        if not quick_candidates:
            break
        book, product = quick_candidates[position % len(quick_candidates)]
        try:
            create_single(
                book,
                product,
                edition="quick",
                suffix="Q" if position < len(quick_candidates) else f"Q{position}",
                price="9.90",
                original="19.90",
                name_suffix="速学版",
            )
        except LibraryError as exc:
            report.skipped.append(f"{book.code} quick: {exc}")

    report.assets = _seed_assets(service, book_models, report)

    service.audit(
        "seed.run",
        "library",
        "seed",
        after=report.as_dict(),
        detail=f"生成验收数据：{report.books} 本教材、{report.skus} 个 SKU",
        actor=actor,
    )
    return report


def _apply_delivery(
    service: LibraryService,
    sku_id: str,
    lifecycle: SkuLifecycle,
    cycle: list[DeliveryStatus],
) -> None:
    if lifecycle is SkuLifecycle.PUBLISHED:
        status = DeliveryStatus.VERIFIED
    elif lifecycle is SkuLifecycle.READY:
        status = DeliveryStatus.UPLOADED
    elif lifecycle is SkuLifecycle.REVIEW_REQUIRED:
        status = DeliveryStatus.NEEDS_MANUAL
    else:
        status = cycle[0]
    if status is DeliveryStatus.NOT_DELIVERED:
        return
    try:
        service.set_delivery_status(sku_id, status)
    except (LibraryError, ValueError):
        pass


def _tag_sku(service: LibraryService, sku_id: str, tags: list[Any]) -> None:
    if not tags:
        return
    index = int(stable_hash(sku_id)[:8], 16) % 97
    try:
        if index % 7 == 0:
            service.tag_entity("sku", sku_id, tags[1].id)
        if index % 11 == 0:
            service.tag_entity("sku", sku_id, tags[2].id)
    except LibraryError:
        pass


def _seed_categories(service: LibraryService) -> list[Any]:
    existing = {category.code: category for category in service.list_categories()}
    result = []
    for code, name in SEED_CATEGORIES:
        category = existing.get(code)
        if category is None:
            category = service.create_category(code=code, name=name, sort_order=len(result))
        result.append(category)
    return result


def _seed_tags(service: LibraryService) -> list[Any]:
    existing = {tag.name: tag for tag in service.list_tags()}
    result = []
    for name in SEED_TAGS:
        tag = existing.get(name)
        if tag is None:
            tag = service.create_tag(name)
        result.append(tag)
    return result


def _seed_assets(
    service: LibraryService, books: list[Any], report: SeedReport
) -> int:
    source_dir = service.paths.incoming / "seed-assets"
    source_dir.mkdir(parents=True, exist_ok=True)
    created = 0
    for index in range(12):
        path = source_dir / f"seed-asset-{index + 1:02d}.txt"
        if not path.is_file():
            path.write_text(
                f"文链家验收资产 {index + 1}\n"
                f"seed asset for product-library acceptance\n",
                encoding="utf-8",
            )
        try:
            asset, is_new = service.assets.add_file(
                path,
                license_source="seed",
                version="1",
                metadata={"seed": True, "index": index + 1},
            )
        except (LibraryError, OSError) as exc:
            report.skipped.append(f"asset {path.name}: {exc}")
            continue
        created += 1
        if index < len(books):
            service.assets.link(asset.id, "book", books[index].id, role="cover")
            # Deliberately share a few files across books so the acceptance run
            # proves that one asset can be referenced by multiple products.
            if index < 3 and len(books) > index + 2:
                service.assets.link(asset.id, "book", books[index + 1].id, role="gallery")
                service.assets.link(asset.id, "book", books[index + 2].id, role="gallery")
    return created


def _reset_seed(service: LibraryService) -> None:
    """Remove only rows created by this seed (prefix based)."""
    db = service.db
    sku_rows = db.query("SELECT id FROM skus WHERE sku_code LIKE ?", (f"{SEED_PREFIX}-%",))
    for row in sku_rows:
        service.assets.unlink_owner("sku", row["id"])
        db.delete_sku(row["id"])

    book_rows = db.query("SELECT id FROM books WHERE code LIKE ?", (f"{SEED_PREFIX}-%",))
    for row in book_rows:
        book_id = row["id"]
        service.assets.unlink_owner("book", book_id)
        db.execute("DELETE FROM book_versions WHERE book_id=?", (book_id,))
        db.execute("DELETE FROM products WHERE book_id=?", (book_id,))
        db.execute("DELETE FROM books WHERE id=?", (book_id,))

    for code, _ in SEED_CATEGORIES:
        db.execute("DELETE FROM categories WHERE code=?", (code,))
    placeholders = ",".join("?" for _ in SEED_TAGS)
    db.execute(f"DELETE FROM tags WHERE name IN ({placeholders})", tuple(SEED_TAGS))

    page = service.assets.list_assets(page=1, page_size=500)
    for item in page.items:
        metadata = item.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except ValueError:
                metadata = {}
        if metadata.get("seed"):
            try:
                service.assets.delete_asset(
                    item["id"], confirm=True, delete_file=True
                )
            except LibraryError:
                pass
