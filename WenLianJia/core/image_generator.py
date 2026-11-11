"""Generate the product image set from real production data.

No image model is required for the deterministic edition: backgrounds, layout
and all Chinese text are drawn locally with Pillow, while page previews are
rendered from the actual final PDF.  Every number shown is read from the
database or the generated files.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from models.schemas import Product, utc_now
from .config import SettingsManager
from .db import Database
from .logging_setup import get_logger
from .paths import AppPaths
from .watermark import resolve_font

logger = get_logger("images")

WIDTH, HEIGHT = 1080, 1440
NAVY = (16, 42, 67)
NAVY_2 = (28, 72, 102)
CREAM = (247, 243, 234)
INK = (28, 32, 36)
GRAY = (96, 105, 112)
GOLD = (201, 162, 39)
RED = (164, 58, 46)
WHITE = (255, 255, 255)

_FEATURES = (
    ("逐题详解", "每道题都有完整推导，不只给答案"),
    ("独立复核", "第二遍独立求解，再对照裁决"),
    ("公式排版", "LaTeX 排版，上下标与推导清晰"),
    ("方法总结", "提炼考点、思路与常见错误"),
)


class ImageGenerationError(RuntimeError):
    pass


@dataclass
class ProductImageResult:
    files: list[Path] = field(default_factory=list)
    manifest: Path | None = None
    warnings: list[str] = field(default_factory=list)
    mode: str = "local_design"


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = (
        r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
    )
    for candidate in candidates:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size=size, index=0)
            except OSError:
                continue
    raise ImageGenerationError("未找到可用中文字体，无法生成商品图片")


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for char in str(text):
        if char == "\n":
            lines.append(current)
            current = ""
            continue
        trial = current + char
        if draw.textlength(trial, font=font) <= width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = char
    if current:
        lines.append(current)
    return lines


def _draw_centered(
    draw: ImageDraw.ImageDraw,
    text: str,
    y: int,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
    width: int = WIDTH,
    line_gap: int = 12,
    max_lines: int = 3,
) -> int:
    lines = _wrap(draw, text, font, width)[:max_lines]
    for line in lines:
        line_width = draw.textlength(line, font=font)
        draw.text(((WIDTH - line_width) / 2, y), line, font=font, fill=fill)
        y += font.size + line_gap
    return y


def _draw_left(
    draw: ImageDraw.ImageDraw,
    text: str,
    x: int,
    y: int,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
    width: int,
    line_gap: int = 8,
    max_lines: int = 6,
) -> int:
    for line in _wrap(draw, text, font, width)[:max_lines]:
        draw.text((x, y), line, font=font, fill=fill)
        y += font.size + line_gap
    return y


def _draw_title(
    draw: ImageDraw.ImageDraw,
    text: str,
    x: int,
    y: int,
    fill: tuple[int, int, int],
    width: int,
    size: int = 58,
    min_size: int = 38,
) -> int:
    """Draw a heading, preferring one line, then a balanced two-line split."""
    text = str(text).strip()
    for candidate in range(size, min_size - 1, -2):
        font = _font(candidate, bold=True)
        if draw.textlength(text, font=font) <= width:
            draw.text((x, y), text, font=font, fill=fill)
            return y + candidate
        if len(text) >= 4:
            split = (len(text) + 1) // 2
            first, second = text[:split], text[split:]
            if (
                draw.textlength(first, font=font) <= width
                and draw.textlength(second, font=font) <= width
            ):
                draw.text((x, y), first, font=font, fill=fill)
                draw.text((x, y + candidate + 8), second, font=font, fill=fill)
                return y + 2 * candidate + 8
    font = _font(min_size, bold=True)
    lines = _wrap(draw, text, font, width)[:2]
    for index, line in enumerate(lines):
        draw.text((x, y + index * (min_size + 8)), line, font=font, fill=fill)
    return y + len(lines) * (min_size + 8)


def _gradient(width: int, height: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    image = Image.new("RGB", (width, height), top)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = tuple(int(top[i] * (1 - ratio) + bottom[i] * ratio) for i in range(3))
        draw.line([(0, y), (width, y)], fill=color)
    return image


def _paper_texture(image: Image.Image, seed: int = 7) -> Image.Image:
    rng = random.Random(seed)
    noise = Image.new("L", (image.width // 3, image.height // 3))
    pixels = noise.load()
    for y in range(noise.height):
        for x in range(noise.width):
            pixels[x, y] = rng.randint(118, 138)
    noise = noise.resize(image.size, Image.Resampling.BILINEAR).filter(ImageFilter.GaussianBlur(0.5))
    overlay = Image.new("RGB", image.size, (100, 94, 84))
    return Image.blend(image, Image.composite(overlay, image, noise), 0.08)


def _panel(image: Image.Image, box: tuple[int, int, int, int], fill=(255, 255, 255), radius=26) -> None:
    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(shadow)
    x0, y0, x1, y1 = box
    draw.rounded_rectangle((x0 + 8, y0 + 12, x1 + 8, y1 + 12), radius=radius, fill=(0, 0, 0, 40))
    shadow = shadow.filter(ImageFilter.GaussianBlur(12))
    image.alpha_composite(shadow)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def _pdf_page_image(document: fitz.Document, index: int, dpi: int = 140) -> Image.Image:
    page = document[index]
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _paste_page(
    canvas: Image.Image,
    page_image: Image.Image,
    box: tuple[int, int, int, int],
    *,
    shadow: bool = True,
) -> None:
    x0, y0, x1, y1 = box
    target_w, target_h = x1 - x0, y1 - y0
    scale = min(target_w / page_image.width, target_h / page_image.height)
    thumb = page_image.resize(
        (max(1, int(page_image.width * scale)), max(1, int(page_image.height * scale))),
        Image.Resampling.LANCZOS,
    )
    x = x0 + (target_w - thumb.width) // 2
    y = y0 + (target_h - thumb.height) // 2
    if shadow:
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        draw.rectangle((x - 6, y + 8, x + thumb.width + 14, y + thumb.height + 18), fill=(0, 0, 0, 55))
        layer = layer.filter(ImageFilter.GaussianBlur(10))
        canvas.alpha_composite(layer)
    canvas.paste(thumb, (x, y))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((x, y, x + thumb.width, y + thumb.height), outline=(255, 255, 255), width=3)


class ImageGenerator:
    def __init__(self, settings: SettingsManager, db: Database, paths: AppPaths):
        self.settings = settings
        self.db = db
        self.paths = paths

    def generate(
        self,
        product: Product,
        full_pdf: Path,
        quick_pdf: Path | None,
        deliverables: list[Path] | None = None,
    ) -> ProductImageResult:
        if not Path(full_pdf).exists():
            raise ImageGenerationError(f"最终 PDF 不存在：{full_pdf}")
        product_dir = self.paths.product_dir(product.sku, product.title)
        out_dir = self.paths.product_subdirs(product_dir)["images"]
        out_dir.mkdir(parents=True, exist_ok=True)
        warnings: list[str] = []
        stats = self._stats(product)
        files: list[Path] = []
        document = fitz.open(full_pdf)
        try:
            pages = self._select_pages(document)
            cover = _pdf_page_image(document, pages[0] if pages else 0, dpi=150)
            body = [_pdf_page_image(document, index, dpi=130) for index in pages[1:4]]
            jobs = [
                ("01_商品封面.png", self._cover(product, stats)),
                ("02_商品主图.png", self._main(product, stats, cover)),
                ("03_内容展示图.png", self._content(product, body)),
                ("04_商品详情图.png", self._details(product, stats)),
                ("05_商品特色图.png", self._features(product, stats)),
                ("06_文件内容说明图.png", self._file_list(product, stats, deliverables or [full_pdf, *([quick_pdf] if quick_pdf else [])])),
                ("07_品牌宣传图.png", self._brand(product)),
            ]
        finally:
            document.close()

        for filename, image in jobs:
            target = out_dir / filename
            image.convert("RGB").save(target, "PNG", optimize=True)
            files.append(target)
        manifest = out_dir / "商品图片清单.json"
        manifest.write_text(
            json.dumps(
                {
                    "product_id": product.id,
                    "sku": product.sku,
                    "title": product.title,
                    "generated_at": utc_now(),
                    "width": WIDTH,
                    "height": HEIGHT,
                    "mode": "local_design",
                    "source_pdf": str(full_pdf),
                    "files": [file.name for file in files],
                    "stats": stats,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        logger.info("generated %d product images for %s", len(files), product.sku)
        return ProductImageResult(files=files, manifest=manifest, warnings=warnings)

    # ------------------------------------------------------------------ data

    def _stats(self, product: Product) -> dict[str, object]:
        counts = self.db.exercise_status_counts(product.id)
        chapters = self.db.list_chapters(product.id)
        total = sum(counts.values())
        verified = counts.get("verified", 0) + counts.get("approved", 0)
        return {
            "total": total,
            "verified": verified,
            "review": counts.get("needs_review", 0),
            "chapters": len([chapter for chapter in chapters if chapter.level <= 1]) or len(chapters),
            "chapter_titles": [
                chapter.title for chapter in chapters if chapter.level <= 1
            ][:8],
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
            if len(selected) >= 4:
                break
        return selected

    # ----------------------------------------------------------------- images

    def _base_dark(self) -> Image.Image:
        image = _gradient(WIDTH, HEIGHT, (12, 33, 54), (31, 77, 105)).convert("RGBA")
        draw = ImageDraw.Draw(image, "RGBA")
        for radius in (560, 470, 380):
            draw.ellipse(
                (WIDTH - radius - 90, -radius // 2, WIDTH - 90 + radius, radius),
                outline=(255, 255, 255, 16),
                width=2,
            )
        return image

    def _base_cream(self) -> Image.Image:
        image = _paper_texture(Image.new("RGBA", (WIDTH, HEIGHT), CREAM + (255,)), seed=11)
        draw = ImageDraw.Draw(image, "RGBA")
        draw.rectangle((0, 0, WIDTH, 18), fill=NAVY)
        draw.rectangle((0, HEIGHT - 18, WIDTH, HEIGHT), fill=NAVY)
        # A quiet gold arc in the upper-right corner; never behind footer text.
        draw.arc((760, -180, 1180, 240), 20, 300, fill=(201, 162, 39, 55), width=3)
        draw.arc((820, -120, 1120, 180), 20, 300, fill=(201, 162, 39, 35), width=2)
        return image

    def _brand_mark(self, image: Image.Image, y: int = 86, dark: bool = True) -> None:
        draw = ImageDraw.Draw(image)
        brand = str(self.settings.get("app.brand", "全网同名爱做题的大肥鱼"))
        color = (235, 232, 224) if dark else NAVY
        draw.text((72, y), brand, font=_font(30, bold=True), fill=color)
        draw.line([(72, y + 50), (222, y + 50)], fill=GOLD, width=3)
        label = "教材习题解析 · 数字商品"
        label_width = draw.textlength(label, font=_font(22))
        draw.text((WIDTH - 72 - label_width, y + 6), label, font=_font(22), fill=color)

    def _cover(self, product: Product, stats: dict[str, object]) -> Image.Image:
        image = self._base_dark()
        self._brand_mark(image)
        draw = ImageDraw.Draw(image)
        title_font = _font(78, bold=True)
        y = 300
        lines = _wrap(draw, product.source_title or product.title, title_font, 900)[:3]
        for line in lines:
            draw.text((72, y), line, font=title_font, fill=WHITE)
            y += 100
        draw.rectangle((72, y + 30, 290, y + 38), fill=GOLD)
        y += 90
        draw.text((72, y), "习题答案与详细解析", font=_font(58, bold=True), fill=(244, 224, 160))
        y += 100
        draw.text((72, y), "逐题详解 · 独立复核 · 公式排版", font=_font(30), fill=(210, 220, 226))
        y += 100
        chips = [
            f"{stats['total']} 道习题",
            f"{stats['chapters']} 章覆盖",
            f"{stats['verified']} 道复核",
        ]
        x = 72
        for chip in chips:
            width = int(draw.textlength(chip, font=_font(26, bold=True))) + 44
            draw.rounded_rectangle((x, y, x + width, y + 56), radius=28, fill=(255, 255, 255, 18), outline=(244, 224, 160, 120), width=1)
            draw.text((x + 22, y + 12), chip, font=_font(26, bold=True), fill=(244, 224, 160))
            x += width + 18
        _draw_left(
            draw,
            "保留原书页码，逐题给出考点、思路、推导与最终答案；所有解析均经过独立复核流程。",
            72,
            900,
            _font(25),
            (206, 218, 227),
            760,
            line_gap=12,
            max_lines=3,
        )
        draw.line([(72, 1090), (1008, 1090)], fill=(201, 162, 39, 120), width=2)
        draw.text((72, 1155), "整理品牌", font=_font(24), fill=(170, 186, 198))
        draw.text((72, 1195), str(self.settings.get("app.brand", "全网同名爱做题的大肥鱼")), font=_font(40, bold=True), fill=WHITE)
        draw.text((72, 1258), f"版本 v{product.version or '1.0'}  ·  以实际完成的解析 PDF 为准", font=_font(22), fill=(180, 194, 204))
        return image.convert("RGB")

    def _main(self, product: Product, stats: dict[str, object], cover: Image.Image) -> Image.Image:
        image = self._base_cream()
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, WIDTH, 250), fill=NAVY)
        self._brand_mark(image)
        draw.text((72, 330), "一本可以直接交付的", font=_font(38), fill=GRAY)
        _draw_title(draw, product.source_title or product.title, 72, 390, NAVY, 430, size=58)
        y = 620
        for line in ("逐题详解，不只给最终答案", "独立复核，标注待人工确认项", "A4 正式排版，中文与公式清晰"):
            draw.ellipse((72, y + 10, 92, y + 30), fill=GOLD)
            _draw_left(draw, line, 110, y, _font(27), INK, 400, max_lines=2, line_gap=4)
            y += 92
        _panel(image, (520, 300, 1010, 1000), fill=(255, 255, 255), radius=18)
        _paste_page(image, cover, (545, 325, 985, 975))
        draw.text((520, 1030), f"{stats['total']} 道习题 · {stats['chapters']} 章 · 完整解析", font=_font(28, bold=True), fill=NAVY)
        draw.text((72, 1120), "打开即可核对：章节、题目、公式与最终答案", font=_font(24), fill=GRAY)
        draw.text((72, 1330), "商品主图 · 以实际交付 PDF 内容为准", font=_font(22), fill=GRAY)
        return image.convert("RGB")

    def _content(self, product: Product, pages: list[Image.Image]) -> Image.Image:
        image = self._base_dark()
        self._brand_mark(image)
        draw = ImageDraw.Draw(image)
        draw.text((72, 240), "内容实拍", font=_font(64, bold=True), fill=WHITE)
        draw.text((72, 330), "从真实解析 PDF 渲染的页面预览", font=_font(28), fill=(196, 210, 220))
        if not pages:
            draw.text((72, 520), "暂无页面预览", font=_font(34), fill=(196, 210, 220))
            return image.convert("RGB")
        boxes = ((64, 450, 504, 1190), (576, 450, 1016, 1190))
        for index, page in enumerate(pages[:2]):
            _paste_page(image, page, boxes[index])
            draw = ImageDraw.Draw(image)
            draw.text((boxes[index][0], 1260), f"正文预览 {index + 1}", font=_font(24), fill=(196, 210, 220))
        draw.line([(72, 1330), (1008, 1330)], fill=(201, 162, 39, 120), width=2)
        draw.text((72, 1358), "公式、步骤、最终答案均来自已生成的 PDF", font=_font(24), fill=(210, 220, 226))
        return image.convert("RGB")

    def _details(self, product: Product, stats: dict[str, object]) -> Image.Image:
        image = self._base_cream()
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, WIDTH, 260), fill=NAVY)
        self._brand_mark(image)
        draw.text((72, 320), "内容范围", font=_font(58, bold=True), fill=NAVY)
        draw.text((72, 400), "按教材实际章节整理，数字来自生产数据库", font=_font(26), fill=GRAY)
        titles = [str(title) for title in (stats.get("chapter_titles") or [])][:4]
        if not titles:
            titles = ["教材章节"]
        if int(stats.get("chapters") or 0) > len(titles):
            draw.text((72, 444), f"共 {stats['chapters']} 章，以下展示前 {len(titles)} 章", font=_font(22), fill=GRAY)
        y = 500
        for index, title in enumerate(titles, start=1):
            draw.rounded_rectangle((72, y, 1008, y + 86), radius=14, fill=(255, 255, 255))
            draw.text((96, y + 18), f"{index:02d}", font=_font(28, bold=True), fill=GOLD)
            _draw_left(draw, title, 158, y + 25, _font(27, bold=True), INK, 760, max_lines=1)
            y += 98
        box_y = y + 16
        draw.rounded_rectangle((72, box_y, 1008, box_y + 174), radius=18, fill=NAVY)
        for offset, (label, value) in enumerate(
            (("收录习题", stats["total"]), ("已自动复核", stats["verified"]), ("待人工复核", stats["review"]))
        ):
            x = 110 + offset * 300
            draw.text((x, box_y + 28), str(value), font=_font(50, bold=True), fill=(244, 224, 160))
            draw.text((x, box_y + 96), label, font=_font(23), fill=(214, 224, 232))
        feature_y = box_y + 206
        if feature_y < 1160:
            draw.text((72, feature_y), "每道题的完整结构", font=_font(28, bold=True), fill=NAVY)
            items = ("题目原文与页码", "考点与解题思路", "详细推导过程", "最终答案与总结")
            for index, label in enumerate(items):
                col, row = index % 2, index // 2
                x = 72 + col * 474
                top = feature_y + 50 + row * 84
                draw.rounded_rectangle((x, top, x + 456, top + 70), radius=12, fill=(255, 255, 255))
                draw.text((x + 18, top + 16), f"{index + 1:02d}", font=_font(22, bold=True), fill=GOLD)
                _draw_left(draw, label, x + 64, top + 18, _font(24), INK, 370, max_lines=1)
        draw.text((72, 1352), "详情图 · 章节与数量均可追溯", font=_font(22), fill=GRAY)
        return image.convert("RGB")

    def _features(self, product: Product, stats: dict[str, object]) -> Image.Image:
        image = self._base_dark()
        self._brand_mark(image)
        draw = ImageDraw.Draw(image)
        draw.text((72, 250), "为什么选择这份解析", font=_font(56, bold=True), fill=WHITE)
        y = 370
        for index, (title, detail) in enumerate(_FEATURES, start=1):
            _panel(image, (64, y, 1016, y + 180), fill=(255, 255, 255), radius=18)
            draw = ImageDraw.Draw(image)
            draw.text((98, y + 40), f"{index:02d}", font=_font(44, bold=True), fill=GOLD)
            draw.text((190, y + 34), title, font=_font(38, bold=True), fill=NAVY)
            _draw_left(draw, detail, 190, y + 94, _font(26), GRAY, 760, max_lines=2)
            y += 205
        draw.text((72, 1280), f"已自动复核 {stats['verified']} 道 · 逐题保留解题链条", font=_font(26), fill=(214, 224, 232))
        draw.text((72, 1330), str(self.settings.get("app.brand", "全网同名爱做题的大肥鱼")), font=_font(28, bold=True), fill=(244, 224, 160))
        return image.convert("RGB")

    def _file_list(
        self, product: Product, stats: dict[str, object], deliverables: list[Path]
    ) -> Image.Image:
        image = self._base_cream()
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, WIDTH, 250), fill=NAVY)
        self._brand_mark(image)
        draw.text((72, 320), "购买后获得", font=_font(58, bold=True), fill=NAVY)
        draw.text((72, 400), "以下为实际交付文件，不含未生成的内容", font=_font(26), fill=GRAY)
        y = 480
        names = [Path(path).name for path in deliverables if Path(path).exists()]
        if not names:
            names = ["完整解析 PDF", "答案速查 PDF"]
        names = names[:5]
        names.extend(["商品图片（7 张 PNG）", "商品信息与交付记录"])
        for name in names:
            draw.rounded_rectangle((72, y, 1008, y + 88), radius=12, fill=(255, 255, 255))
            draw.ellipse((96, y + 28, 128, y + 60), fill=(201, 162, 39, 55))
            draw.line([(104, y + 44), (110, y + 51), (121, y + 37)], fill=NAVY, width=3)
            _draw_left(draw, name, 156, y + 26, _font(24), INK, 820, max_lines=1)
            y += 104
        draw.rounded_rectangle((72, y + 24, 1008, y + 190), radius=14, fill=(255, 255, 255))
        draw.text((100, y + 50), f"共 {stats['total']} 道习题解析", font=_font(30, bold=True), fill=NAVY)
        draw.text((100, y + 108), "PDF 文件 + 商品图片 + 商品信息与交付记录", font=_font(24), fill=GRAY)
        draw.text((72, 1340), "文件说明图 · 以最终交付清单为准", font=_font(22), fill=GRAY)
        return image.convert("RGB")

    def _brand(self, product: Product) -> Image.Image:
        image = self._base_dark()
        draw = ImageDraw.Draw(image)
        draw.ellipse((720, 180, 1040, 500), outline=(201, 162, 39, 90), width=3)
        draw.ellipse((790, 250, 970, 430), outline=(201, 162, 39, 45), width=2)
        draw.text((72, 300), "爱做题的大肥鱼", font=_font(74, bold=True), fill=WHITE)
        draw.text((72, 400), "全网同名", font=_font(36), fill=(244, 224, 160))
        draw.rectangle((72, 500, 420, 508), fill=GOLD)
        _draw_left(
            draw,
            "把教材习题做成可管理、可审核、\n可交付的数字解析作品。",
            72,
            570,
            _font(32),
            (216, 226, 234),
            850,
            line_gap=12,
            max_lines=2,
        )
        y = 760
        for title, detail in (
            ("原创解析", "逐题推导，不拼凑答案"),
            ("质量优先", "独立复核后再交付"),
            ("可追溯", "每道题保留页码与记录"),
        ):
            draw.text((72, y), title, font=_font(32, bold=True), fill=(244, 224, 160))
            draw.text((280, y + 4), detail, font=_font(26), fill=(214, 224, 232))
            y += 78
        draw.text((72, 1180), "整理品牌", font=_font(24), fill=(170, 186, 198))
        draw.text((72, 1220), str(self.settings.get("app.brand", "全网同名爱做题的大肥鱼")), font=_font(44, bold=True), fill=WHITE)
        draw.text((72, 1320), "本图由程序排版，文字内容可核验", font=_font(22), fill=(180, 194, 204))
        return image.convert("RGB")
