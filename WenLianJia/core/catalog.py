"""Textbook, production and sale-SKU catalogue.

The catalogue is the layer above the production pipeline:

* :class:`Book` identifies a textbook edition;
* :class:`Product` is one produced volume (answers + PDFs);
* :class:`SalesSKU` is what the owner actually sells, including bundles that
  reference several existing productions without copying their files.

All money is handled as integer cents.  Yuan values are only parsed/rendered at
the boundary, so ``3.99`` never becomes ``3.989999...``.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable

from models.schemas import (
    Book,
    Product,
    SalesSKU,
    SkuItem,
    Subject,
    utc_now,
)
from .config import SettingsManager
from .db import Database
from .logging_setup import get_logger
from .paths import AppPaths

logger = get_logger("catalog")

MONEY = Decimal("0.01")
EDITIONS = {"detailed": "详细解析版", "quick": "答案速查版", "both": "详细解析+速查版"}


class CatalogError(RuntimeError):
    pass


def yuan_to_cents(value: object, *, field_name: str = "价格") -> int:
    """Parse a CNY amount with two decimals into integer cents."""

    if value is None or value == "":
        return 0
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise CatalogError(f"{field_name}不是有效数字：{value}") from exc
    if amount < 0:
        raise CatalogError(f"{field_name}不能为负数")
    return int((amount.quantize(MONEY, rounding=ROUND_HALF_UP) * 100).to_integral_value())


def cents_to_yuan(cents: int) -> str:
    return f"{Decimal(int(cents)) / 100:.2f}"


def discount_info(sku: SalesSKU) -> dict[str, object]:
    original = int(sku.original_price_cents or 0)
    price = int(sku.price_cents or 0)
    amount = max(0, original - price) if original else 0
    rate = (Decimal(amount) / Decimal(original)) if original else Decimal("0")
    return {
        "price": cents_to_yuan(price),
        "original_price": cents_to_yuan(original) if original else "",
        "discount_amount": cents_to_yuan(amount),
        "discount_rate": float((rate * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        "price_cents": price,
        "original_price_cents": original,
        "discount_amount_cents": amount,
    }


@dataclass
class SkuDeliveryResult:
    sku_dir: Path
    manifest: Path
    info_json: Path
    delivery_text: Path
    acceptance: Path
    files: list[dict[str, object]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    status: str = "needs_review"


class CatalogManager:
    def __init__(
        self,
        settings: SettingsManager,
        db: Database,
        paths: AppPaths,
        *,
        netdisk=None,
    ):
        self.settings = settings
        self.db = db
        self.paths = paths
        self.netdisk = netdisk

    # ------------------------------------------------------------------ books

    def create_book(
        self,
        *,
        code: str,
        title: str,
        authors: Iterable[str] | None = None,
        original_title: str = "",
        publisher: str = "",
        edition: str = "",
        publication_year: str = "",
        isbn_10: str = "",
        isbn_13: str = "",
        volumes: int = 1,
        language: str = "zh-CN",
        subject: Subject = Subject.OTHER,
        metadata: dict | None = None,
    ) -> Book:
        code = code.strip().upper()
        title = title.strip()
        if not code or not title:
            raise CatalogError("书目编号和书名不能为空")
        if self.db.get_book_by_code(code):
            raise CatalogError(f"书目编号已存在：{code}")
        book = Book(
            code=code,
            title=title,
            original_title=original_title.strip(),
            authors=[a.strip() for a in (authors or []) if a.strip()],
            publisher=publisher.strip(),
            edition=edition.strip(),
            publication_year=publication_year.strip(),
            isbn_10=isbn_10.strip(),
            isbn_13=isbn_13.strip(),
            volumes=max(1, int(volumes)),
            language=language,
            subject=subject,
            metadata=metadata or {},
        )
        self.db.add_book(book)
        self.write_book_metadata(book)
        logger.info("created book %s (%s)", book.code, book.id)
        return book

    def update_book(self, book: Book, **fields) -> Book:
        self.db.update_book(book.id, **fields)
        refreshed = self.db.get_book(book.id)
        if refreshed:
            self.write_book_metadata(refreshed)
            return refreshed
        raise CatalogError("书目不存在")

    def attach_production(
        self,
        book_id: str,
        product_id: str,
        *,
        volume_label: str,
        volume_number: int = 0,
        volume_code: str = "",
    ) -> Product:
        book = self.db.get_book(book_id)
        product = self.db.get_product(product_id)
        if not book or not product:
            raise CatalogError("书目或生产实体不存在")
        if product.book_id and product.book_id != book_id:
            raise CatalogError("该生产实体已属于另一本书")
        siblings = [
            p
            for p in self.db.list_productions(book_id)
            if p.id != product_id and p.volume_number == volume_number and volume_number > 0
        ]
        if siblings:
            raise CatalogError(f"第 {volume_number} 册已经绑定其他生产实体")
        self.db.update_product(
            product_id,
            book_id=book_id,
            volume_label=volume_label.strip() or f"第{volume_number}册",
            volume_number=int(volume_number),
            volume_code=volume_code.strip(),
        )
        if book.volumes < len(self.db.list_productions(book_id)):
            self.db.update_book(book_id, volumes=len(self.db.list_productions(book_id)))
        updated = self.db.get_product(product_id)
        if updated is None:
            raise CatalogError("绑定后无法读取生产实体")
        return updated

    # ------------------------------------------------------------------- skus

    def _next_sku_code(self, book: Book, suffix: str) -> str:
        base = f"{book.code}-{suffix}".upper()
        candidate = base
        index = 2
        while self.db.get_sku_by_code(candidate):
            candidate = f"{base}{index}"
            index += 1
        return candidate

    def _description(self, book: Book, items: list[SkuItem], productions: list[Product]) -> str:
        parts = []
        for item in items:
            product = next((p for p in productions if p.id == item.product_id), None)
            if product:
                parts.append(
                    f"{product.volume_label or product.title}：{product.exercise_count} 道习题，"
                    f"{product.chapter_count} 章，{EDITIONS.get(item.edition, item.edition)}"
                )
        if len(items) > 1:
            head = f"《{book.title}》上下册组合套装，包含 {len(items)} 个独立分册。"
        else:
            head = f"《{book.title}》{items[0].volume_label if items else ''}习题答案与详细解析。"
        return head + " " + "；".join(parts) + " 所有解析均经过独立复核流程，PDF 保留原书页码。"

    def create_single_sku(
        self,
        *,
        book_id: str,
        product_id: str,
        name: str,
        price: object,
        original_price: object = None,
        edition: str = "detailed",
        sku_code: str = "",
        enabled: bool = True,
        description: str = "",
    ) -> SalesSKU:
        book = self.db.get_book(book_id)
        product = self.db.get_product(product_id)
        if not book or not product:
            raise CatalogError("书目或生产实体不存在")
        if product.book_id != book_id:
            raise CatalogError("生产实体尚未绑定到这本书")
        if edition not in ("detailed", "quick", "both"):
            raise CatalogError("版本类型必须是 detailed、quick 或 both")
        price_cents = yuan_to_cents(price, field_name="售价")
        original_cents = yuan_to_cents(original_price, field_name="原价") if original_price else 0
        if price_cents <= 0:
            raise CatalogError("售价必须大于 0")
        if original_cents and original_cents < price_cents:
            raise CatalogError("原价不能低于售价")
        items = [
            SkuItem(
                product_id=product.id,
                volume_label=product.volume_label,
                edition=edition,
                sort_order=0,
            )
        ]
        code = (sku_code or "").strip().upper() or self._next_sku_code(
            book, product.volume_code or f"V{product.volume_number or 1}"
        )
        if self.db.get_sku_by_code(code):
            raise CatalogError(f"SKU 编号已存在：{code}")
        sku = SalesSKU(
            sku_code=code,
            book_id=book_id,
            name=name.strip() or f"{book.title} {product.volume_label} {EDITIONS[edition]}",
            kind="single",
            items=items,
            price_cents=price_cents,
            original_price_cents=original_cents,
            enabled=enabled,
            description=description.strip()
            or self._description(book, items, [product]),
            status="draft",
        )
        self.db.add_sku(sku)
        logger.info("created single sku %s", sku.sku_code)
        return sku

    def component_baseline_cents(self, book_id: str, items: list[SkuItem]) -> int:
        """Sum of the existing single-volume reference prices.

        A bundle may not be more expensive than buying its parts unless the
        owner explicitly allows it.  This is checked against real single SKUs,
        never against invented prices.
        """

        singles = [s for s in self.db.list_skus(book_id) if s.kind == "single"]
        total = 0
        missing: list[str] = []
        for item in items:
            match = next(
                (
                    s
                    for s in singles
                    if s.items
                    and s.items[0].product_id == item.product_id
                    and (s.items[0].edition == item.edition or item.edition == "both")
                ),
                None,
            )
            if not match:
                match = next(
                    (
                        s
                        for s in singles
                        if s.items and s.items[0].product_id == item.product_id
                    ),
                    None,
                )
            if not match:
                missing.append(item.product_id)
                continue
            total += int(match.original_price_cents or match.price_cents)
        if missing:
            raise CatalogError(
                "套装需要先为每个分册建立单册 SKU 作为价格基准；缺少："
                + "、".join(missing)
            )
        return total

    def create_bundle(
        self,
        *,
        book_id: str,
        items: list[SkuItem],
        name: str,
        price: object,
        original_price: object = None,
        sku_code: str = "",
        enabled: bool = True,
        description: str = "",
        allow_above_parts: bool = False,
    ) -> SalesSKU:
        book = self.db.get_book(book_id)
        if not book:
            raise CatalogError("书目不存在")
        if len(items) < 2:
            raise CatalogError("套装至少需要两个分册")
        product_ids = [item.product_id for item in items]
        if len(set(product_ids)) != len(product_ids):
            raise CatalogError("套装不能重复包含同一个分册")
        productions: list[Product] = []
        for item in items:
            product = self.db.get_product(item.product_id)
            if not product:
                raise CatalogError(f"生产实体不存在：{item.product_id}")
            if product.book_id != book_id:
                raise CatalogError("套装中的分册不属于同一本书")
            productions.append(product)
        if any(not p.verified_count or p.status not in ("packaged", "completed", "ready") for p in productions):
            raise CatalogError("套装中的每个分册都必须完成解析与复核")
        baseline = self.component_baseline_cents(book_id, items)
        price_cents = yuan_to_cents(price, field_name="套装售价")
        original_cents = (
            yuan_to_cents(original_price, field_name="套装原价")
            if original_price
            else baseline
        )
        if price_cents <= 0:
            raise CatalogError("套装售价必须大于 0")
        if not allow_above_parts and price_cents > baseline:
            raise CatalogError(
                f"套装售价不能高于各分册原价合计 ¥{cents_to_yuan(baseline)}；"
                "如确需高于，请由用户明确允许"
            )
        if original_cents and original_cents < price_cents:
            raise CatalogError("套装原价不能低于售价")
        for index, item in enumerate(items):
            item.sort_order = index
            item.volume_label = item.volume_label or next(
                p.volume_label for p in productions if p.id == item.product_id
            )
        code = (sku_code or "").strip().upper() or self._next_sku_code(book, "B")
        if self.db.get_sku_by_code(code):
            raise CatalogError(f"SKU 编号已存在：{code}")
        sku = SalesSKU(
            sku_code=code,
            book_id=book_id,
            name=name.strip() or f"{book.title} 上下册完整套装",
            kind="bundle",
            items=items,
            price_cents=price_cents,
            original_price_cents=original_cents,
            enabled=enabled,
            description=description.strip()
            or self._description(book, items, productions),
            status="draft",
            allow_above_parts=allow_above_parts,
        )
        sku.description += f" 套装原价 ¥{cents_to_yuan(baseline)}，立省 ¥{cents_to_yuan(max(0, baseline - price_cents))}。"
        self.db.add_sku(sku)
        logger.info("created bundle sku %s (%d parts)", sku.sku_code, len(items))
        return sku

    def update_sku(self, sku: SalesSKU, **fields) -> SalesSKU:
        if "price_cents" in fields:
            price = int(fields["price_cents"])
            if price <= 0:
                raise CatalogError("售价必须大于 0")
            fields["price_cents"] = price
        if "original_price_cents" in fields:
            fields["original_price_cents"] = max(0, int(fields["original_price_cents"]))
        self.db.update_sku(sku.id, **fields)
        updated = self.db.get_sku(sku.id)
        if updated is None:
            raise CatalogError("SKU 不存在")
        # Re-run bundle price validation against the reference singles.
        if updated.kind == "bundle" and not updated.allow_above_parts:
            baseline = self.component_baseline_cents(updated.book_id, updated.items)
            if updated.price_cents > baseline:
                self.db.update_sku(sku.id, price_cents=sku.price_cents)
                raise CatalogError("套装售价超过分册合计，已撤销修改")
        return updated

    def set_price(self, sku: SalesSKU, price: object, original_price: object = None) -> SalesSKU:
        fields: dict[str, object] = {"price_cents": yuan_to_cents(price, field_name="售价")}
        if original_price is not None:
            fields["original_price_cents"] = yuan_to_cents(
                original_price, field_name="原价"
            )
        return self.update_sku(sku, **fields)

    def batch_set_prices(self, prices: dict[str, object]) -> dict[str, SalesSKU]:
        updated: dict[str, SalesSKU] = {}
        for sku_id, price in prices.items():
            sku = self.db.get_sku(sku_id)
            if not sku:
                continue
            updated[sku.id] = self.set_price(sku, price)
        return updated

    def delete_sku(self, sku_id: str) -> None:
        sku = self.db.get_sku(sku_id)
        if not sku:
            return
        # Deleting a sale never touches productions or their PDFs.
        self.db.delete_sku(sku_id)
        logger.info("deleted sku %s", sku.sku_code)

    def sku_totals(self, sku: SalesSKU) -> dict[str, object]:
        info = discount_info(sku)
        info["component_total"] = (
            cents_to_yuan(self.component_baseline_cents(sku.book_id, sku.items))
            if sku.kind == "bundle"
            else ""
        )
        return info

    # ------------------------------------------------------------- deliverables

    def _product_dir(self, product: Product) -> Path:
        for completed in (False, True):
            path = self.paths.product_dir(
                product.sku, product.title, completed=completed
            )
            if path.exists():
                return path
        return self.paths.product_dir(product.sku, product.title)

    def _find_pdfs(self, product: Product, edition: str) -> list[Path]:
        files = [
            Path(f.path)
            for f in self.db.list_files(product.id, kind="pdf")
            if Path(f.path).exists()
        ]
        if not files:
            pdf_dir = self._product_dir(product) / "04_最终PDF"
            if pdf_dir.exists():
                files = sorted(pdf_dir.glob("*.pdf"))
        if edition == "detailed":
            selected = [p for p in files if "答案速查" not in p.name]
        elif edition == "quick":
            selected = [p for p in files if "答案速查" in p.name]
        else:
            selected = files
        return selected

    def _cover(self, book: Book) -> Path | None:
        if book.cover_image:
            path = Path(book.cover_image)
            if path.exists():
                return path
        for candidate in (
            self.paths.book_dir(book.code) / "cover_original.jpg",
            self.paths.book_dir(book.code) / "cover_original.png",
            self.paths.book_dir(book.code) / "cover_original.webp",
        ):
            if candidate.exists():
                return candidate
        return None

    def build_delivery(self, sku: SalesSKU) -> SkuDeliveryResult:
        book = self.db.get_book(sku.book_id)
        if not book:
            raise CatalogError("SKU 对应的书目不存在")
        productions = []
        pdf_files: list[Path] = []
        for item in sku.items:
            product = self.db.get_product(item.product_id)
            if not product:
                raise CatalogError(f"分册不存在：{item.product_id}")
            productions.append(product)
            found = self._find_pdfs(product, item.edition)
            if not found:
                raise CatalogError(f"{product.volume_label or product.title} 没有可交付 PDF")
            pdf_files.extend(found)

        sku_dir = self.paths.sku_dir(sku.sku_code)
        subdirs = self.paths.sku_subdirs(sku_dir)
        image_files = sorted(
            p for p in subdirs["images"].glob("*.png") if p.is_file()
        )
        warnings: list[str] = []
        if not image_files:
            warnings.append("尚未生成 SKU 商品图片")
        if not book.cover_image or not Path(book.cover_image).exists():
            warnings.append("没有可核实的原版封面；商品图片必须使用自主设计封面并明确标注")
        if any(p.status not in ("packaged", "completed", "ready") for p in productions):
            warnings.append("有分册尚未完成生产/复核")

        files: list[dict[str, object]] = []
        for path in [*pdf_files, *image_files]:
            files.append(self._file_entry(path, "pdf" if path.suffix.lower() == ".pdf" else "product_image"))
        from .product_manager import sha256_file

        total_bytes = sum(int(f["size"]) for f in files)
        acceptance_checks = [
            {
                "id": "pdfs",
                "label": "每个分册都有最终 PDF",
                "status": "passed" if len(pdf_files) >= max(1, len(productions)) else "failed",
            },
            {
                "id": "images",
                "label": "SKU 独立商品图片（7 张）",
                "status": "passed" if len(image_files) >= 7 else "failed",
            },
            {
                "id": "volume_identity",
                "label": "套装含两个及以上分册且不重复",
                "status": "passed"
                if sku.kind == "single"
                or len({item.product_id for item in sku.items}) >= 2
                else "failed",
            },
            {
                "id": "price",
                "label": "价格合法（人民币两位小数）",
                "status": "passed" if sku.price_cents > 0 and sku.original_price_cents >= 0 else "failed",
            },
            {
                "id": "cover",
                "label": "原版封面已核实或明确使用自主设计封面",
                "status": "passed" if book.source_verified else "warning",
            },
            {
                "id": "cloud",
                "label": "云端上传与分享",
                "status": "pending",
            },
        ]
        passed = all(c["status"] in ("passed", "pending") for c in acceptance_checks)
        status = "ready" if passed and len(image_files) >= 7 else "needs_review"

        info = {
            "generated_at": utc_now(),
            "sku": sku.model_dump(mode="json"),
            "totals": self.sku_totals(sku),
            "book": book.model_dump(mode="json"),
            "productions": [p.model_dump(mode="json") for p in productions],
            "files": files,
            "total_files": len(files),
            "total_bytes": total_bytes,
            "warnings": warnings,
            "acceptance": acceptance_checks,
            "status": status,
        }
        info_json = subdirs["info"] / "商品信息.json"
        info_json.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        intro = subdirs["info"] / "商品介绍.md"
        intro.write_text(self._intro_markdown(sku, book, productions), encoding="utf-8")
        manifest = subdirs["delivery"] / "文件清单.json"
        manifest.write_text(
            json.dumps(
                {
                    "sku_code": sku.sku_code,
                    "book_code": book.code,
                    "files": files,
                    "note": "套装只记录对生产文件的引用，不复制 PDF 数据。",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        delivery_text = subdirs["delivery"] / "商品交付信息.txt"
        lines = [
            f"商品：{sku.name}",
            f"SKU：{sku.sku_code}",
            f"售价：¥{cents_to_yuan(sku.price_cents)}",
            f"包含：{len(productions)} 个分册",
            "",
        ]
        for item in sku.items:
            product = next(p for p in productions if p.id == item.product_id)
            lines.append(
                f"- {product.volume_label or product.title}（{EDITIONS.get(item.edition, item.edition)}）"
            )
        lines.extend(["", "交付文件："])
        for entry in files:
            lines.append(f"- {entry['name']}  {entry['size']} bytes")
        lines.append(f"- {intro.name}（商品介绍）")
        lines.extend(["", "说明：套装复用已生产的 PDF，不重复调用 AI 或重新生成 PDF。"])
        delivery_text.write_text("\n".join(lines), encoding="utf-8")
        acceptance = subdirs["delivery"] / "验收记录.json"
        acceptance.write_text(
            json.dumps(
                {
                    "sku_code": sku.sku_code,
                    "checked_at": utc_now(),
                    "checks": acceptance_checks,
                    "overall": status,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self.db.update_sku(
            sku.id, image_dir=str(subdirs["images"]), status=status
        )
        self.write_book_metadata(book)
        return SkuDeliveryResult(
            sku_dir=sku_dir,
            manifest=manifest,
            info_json=info_json,
            delivery_text=delivery_text,
            acceptance=acceptance,
            files=files,
            warnings=warnings,
            status=status,
        )

    def _intro_markdown(self, sku: SalesSKU, book: Book, productions: list[Product]) -> str:
        info = discount_info(sku)
        lines = [
            f"# {sku.name}",
            "",
            f"- SKU：{sku.sku_code}",
            f"- 书目：{book.title}（{book.code}）",
            f"- 售价：¥{info['price']}",
        ]
        if info["original_price_cents"] > info["price_cents"]:
            lines.append(f"- 原价：¥{info['original_price']}，优惠 ¥{info['discount_amount']}")
        lines.extend(["", sku.description.strip() or "逐题详解，独立复核，A4 正式排版。", "", "## 包含分册", ""])
        for item in sku.items:
            product = next((p for p in productions if p.id == item.product_id), None)
            if not product:
                continue
            lines.append(
                f"- **{product.volume_label or product.title}**：{product.chapter_count} 章 / "
                f"{product.exercise_count} 道习题 / {product.verified_count} 道已复核 "
                f"（{EDITIONS.get(item.edition, item.edition)}）"
            )
        lines.extend(
            [
                "",
                "## 交付内容",
                "",
                "- 每个分册的详细解析 PDF（A4、可打印）",
                "- 本 SKU 的 7 张商品图片",
                "- 商品信息、文件清单与验收记录",
                "",
                "> 本商品为原创解析作品，仅供学习参考。"
                "未取得出版社原版封面授权时，商品图片使用自主设计视觉封面并明确标注。",
            ]
        )
        return "\n".join(lines) + "\n"

    def _file_entry(self, path: Path, kind: str) -> dict[str, object]:
        from .product_manager import sha256_file

        return {
            "name": path.name,
            "kind": kind,
            "path": str(path),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }

    def generate_images(self, sku: SalesSKU, *, cover_override: Path | None = None):
        from .sku_image_generator import SkuImageGenerator

        book = self.db.get_book(sku.book_id)
        if not book:
            raise CatalogError("书目不存在")
        productions = []
        pdfs: dict[str, Path] = {}
        for item in sku.items:
            product = self.db.get_product(item.product_id)
            if not product:
                raise CatalogError(f"分册不存在：{item.product_id}")
            productions.append(product)
            found = self._find_pdfs(product, item.edition)
            if not found:
                raise CatalogError(f"{product.volume_label or product.title} 没有 PDF")
            pdfs[product.id] = found[0]
        return SkuImageGenerator(self.settings, self.paths).generate(
            sku=sku,
            book=book,
            productions=productions,
            pdfs=pdfs,
            cover=cover_override or self._cover(book),
        )

    def write_book_metadata(self, book: Book) -> Path:
        book_dir = self.paths.book_dir(book.code)
        productions = self.db.list_productions(book.id)
        payload = {
            "title": book.title,
            "original_title": book.original_title,
            "author": book.authors,
            "translator": book.translators,
            "publisher": book.publisher,
            "edition": book.edition,
            "publication_year": book.publication_year,
            "isbn_10": book.isbn_10,
            "isbn_13": book.isbn_13,
            "volumes": book.volumes,
            "language": book.language,
            "subject": book.subject.value,
            "cover_image": book.cover_image,
            "cover_source_url": book.cover_source_url,
            "source_license": book.source_license,
            "source_verified": book.source_verified,
            "verification_status": book.verification_status,
            "catalog_code": book.code,
            "productions": [
                {
                    "product_id": p.id,
                    "volume_label": p.volume_label,
                    "volume_number": p.volume_number,
                    "volume_code": p.volume_code,
                    "status": p.status,
                    "exercise_count": p.exercise_count,
                    "verified_count": p.verified_count,
                }
                for p in productions
            ],
            "updated_at": book.updated_at,
        }
        path = book_dir / "book_metadata.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    # -------------------------------------------------------------------- cloud

    def upload_sku(self, sku: SalesSKU, *, verify: bool = True):
        if not self.netdisk:
            raise CatalogError("百度网盘客户端未配置，不能上传")
        delivery = self.build_delivery(sku)
        book = self.db.get_book(sku.book_id)
        if not book:
            raise CatalogError("书目不存在")
        remote_root = f"{self.netdisk.effective_root}/{book.code}/{sku.sku_code}"
        folders = {"pdf": "01_习题详解", "product_image": "02_商品图片"}
        for folder in ("01_习题详解", "02_商品图片", "03_商品信息"):
            self.netdisk.ensure_dir(f"{remote_root}/{folder}")
        uploaded = []
        for entry in delivery.files:
            local = Path(str(entry["path"]))
            folder = folders.get(str(entry["kind"]), "02_商品图片")
            remote = f"{remote_root}/{folder}/{local.name}"
            uploaded.append(
                self.netdisk.upload_file(
                    local, remote, product_id=sku.id, progress=None
                )
            )
        subdirs = self.paths.sku_subdirs(self.paths.sku_dir(sku.sku_code))
        for local in sorted(p for p in subdirs["info"].glob("*") if p.is_file()):
            remote = f"{remote_root}/03_商品信息/{local.name}"
            uploaded.append(
                self.netdisk.upload_file(
                    local, remote, product_id=sku.id, progress=None
                )
            )
        if verify:
            return self.verify_sku_uploads(sku)
        return uploaded

    def verify_sku_uploads(self, sku: SalesSKU) -> list:
        """Verify recorded cloud uploads by size and optional download hash."""
        if not self.netdisk:
            raise CatalogError("百度网盘客户端未配置，不能校验")
        records = self.db.list_uploads(sku.id)
        full_hash = bool(self.settings.get("netdisk.verify_download_hash", False))
        for record in records:
            try:
                meta = self.netdisk.stat(record.remote_path)
                remote_size = int(meta.get("size", -1))
                if remote_size != int(record.size):
                    self.db.update_upload(
                        record.id,
                        status="failed",
                        error=f"云端大小不一致：{remote_size} != {record.size}",
                    )
                    continue
                if full_hash:
                    digest = self.netdisk.download_sha256(record.remote_path)
                    if digest != record.sha256:
                        self.db.update_upload(
                            record.id,
                            status="failed",
                            error="下载内容的 SHA-256 与本地文件不一致",
                        )
                        continue
                self.db.update_upload(
                    record.id,
                    status="verified",
                    error="",
                    remote_md5=str(meta.get("md5", "") or ""),
                )
            except Exception as exc:  # noqa: BLE001
                self.db.update_upload(
                    record.id, status="failed", error=str(exc)[:500]
                )
        return self.db.list_uploads(sku.id)

    def create_share(self, sku: SalesSKU, **kwargs):
        if not self.netdisk:
            raise CatalogError("百度网盘客户端未配置，不能分享")
        book = self.db.get_book(sku.book_id)
        if not book:
            raise CatalogError("书目不存在，不能分享")
        remote_dir = f"{self.netdisk.effective_root}/{book.code}/{sku.sku_code}"
        return self.netdisk.create_share_dir(
            remote_dir, product_id=sku.id, **kwargs
        )
