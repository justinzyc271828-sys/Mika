from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import httpx
import pymupdf as fitz
from PIL import Image

from core.book_lookup import BookLookupService, DeepSeekTools, extract_identity
from core.catalog import CatalogManager
from core.config import SettingsManager
from core.db import Database
from core.paths import AppPaths
from core.secrets import SecretStore
from models.schemas import Product, SkuItem, Subject, UploadRecord
from core.product_manager import sha256_file


class FakeNetdisk:
    effective_root = "/apps/test/家/商品库"

    def __init__(self, db: Database):
        self.db = db
        self.dirs: list[str] = []
        self.files: list[str] = []
        self.remote_sizes: dict[str, int] = {}

    def ensure_dir(self, path, *, create_root=False):
        self.dirs.append(path)
        return {"path": path}

    def upload_file(self, local, remote, product_id="", progress=None):
        local = Path(local)
        record = UploadRecord(
            product_id=product_id,
            local_path=str(local),
            remote_path=remote,
            size=local.stat().st_size,
            sha256=sha256_file(local),
            status="uploaded",
        )
        self.db.save_upload(record)
        self.files.append(remote)
        self.remote_sizes[remote] = record.size
        return record

    def stat(self, remote_path, *, dlink=False):
        return {"size": self.remote_sizes[remote_path], "md5": "cloud-hash"}


class FakeTools:
    def __init__(self, results: list[dict] | None = None, content: str = ""):
        self.calls = 0
        self.cost = 0.0
        self.cost_known = True
        self.searches = 0
        self.fetches = 0
        self.results = results or []
        self.content = content

    def configured(self):
        return True, ""

    def search(self, query, count=None, need_content=None):
        self.calls += 1
        self.searches += 1
        return {"results": list(self.results)}

    def fetch(self, url):
        self.calls += 1
        self.fetches += 1
        return {"title": "页面", "content": self.content}


class FakeResponse:
    status_code = 200
    headers = {"content-type": "image/png"}

    def __init__(self, content: bytes):
        self.content = content


class FakeClient:
    def __init__(self):
        image = Image.new("RGB", (400, 600), "#102A43")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        self.payload = buffer.getvalue()

    def get(self, url, headers=None):
        return FakeResponse(self.payload)

    def close(self):
        pass


def _strong_result() -> dict:
    return {
        "title": "线性代数（第六版）",
        "url": "https://publisher.example/book/123",
        "snippet": "同济大学数学系 编 高等教育出版社 ISBN 978-7-04-039663-8",
        "summary": "线性代数 第六版",
        "authority": 4,
        "rerank_score": 2.8,
        "date": "2024-01-01T00:00:00Z",
    }


def _page() -> str:
    return (
        "# 线性代数（第六版）\n\n作者：同济大学数学系\n出版社：高等教育出版社\n"
        "版次：第6版\n出版年份：2014\nISBN：978-7-04-039663-8\n\n"
        "![封面](https://publisher.example/cover.jpg)\n"
    )


def test_extract_identity_from_fixture():
    identity = extract_identity(Path("tests/fixtures/sample_linear_algebra.pdf"))
    assert "线性代数" in identity.title
    assert identity.subject == Subject.MATHEMATICS


def test_lookup_verifies_isbn_cover_and_caches(app):
    settings, db, secrets, paths = app.settings, app.db, app.secrets, app.paths
    tools = FakeTools([_strong_result()], _page())
    service = BookLookupService(settings, db, secrets, paths, tools=tools, client=FakeClient())
    outcome = service.lookup(
        title="线性代数",
        authors=["同济大学数学系"],
        publisher="高等教育出版社",
        isbn="9787040396638",
        force=True,
    )
    assert outcome.status == "VERIFIED"
    assert len(outcome.candidates) == 1
    assert outcome.cover_path and outcome.cover_path.exists()
    assert outcome.book["isbn_13"] == "9787040396638"
    assert outcome.book["publisher"] == "高等教育出版社"
    assert tools.searches == 2
    assert tools.fetches == 1

    cached_outcome = service.lookup(
        title="线性代数",
        authors=["同济大学数学系"],
        publisher="高等教育出版社",
        isbn="9787040396638",
    )
    assert cached_outcome.cached is True
    assert tools.searches == 2


