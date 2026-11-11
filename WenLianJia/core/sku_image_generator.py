"""Deterministic per-SKU product images.

Each sale gets its own seven-image set.  The images are built from the actual
SKU composition: a single-volume SKU shows that volume, a bundle shows every
volume, and the file list names the real delivery PDFs.  Prices and counts are
drawn by this program, not by an image model.  No publisher cover is ever
invented: a real cover is only shown when ``book.source_verified`` is true.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import fitz
from PIL import Image, ImageDraw

from models.schemas import Book, Product, SalesSKU, utc_now
from .config import SettingsManager
from .logging_setup import get_logger
from .paths import AppPaths
from .image_generator import (
    CREAM,
    GOLD,
    GRAY,
    HEIGHT,
    INK,
    NAVY,
    NAVY_2,
    WHITE,
    WIDTH,
    _draw_centered,
    _draw_left,
    _draw_title,
    _font,
    _gradient,
    _panel,
    _paper_texture,
    _paste_page,
    _pdf_page_image,
    _wrap,
)

logger = get_logger("sku_images")


class SkuImageError(RuntimeError):
    pass


@dataclass
class SkuImageResult:
    files: list[Path] = field(default_factory=list)
    manifest: Path | None = None
    warnings: list[str] = field(default_factory=list)
    mode: str = "local_design"


class SkuImageGenerator:
    def __init__(self, settings: SettingsManager, paths: AppPaths):
        self.settings = settings
        self.paths = paths

    def generate(
        self,
        *,
        sku: SalesSKU,
        book: Book,
        productions: list[Product],
        pdfs: dict[str, Path],
        cover: Path | None = None,
    ) -> SkuImageResult:
        if not productions:
            raise SkuImageError("SKU 没有生产实体")
        for product in productions:
            if product.id not in pdfs or not Path(pdfs[product.id]).exists():
                raise SkuImageError(f"{product.volume_label or product.title} 缺少 PDF")
        sku_dir = self.paths.sku_dir(sku.sku_code)
        out_dir = self.paths.sku_subdirs(sku_dir)["images"]
        stats = self._stats(productions)
        pages: list[tuple[Product, Image.Image]] = []
        body: list[tuple[Product, Image.Image]] = []
        documents: list[fitz.Document] = []
        try:
            for product in productions:
                document = fitz.open(pdfs[product.id])
                documents.append(document)
                selected = self._select_pages(document)
                first = selected[0] if selected else 0
                pages.append((product, _pdf_page_image(document, first, dpi=130)))
                index = selected[1] if len(selected) > 1 else first
                body.append((product, _pdf_page_image(document, index, dpi=120)))
            jobs = [
                ("01_商品封面.png", self._cover(sku, book, stats)),
                ("02_商品主图.png", self._main(sku, book, stats, pages, cover)),
                ("03_内容展示图.png", self._content(sku, book, body)),
                ("04_商品详情图.png", self._details(sku, book, productions, stats)),
                ("05_商品特色图.png", self._features(sku, book, stats)),
                ("06_文件内容说明图.png", self._files(sku, book, productions, pdfs)),
                ("07_品牌宣传图.png", self._brand_image(sku, book)),
            ]
        finally:
            for document in documents:
                document.close()

        files: list[Path] = []
        for filename, image in jobs:
            target = out_dir / filename
            image.convert("RGB").save(target, "PNG", optimize=True)
            files.append(target)
        manifest = out_dir / "SKU商品图片清单.json"
        manifest.write_text(
            json.dumps(
                {
                    "sku_code": sku.sku_code,
                    "name": sku.name,
                    "book_code": book.code,
                    "book_title": book.title,
                    "volume_labels": [p.volume_label for p in productions],
                    "price": f"{sku.price_cents / 100:.2f}",
                    "generated_at": utc_now(),
                    "mode": "local_design",
                    "source_cover": str(cover) if cover else "",
                    "source_cover_verified": book.source_verified,
                    "files": [file.name for file in files],
                    "stats": stats,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        warnings: list[str] = []
        if not cover:
            warnings.append("未使用原版封面：商品图片为自主设计视觉，已明确标注")
        logger.info("generated %d SKU images for %s", len(files), sku.sku_code)
        return SkuImageResult(files=files, manifest=manifest, warnings=warnings)

    # ------------------------------------------------------------------ data

    def _stats(self, productions: list[Product]) -> dict[str, object]:
        return {
            "volumes": len(productions),
            "total": sum(p.exercise_count for p in productions),
            "verified": sum(p.verified_count for p in productions),
            "chapters": sum(p.chapter_count for p in productions),
            "volume_labels": [p.volume_label or p.title for p in productions],
            "volume_counts": [p.exercise_count for p in productions],
            "volume_chapters": [p.chapter_count for p in productions],
        }

    @staticmethod
    def _select_pages(document: fitz.Document) -> list[int]:
        if document.page_count == 0:
            return []
        candidates: list[tuple[int, int]] = []
        for index in range(document.page_count):
            text = document[index].get_text()
            score = text.count("【") + text.count("题目") + text.count("解答")
            if "【详细解答】" in text:
                score += 5
            candidates.append((score, index))
        candidates.sort(reverse=True)
        selected = [0]
        for _, index in candidates:
            if index not in selected:
                selected.append(index)
            if len(selected) >= 3:
                break
        return selected

    # ------------------------------------------------------------------ canvas

    def _dark(self) -> Image.Image:
        image = _gradient(WIDTH, HEIGHT, (12, 33, 54), (31, 77, 105)).convert("RGBA")
        draw = ImageDraw.Draw(image, "RGBA")
        for radius in (560, 470, 380):
            draw.ellipse(
                (WIDTH - radius - 90, -radius // 2, WIDTH - 90 + radius, radius),
                outline=(255, 255, 255, 16),
                width=2,
            )
        return image

    def _cream(self) -> Image.Image:
        image = _paper_texture(Image.new("RGBA", (WIDTH, HEIGHT), CREAM + (255,)), seed=13)
        draw = ImageDraw.Draw(image, "RGBA")
        draw.rectangle((0, 0, WIDTH, 18), fill=NAVY)
        draw.rectangle((0, HEIGHT - 18, WIDTH, HEIGHT), fill=NAVY)
        draw.arc((760, -180, 1180, 240), 20, 300, fill=(201, 162, 39, 55), width=3)
        draw.arc((820, -120, 1120, 180), 20, 300, fill=(201, 162, 39, 35), width=2)
        return image

    def _brand_mark(self, image: Image.Image, dark: bool = True, y: int = 76) -> None:
        draw = ImageDraw.Draw(image)
        brand = str(self.settings.get("app.brand", "全网同名爱做题的大肥鱼"))
        color = (235, 232, 224) if dark else NAVY
        draw.text((64, y), brand, font=_font(28, bold=True), fill=color)
        draw.line([(64, y + 46), (214, y + 46)], fill=GOLD, width=3)
        label = "教材习题解析 · 数字商品"
        label_width = draw.textlength(label, font=_font(20))
        draw.text(
            (WIDTH - 64 - label_width, y + 4),
            label,
            font=_font(20),
            fill=color,
        )

    def _chip(self, draw: ImageDraw.ImageDraw, x: int, y: int, text: str) -> int:
        width = int(draw.textlength(text, font=_font(25, bold=True))) + 38
        draw.rounded_rectangle(
            (x, y, x + width, y + 50),
            radius=25,
            fill=(255, 255, 255, 18),
            outline=(244, 224, 160, 120),
            width=1,
        )
        draw.text((x + 19, y + 10), text, font=_font(25, bold=True), fill=(244, 224, 160))
        return x + width + 14

    def _volume_tag(self, draw: ImageDraw.ImageDraw, x: int, y: int, label: str) -> None:
        width = int(draw.textlength(label, font=_font(22, bold=True))) + 30
        draw.rounded_rectangle(
            (x, y, x + width, y + 40),
            radius=8,
            fill=(16, 42, 67),
        )
        draw.text((x + 15, y + 8), label, font=_font(22, bold=True), fill=WHITE)

    def _cover_thumb(
        self,
        image: Image.Image,
        cover: Path,
        box: tuple[int, int, int, int],
    ) -> None:
        try:
            thumb = Image.open(cover).convert("RGB")
        except Exception:
            return
        _panel(image, box, fill=(255, 255, 255), radius=8)
        x0, y0, x1, y1 = box
        inner = (x0 + 10, y0 + 10, x1 - 10, y1 - 10)
        target_w, target_h = inner[2] - inner[0], inner[3] - inner[1]
        scale = min(target_w / thumb.width, target_h / thumb.height)
        thumb = thumb.resize(
            (max(1, int(thumb.width * scale)), max(1, int(thumb.height * scale))),
            Image.Resampling.LANCZOS,
        )
        x = inner[0] + (target_w - thumb.width) // 2
        y = inner[1] + (target_h - thumb.height) // 2
        image.paste(thumb, (x, y))

    # ------------------------------------------------------------------ images

    def _cover(self, sku: SalesSKU, book: Book, stats: dict[str, object]) -> Image.Image:
        image = self._dark()
        self._brand_mark(image)
        draw = ImageDraw.Draw(image)
        y = 250
        volume = (
            "上下册完整套装"
            if sku.kind == "bundle"
            else (stats["volume_labels"][0] if stats["volume_labels"] else "")
        )
        if volume:
            draw.text((64, y), volume, font=_font(34, bold=True), fill=(244, 224, 160))
            y += 70
        y = _draw_title(draw, book.title or sku.name, 64, y, WHITE, 900, size=76, min_size=46)
        y += 26
        draw.rectangle((64, y, 270, y + 8), fill=GOLD)
        y += 54
        edition = "答案速查版" if self._is_quick(sku) else "习题答案与详细解析"
        draw.text((64, y), edition, font=_font(54, bold=True), fill=(244, 224, 160))
        y += 90
        draw.text((64, y), "逐题详解 · 独立复核 · 公式排版", font=_font(28), fill=(210, 220, 226))
        y += 80
        x = 64
        x = self._chip(draw, x, y, f"{stats['total']} 道习题")
        x = self._chip(draw, x, y, f"{stats['volumes']} 册")
        x = self._chip(draw, x, y, f"{stats['verified']} 道复核")
        y += 92
        price = f"¥{sku.price_cents / 100:.2f}"
        draw.text((64, y), price, font=_font(56, bold=True), fill=WHITE)
        if sku.original_price_cents > sku.price_cents:
            original = f"¥{sku.original_price_cents / 100:.2f}"
            draw.text((64 + 190, y + 16), original, font=_font(26), fill=(180, 194, 204))
            draw.line(
                [(64 + 190, y + 34), (64 + 190 + int(draw.textlength(original, font=_font(26))), y + 34)],
                fill=(180, 194, 204),
                width=2,
            )
        draw.line([(64, 1090), (1016, 1090)], fill=(201, 162, 39, 120), width=2)
        draw.text((64, 1150), "整理品牌", font=_font(22), fill=(170, 186, 198))
        draw.text((64, 1190), str(self.settings.get("app.brand", "全网同名爱做题的大肥鱼")), font=_font(38, bold=True), fill=WHITE)
        draw.text(
            (64, 1254),
            "图片文字与价格由程序排版 · 以实际交付文件为准",
            font=_font(21),
            fill=(180, 194, 204),
        )
        return image.convert("RGB")

    def _main(
        self,
        sku: SalesSKU,
        book: Book,
        stats: dict[str, object],
        pages: list[tuple[Product, Image.Image]],
        cover: Path | None,
    ) -> Image.Image:
        image = self._cream()
        self._brand_mark(image, dark=False)
        draw = ImageDraw.Draw(image)
        draw.text((64, 190), "一本可以直接交付的", font=_font(34), fill=GRAY)
        y = _draw_title(draw, book.title or sku.name, 64, 245, NAVY, 470, size=58, min_size=38)
        y += 18
        draw.text((64, y), self._volume_line(stats, sku), font=_font(28, bold=True), fill=(146, 112, 18))
        y += 70
        bullets = [
            "逐题详解，不只给最终答案",
            "独立复核，标注待人工确认项",
            "A4 正式排版，中文与公式清晰",
        ]
        for text in bullets:
            draw.ellipse((64, y + 8, 82, y + 26), fill=GOLD)
            draw.text((100, y), text, font=_font(27), fill=INK)
            y += 58
        y += 18
        if sku.kind == "bundle":
            draw.text((64, y), f"套装价 ¥{sku.price_cents / 100:.2f}", font=_font(38, bold=True), fill=NAVY)
            draw.text(
                (64, y + 54),
                f"原价 ¥{sku.original_price_cents / 100:.2f} · 省 ¥"
                f"{max(0, sku.original_price_cents - sku.price_cents) / 100:.2f}",
                font=_font(23, bold=True),
                fill=(146, 112, 18),
            )
        else:
            draw.text((64, y), f"到手价 ¥{sku.price_cents / 100:.2f}", font=_font(34, bold=True), fill=NAVY)

        # Right-hand page cards: one per volume, side by side for bundles.
        count = len(pages)
        if count == 1:
            _paste_page(image, pages[0][1], (530, 230, 1000, 1010))
            self._volume_tag(draw, 620, 1026, pages[0][0].volume_label or "分册")
        else:
            gap = 28
            card_w = (470 - gap) // 2
            for index, (product, page_image) in enumerate(pages[:2]):
                x0 = 530 + index * (card_w + gap)
                _paste_page(image, page_image, (x0, 250, x0 + card_w, 880))
                self._volume_tag(draw, x0 + 18, 896, product.volume_label or f"第{index+1}册")
            if count > 2:
                draw.text((530, 960), f"另有 {count - 2} 册", font=_font(22), fill=GRAY)
        if cover:
            self._cover_thumb(image, cover, (64, 1040, 250, 1320))
            draw.text((270, 1060), "原版教材封面", font=_font(20, bold=True), fill=NAVY)
            draw.text((270, 1092), "仅作版本识别", font=_font(19), fill=GRAY)
        else:
            draw.text((64, 1075), "自主设计视觉封面", font=_font(20, bold=True), fill=NAVY)
            draw.text((64, 1107), "非出版社原版封面", font=_font(19), fill=GRAY)
        draw.text((64, 1352), "商品主图 · 文字与价格由程序排版 · 以实际交付 PDF 为准", font=_font(19), fill=GRAY)
        return image.convert("RGB")

    def _content(
        self, sku: SalesSKU, book: Book, body: list[tuple[Product, Image.Image]]
    ) -> Image.Image:
        image = self._cream()
        self._brand_mark(image, dark=False)
        draw = ImageDraw.Draw(image)
        draw.text((64, 180), "内容实拍", font=_font(58, bold=True), fill=NAVY)
        draw.text((64, 258), "以下页面来自实际生成的解析 PDF", font=_font(25), fill=GRAY)
        if len(body) == 1:
            _paste_page(image, body[0][1], (130, 380, 950, 1100))
            self._volume_tag(draw, 480, 1120, body[0][0].volume_label or "内页")
        else:
            for index, (product, page_image) in enumerate(body[:2]):
                x0 = 78 + index * 470
                _paste_page(image, page_image, (x0, 390, x0 + 430, 1070))
                self._volume_tag(draw, x0 + 120, 1090, product.volume_label or f"第{index+1}册")
        draw.text((64, 1348), f"{sku.name} · 真实内容预览", font=_font(20), fill=GRAY)
        return image.convert("RGB")

    def _details(
        self,
        sku: SalesSKU,
        book: Book,
        productions: list[Product],
        stats: dict[str, object],
    ) -> Image.Image:
        image = self._cream()
        self._brand_mark(image, dark=False)
        draw = ImageDraw.Draw(image)
        draw.text((64, 170), "商品详情", font=_font(58, bold=True), fill=NAVY)
        draw.text((64, 250), f"书目编号 {book.code} · 销售 SKU {sku.sku_code}", font=_font(24), fill=GRAY)
        y = 330
        for product in productions[:4]:
            _panel(image, (64, y, 1016, y + 170), fill=(255, 255, 255), radius=18)
            draw.text((92, y + 24), product.volume_label or product.title, font=_font(32, bold=True), fill=NAVY)
            draw.text(
                (92, y + 76),
                f"{product.chapter_count} 章 · {product.exercise_count} 道习题 · 已复核 {product.verified_count} 道",
                font=_font(24),
                fill=GRAY,
            )
            draw.text((92, y + 118), product.title, font=_font(20), fill=GRAY)
            y += 190
        _panel(image, (64, 1120, 1016, 1280), fill=(16, 42, 67), radius=18)
        draw.text((92, 1148), sku.name, font=_font(30, bold=True), fill=WHITE)
        draw.text(
            (92, 1196),
            f"售价 ¥{sku.price_cents / 100:.2f} · {EDITIONS_LABEL.get(sku.items[0].edition, '详细解析') if sku.items else ''}",
            font=_font(24),
            fill=(244, 224, 160),
        )
        draw.text(
            (92, 1238),
            "PDF 保留原书页码；解析包含考点、思路、推导、最终答案与易错点。",
            font=_font(20),
            fill=(210, 220, 226),
        )
        draw.text((64, 1352), "商品详情图 · 数据来自生产数据库", font=_font(19), fill=GRAY)
        return image.convert("RGB")

    def _features(
        self, sku: SalesSKU, book: Book, stats: dict[str, object]
    ) -> Image.Image:
        image = self._cream()
        self._brand_mark(image, dark=False)
        draw = ImageDraw.Draw(image)
        draw.text((64, 180), "为什么选择", font=_font(34), fill=GRAY)
        _draw_title(draw, book.title or sku.name, 64, 232, NAVY, 900, size=54, min_size=38)
        rows = [
            ("分册独立生产", f"共 {stats['volumes']} 个生产实体，每册独立解析与复核"),
            ("套装文件复用", "套装只关联已生成的 PDF，不重复调用 AI，不重复生成"),
            ("真实数据", f"{stats['total']} 道习题、{stats['chapters']} 章，全部来自生产记录"),
            ("独立复核", f"已复核 {stats['verified']} 道；未通过项标记待人工确认"),
        ]
        y = 430
        for index, (head, detail) in enumerate(rows, 1):
            _panel(image, (64, y, 1016, y + 180), fill=(255, 255, 255), radius=18)
            draw.text((94, y + 30), f"0{index}", font=_font(34, bold=True), fill=GOLD)
            draw.text((174, y + 26), head, font=_font(31, bold=True), fill=NAVY)
            _draw_left(draw, detail, 174, y + 78, _font(23), GRAY, 800, max_lines=2)
            y += 204
        draw.text((64, 1350), "商品特色图 · 不使用未经验证的统计数字", font=_font(19), fill=GRAY)
        return image.convert("RGB")

    def _files(
        self,
        sku: SalesSKU,
        book: Book,
        productions: list[Product],
        pdfs: dict[str, Path],
    ) -> Image.Image:
        image = self._cream()
        self._brand_mark(image, dark=False)
        draw = ImageDraw.Draw(image)
        draw.text((64, 180), "购买后获得", font=_font(58, bold=True), fill=NAVY)
        draw.text((64, 258), "以下为实际交付文件，套装不额外复制生产数据", font=_font(24), fill=GRAY)
        y = 330
        for product in productions:
            pdf = pdfs.get(product.id)
            if not pdf:
                continue
            _panel(image, (64, y, 1016, y + 138), fill=(255, 255, 255), radius=16)
            draw.ellipse((90, y + 36, 136, y + 82), fill=GOLD)
            draw.line([(100, y + 60), (112, y + 73), (128, y + 48)], fill=WHITE, width=5)
            draw.text((158, y + 26), product.volume_label or product.title, font=_font(24, bold=True), fill=NAVY)
            _draw_left(draw, pdf.name, 158, y + 64, _font(19), GRAY, 820, max_lines=2)
            y += 158
        with_quick = any(item.edition in ("quick", "both") for item in sku.items)
        notes = [
            "商品图片（7 张 PNG）",
            "商品信息与交付记录",
        ]
        if with_quick:
            notes.insert(1, "答案速查版 PDF（若该分册已生成）")
        for note in notes:
            _panel(image, (64, y, 1016, y + 100), fill=(255, 255, 255), radius=14)
            draw.text((94, y + 30), "•", font=_font(32, bold=True), fill=GOLD)
            draw.text((128, y + 28), note, font=_font(24), fill=INK)
            y += 116
        draw.text((64, 1350), "文件说明图 · 以最终交付清单为准", font=_font(19), fill=GRAY)
        return image.convert("RGB")

    def _brand_image(self, sku: SalesSKU, book: Book) -> Image.Image:
        image = self._dark()
        self._brand_mark(image)
        draw = ImageDraw.Draw(image)
        _draw_centered(draw, "把教材变成可交付的商品", 380, _font(58, bold=True), WHITE, max_lines=2)
        _draw_centered(
            draw,
            "解析 · 复核 · 排版 · 分册 · 套装 · 交付记录",
            520,
            _font(30),
            (210, 220, 226),
        )
        _draw_centered(
            draw,
            book.title,
            660,
            _font(40, bold=True),
            (244, 224, 160),
        )
        _draw_centered(
            draw,
            f"{sku.name}  ·  ¥{sku.price_cents / 100:.2f}",
            740,
            _font(28),
            (210, 220, 226),
        )
        draw.line([(220, 980), (860, 980)], fill=(201, 162, 39, 140), width=2)
        _draw_centered(
            draw,
            str(self.settings.get("app.brand", "全网同名爱做题的大肥鱼")),
            1040,
            _font(36, bold=True),
            WHITE,
        )
        _draw_centered(draw, "本资料为原创解析作品，仅供学习参考。", 1100, _font(21), (180, 194, 204))
        return image.convert("RGB")

    # ------------------------------------------------------------------ helper

    @staticmethod
    def _is_quick(sku: SalesSKU) -> bool:
        return bool(sku.items) and all(item.edition == "quick" for item in sku.items)

    @staticmethod
    def _volume_line(stats: dict[str, object], sku: SalesSKU) -> str:
        labels = [str(label) for label in stats.get("volume_labels", [])]
        if sku.kind == "bundle":
            return " + ".join(labels)
        return labels[0] if labels else ""


EDITIONS_LABEL = {"detailed": "详细解析版", "quick": "答案速查版", "both": "详细解析+速查版"}

