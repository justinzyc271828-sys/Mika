"""Two-volume SKU acceptance flow using synthetic, clearly-labelled fixtures.

This proves volume separation, production reuse by bundles, RGB price
arithmetic, per-SKU images and delivery manifests without calling an external
model or pretending to have found a publisher cover.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.catalog import CatalogManager  # noqa: E402
from core.config import SettingsManager  # noqa: E402
from core.db import Database  # noqa: E402
from core.paths import AppPaths  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402
from models.schemas import Product, SkuItem, Subject  # noqa: E402
from tests.pipeline_smoke import FixtureSolver, FixtureVerifier  # noqa: E402


def main() -> int:
    root = ROOT / ".tmp_two_volume_test"
    if root.exists():
        shutil.rmtree(root)
    paths = AppPaths(root)
    paths.ensure()
    settings = SettingsManager(paths)
    db = Database(paths.database)
    catalog = CatalogManager(settings, db, paths)

    book = catalog.create_book(
        code="LA001",
        title="线性代数",
        authors=["WenLianJia Test Fixture"],
        publisher="测试出版社",
        edition="测试版",
        volumes=2,
        subject=Subject.MATHEMATICS,
        metadata={
            "cover_note": "两册套装验收样张：合成内容，不代表出版社原版教材或真实 AI 解答。",
            "cover_status": "自主设计，无出版社原版封面",
        },
    )

    volumes = [
        ("上册", 1, "U", ROOT / "tests" / "fixtures" / "sample_linear_algebra_upper.pdf"),
        ("下册", 2, "L", ROOT / "tests" / "fixtures" / "sample_linear_algebra_lower.pdf"),
    ]
    productions: dict[str, Product] = {}
    skus = {}
    for label, number, code, pdf in volumes:
        product = Product(
            sku=f"LA001-{code}",
            title=f"线性代数 {label} 习题详解（测试样张）",
            subject=Subject.MATHEMATICS,
            source_title=f"线性代数 {label}（合成测试教材）",
            source_author="WenLianJia Test Fixture",
            source_path=str(pdf),
            version="1.0",
            book_id=book.id,
            volume_label=label,
            volume_number=number,
            volume_code=code,
            metadata={
                "cover_note": "两册套装验收样张：合成内容，不代表真实 AI 结果。",
                "volume_label": label,
            },
        )
        db.add_product(product)
        pipeline = Pipeline(settings, db, paths, solver=FixtureSolver(db), verifier=FixtureVerifier(db))
        result = pipeline.start(product.id, {"upload": False, "share": False, "cleanup": False})
        print(f"{label}: job={result.job.status.value}, product={result.product.status}, "
              f"verified={result.product.verified_count}/{result.product.exercise_count}, "
              f"pdf={result.full_pdf}")
        if result.errors:
            print("ERRORS", result.errors)
            return 2
        if not result.full_pdf or not Path(result.full_pdf).exists():
            return 3
        productions[label] = result.product
        catalog.attach_production(book.id, result.product.id, volume_label=label, volume_number=number, volume_code=code)
        sku = catalog.create_single_sku(
            book_id=book.id,
            product_id=result.product.id,
            name=f"线性代数 {label} 习题详解",
            price="3.99",
            original_price="3.99",
            edition="detailed",
            sku_code=f"LA001-{code}",
        )
        catalog.generate_images(sku)
        delivery = catalog.build_delivery(sku)
        print(f"{label}: sku={sku.sku_code}, images={len(delivery.files)}, status={delivery.status}")
        skus[label] = sku

    bundle = catalog.create_bundle(
        book_id=book.id,
        items=[
            SkuItem(product_id=productions["上册"].id, volume_label="上册", edition="detailed", sort_order=0),
            SkuItem(product_id=productions["下册"].id, volume_label="下册", edition="detailed", sort_order=1),
        ],
        name="线性代数 上下册 习题详解 套装",
        price="5.00",
        original_price="7.98",
        sku_code="LA001-B",
    )
    catalog.generate_images(bundle)
    delivery = catalog.build_delivery(bundle)
    print(f"套装: sku={bundle.sku_code}, price=¥{bundle.price_cents/100:.2f}, "
          f"original=¥{bundle.original_price_cents/100:.2f}, images={len(delivery.files)}")
    print("DELIVERY", delivery.sku_dir)

    summary = {
        "book": book.model_dump(mode="json"),
        "productions": {label: p.model_dump(mode="json") for label, p in productions.items()},
        "skus": {label: sku.model_dump(mode="json") for label, sku in skus.items()},
        "bundle": bundle.model_dump(mode="json"),
        "bundle_delivery": [
            f.as_dict(paths.root) if hasattr(f, "as_dict")
            else (dict(f) if isinstance(f, dict) else str(f))
            for f in delivery.files
        ],
        "note": "合成两册验收样张；未调用 OpenAI、未上传、未创建分享，封面为自主设计。",
    }
    (root / "TWO_VOLUME_ACCEPTANCE.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