def test_lookup_ambiguous_editions_need_review(app):
    settings, db, secrets, paths = app.settings, app.db, app.secrets, app.paths
    results = [
        dict(_strong_result(), title="线性代数（第六版）"),
        dict(_strong_result(), title="线性代数（第七版）", url="https://publisher.example/book/124"),
    ]
    tools = FakeTools(results, _page())
    service = BookLookupService(settings, db, secrets, paths, tools=tools, client=FakeClient())
    outcome = service.lookup(title="线性代数", publisher="高等教育出版社", force=True)
    assert outcome.status == "NEEDS_REVIEW"
    assert outcome.selected is None
    assert outcome.book["verification_status"] == "NEEDS_REVIEW"


def test_deepseek_search_records_cost_and_path(app):
    settings, db, secrets, paths = app.settings, app.db, app.secrets, app.paths
    settings.set("deepseek.enabled", True)
    secrets.set("deepseek_api_key", "sk-test")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"results": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    tools = DeepSeekTools(settings, secrets, db, client=client)
    assert tools.search("线性代数", count=5) == {"results": []}
    client.close()
    assert seen["url"] == "https://api.deepseek.com/v1/tools/search"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["count"] == 5
    assert tools.calls == 1
    assert abs(tools.cost - 0.00075) < 1e-9
    assert tools.cost_known is True


def test_catalog_prices_and_bundle_reuse(app):
    settings, db, secrets, paths = app.settings, app.db, app.secrets, app.paths
    manager = CatalogManager(settings, db, paths)
    book = manager.create_book(code="TWO01", title="线性代数", volumes=2, subject=Subject.MATHEMATICS)
    upper = Product(sku="TWO01-U", title="上册", subject=Subject.MATHEMATICS, status="packaged",
                    chapter_count=2, exercise_count=4, verified_count=4)
    lower = Product(sku="TWO01-L", title="下册", subject=Subject.MATHEMATICS, status="packaged",
                    chapter_count=2, exercise_count=4, verified_count=4)
    db.add_product(upper)
    db.add_product(lower)
    manager.attach_production(book.id, upper.id, volume_label="上册", volume_number=1, volume_code="U")
    manager.attach_production(book.id, lower.id, volume_label="下册", volume_number=2, volume_code="L")
    sku_u = manager.create_single_sku(book_id=book.id, product_id=upper.id, name="上册详解",
                                     price="3.99", original_price="3.99", edition="detailed", sku_code="TWO01-U")
    sku_l = manager.create_single_sku(book_id=book.id, product_id=lower.id, name="下册详解",
                                     price="3.99", original_price="3.99", edition="detailed", sku_code="TWO01-L")
    bundle = manager.create_bundle(
        book_id=book.id,
        items=[SkuItem(product_id=upper.id, volume_label="上册", edition="detailed"),
               SkuItem(product_id=lower.id, volume_label="下册", edition="detailed")],
        name="上下册套装",
        price="5.00",
        original_price="7.98",
        sku_code="TWO01-B",
    )
    assert sku_u.price_cents == 399
    assert sku_l.price_cents == 399
    assert bundle.price_cents == 500
    assert bundle.original_price_cents == 798
    assert manager.sku_totals(bundle)["discount_amount_cents"] == 298

    # A bundle may not exceed the sum of its parts without explicit permission.
    try:
        manager.create_bundle(
            book_id=book.id,
            items=[SkuItem(product_id=upper.id, volume_label="上册", edition="detailed"),
                   SkuItem(product_id=lower.id, volume_label="下册", edition="detailed")],
            name="错误套装",
            price="9.00",
            original_price="9.00",
            sku_code="TWO01-X",
        )
    except Exception as exc:
        assert "不能高于" in str(exc) or "高于" in str(exc)
    else:
        raise AssertionError("bundle above parts should have been rejected")


