"""Add a configurable diagonal brand watermark to every PDF page.

The watermark is drawn deterministically with Pillow using a real Chinese font
and inserted as an image overlay by PyMuPDF.  Image-generation models are never
asked to draw text or a watermark.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image, ImageColor, ImageDraw, ImageFont

from .config import SettingsManager
from .logging_setup import get_logger

logger = get_logger("watermark")

_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
)


class WatermarkError(RuntimeError):
    pass


@dataclass
class WatermarkResult:
    path: Path
    pages: int
    text: str
    angle: float
    opacity: float


def resolve_font(settings: SettingsManager) -> Path:
    configured = str(settings.get("watermark.font", "") or "").strip()
    if configured:
        path = Path(configured)
        if path.exists():
            return path
        if path.name.lower().endswith((".ttf", ".ttc", ".otf")):
            raise WatermarkError(f"水印字体文件不存在：{path}")
    for candidate in _FONT_CANDIDATES:
        path = Path(candidate)
        if path.exists():
            return path
    raise WatermarkError("未找到支持中文的水印字体，请在设置中指定字体文件")


def _hex_rgba(color: str, opacity: float) -> tuple[int, int, int, int]:
    try:
        red, green, blue = ImageColor.getrgb(color)
    except ValueError as exc:
        raise WatermarkError(f"水印颜色格式无效：{color}") from exc
    alpha = max(0, min(255, int(round(opacity * 255))))
    return red, green, blue, alpha


def _text_bitmap(text: str, font: ImageFont.FreeTypeFont, rgba: tuple[int, int, int, int]) -> Image.Image:
    padding = 12
    probe = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    draw = ImageDraw.Draw(probe)
    box = draw.textbbox((0, 0), text, font=font, stroke_width=0)
    width = max(1, box[2] - box[0] + padding * 2)
    height = max(1, box[3] - box[1] + padding * 2)
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.text((padding - box[0], padding - box[1]), text, font=font, fill=rgba)
    return layer


class Watermarker:
    def __init__(self, settings: SettingsManager):
        self.settings = settings

    def apply(self, source: Path, destination: Path | None = None) -> WatermarkResult:
        source = Path(source)
        if not source.exists():
            raise WatermarkError(f"PDF 不存在：{source}")
        destination = Path(destination) if destination else source.with_name(
            source.stem + "_watermarked" + source.suffix
        )
        if destination.resolve() == source.resolve():
            raise WatermarkError("输出文件不能覆盖输入文件")
        text = str(self.settings.get("watermark.text", "全网同名爱做题的大肥鱼")).strip()
        if not text:
            raise WatermarkError("水印文字为空")
        font_path = resolve_font(self.settings)
        font_size_pt = float(self.settings.get("watermark.font_size", 42))
        color = str(self.settings.get("watermark.color", "#9AA0A6"))
        opacity = float(self.settings.get("watermark.opacity", 0.18))
        angle = float(self.settings.get("watermark.rotation", 35))
        pos_x = float(self.settings.get("watermark.position_x", 0.5))
        pos_y = float(self.settings.get("watermark.position_y", 0.5))
        repeat = bool(self.settings.get("watermark.tile", False))
        gap_points = float(self.settings.get("watermark.tile_gap", 260))
        zoom = 2.0
        rgba = _hex_rgba(color, opacity)

        try:
            font = ImageFont.truetype(str(font_path), max(8, int(round(font_size_pt * zoom))))
        except OSError as exc:
            raise WatermarkError(f"无法加载水印字体：{font_path}") from exc

        document = fitz.open(source)
        if document.is_encrypted:
            document.close()
            raise WatermarkError("PDF 已加密，无法添加水印")
        try:
            for page in document:
                page_width = page.rect.width
                page_height = page.rect.height
                overlay = self._page_overlay(
                    page_width,
                    page_height,
                    text,
                    font,
                    rgba,
                    angle,
                    pos_x,
                    pos_y,
                    repeat,
                    gap_points,
                    zoom,
                )
                buffer = io.BytesIO()
                overlay.save(buffer, "PNG", optimize=True)
                page.insert_image(
                    page.rect,
                    stream=buffer.getvalue(),
                    overlay=True,
                    keep_proportion=False,
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            document.save(str(destination), garbage=3, deflate=True)
            pages = document.page_count
        finally:
            document.close()
        logger.info("watermarked %s -> %s (%d pages)", source.name, destination.name, pages)
        return WatermarkResult(
            path=destination,
            pages=pages,
            text=text,
            angle=angle,
            opacity=opacity,
        )

    @staticmethod
    def _page_overlay(
        width_pt: float,
        height_pt: float,
        text: str,
        font: ImageFont.FreeTypeFont,
        rgba: tuple[int, int, int, int],
        angle: float,
        pos_x: float,
        pos_y: float,
        repeat: bool,
        gap_points: float,
        zoom: float,
    ) -> Image.Image:
        width = max(1, int(round(width_pt * zoom)))
        height = max(1, int(round(height_pt * zoom)))
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        tile = _text_bitmap(text, font, rgba).rotate(
            angle, expand=True, resample=Image.Resampling.BICUBIC
        )
        if repeat:
            step_x = max(1, tile.width + int(gap_points * zoom))
            step_y = max(1, tile.height + int(gap_points * zoom))
            for y in range(-tile.height, height + tile.height, step_y):
                for x in range(-tile.width, width + tile.width, step_x):
                    canvas.alpha_composite(tile, (x, y))
        else:
            x = int(width * max(0.0, min(1.0, pos_x)) - tile.width / 2)
            y = int(height * max(0.0, min(1.0, pos_y)) - tile.height / 2)
            canvas.alpha_composite(tile, (x, y))
        return canvas