def test_catalog_delivery_references_pdfs_without_copying(app):
    settings, db, secrets, paths = app.settings, app.db, app.secrets, app.paths
    manager = CatalogManager(settings, db, paths)
    book = manager.create_book(code="DEL01", title="交付测试", volumes=1, subject=Subject.MATHEMATICS)
    product = Product(sku="DEL01-U", title="交付测试", subject=Subject.MATHEMATICS, status="packaged",
                      chapter_count=1, exercise_count=1, verified_count=1,
                      metadata={"pdf_files": []})
    db.add_product(product)
    manager.attach_production(book.id, product.id, volume_label="全一册", volume_number=1, volume_code="U")
    sku = manager.create_single_sku(book_id=book.id, product_id=product.id, name="交付测试",
                                    price="1.00", edition="detailed", sku_code="DEL01-U")
    product_dir = paths.product_dir(product.sku, product.title)
    (product_dir / "04_最终PDF").mkdir(parents=True, exist_ok=True)
    pdf = product_dir / "04_最终PDF" / "DEL01-U_交付测试_v1.0.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "test page")
    doc.save(pdf)
    doc.close()

    images = paths.sku_subdirs(paths.sku_dir(sku.sku_code))["images"]
    images.mkdir(parents=True, exist_ok=True)
    for index in range(1, 8):
        Image.new("RGB", (40, 60), "#ffffff").save(images / f"{index:02d}.png")

    delivery = manager.build_delivery(sku)
    manifest = json.loads(delivery.manifest.read_text(encoding="utf-8"))
    assert any(item["name"] == pdf.name for item in manifest["files"])
    assert len([item for item in manifest["files"] if item["kind"] == "product_image"]) == 7
    assert (paths.sku_dir(sku.sku_code) / "02_商品信息" / "商品介绍.md").exists()
    assert pdf.exists()


def test_sku_upload_creates_folders_and_verifies_size(app):
    settings, db, secrets, paths = app.settings, app.db, app.secrets, app.paths
    fake = FakeNetdisk(db)
    manager = CatalogManager(settings, db, paths, netdisk=fake)
    book = manager.create_book(code="UP01", title="上传测试", volumes=1, subject=Subject.MATHEMATICS)
    product = Product(sku="UP01-U", title="上传测试", subject=Subject.MATHEMATICS, status="packaged",
                      chapter_count=1, exercise_count=1, verified_count=1)
    db.add_product(product)
    manager.attach_production(book.id, product.id, volume_label="全一册", volume_number=1, volume_code="U")
    sku = manager.create_single_sku(book_id=book.id, product_id=product.id, name="上传测试",
                                    price="1.00", edition="detailed", sku_code="UP01-U")
    product_dir = paths.product_dir(product.sku, product.title)
    (product_dir / "04_最终PDF").mkdir(parents=True, exist_ok=True)
    pdf = product_dir / "04_最终PDF" / "UP01-U_上传测试_v1.0.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "upload test")
    doc.save(pdf)
    doc.close()
    images = paths.sku_subdirs(paths.sku_dir(sku.sku_code))["images"]
    images.mkdir(parents=True, exist_ok=True)
    for index in range(1, 8):
        Image.new("RGB", (40, 60), "#ffffff").save(images / f"{index:02d}.png")

    records = manager.upload_sku(sku)
    assert len(records) == 10  # one PDF + seven images + two info files
    assert all(record.status == "verified" for record in records), [
        (record.local_path, record.status, record.error) for record in records
    ]
    assert any(path.endswith("/01_习题详解") for path in fake.dirs)
    assert any(path.endswith("/02_商品图片") for path in fake.dirs)
    assert any(path.endswith("/03_商品信息") for path in fake.dirs)
    assert any("/03_商品信息/商品介绍.md" in path for path in fake.files)
    assert all("/apps/test/家/商品库/UP01/UP01-U/" in path for path in fake.files)

