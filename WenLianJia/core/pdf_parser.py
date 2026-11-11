"""PDF inspection and extraction with native text and OCR fallback.

Design rules:
* every extracted character keeps its source page;
* a scanned page is OCRed, never guessed;
* the original page image is always retained for human or vision-model review;
* a corrupt/encrypted PDF fails loudly and leaves the source file untouched.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pymupdf as fitz
from PIL import Image

from .layout_analyzer import TextLine
from .logging_setup import get_logger
from .ocr_engine import OcrEngine, lines_to_text

logger = get_logger("pdf")

MATH_RE = re.compile(r"[=∫∑∏√±≤≥∞≈≠∂∇∈∉⊂⊆∪∩→←↔αβγδεζηθλμνξπρστφχψωΓΔΘΛΞΠΣΦΨΩ]")


class PdfError(RuntimeError):
    pass


class PdfEncryptedError(PdfError):
    pass


class PdfCorruptError(PdfError):
    pass


@dataclass
class PageArtifact:
    page_number: int
    width: float
    height: float
    text: str
    blocks: list[dict[str, Any]]
    is_scanned: bool
    ocr_confidence: float
    image_path: str
    has_images: bool
    has_formula: bool
    extracted_images: list[str] = field(default_factory=list)


@dataclass
class PdfParseResult:
    path: Path
    sha256: str
    page_count: int
    metadata: dict[str, Any]
    toc: list[dict[str, Any]]
    pages: list[PageArtifact]
    lines: list[TextLine]
    scanned_pages: list[int]
    ocr_backend: str
    output_dir: Path

    @property
    def has_text_layer(self) -> bool:
        return any(len(page.text.strip()) > 30 and not page.is_scanned for page in self.pages)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def pdf_inspect(path: Path) -> dict[str, Any]:
    """Cheap pre-flight used right after import; never renders all pages."""

    if not path.exists():
        raise PdfError(f"文件不存在：{path}")
    if path.stat().st_size == 0:
        raise PdfCorruptError("PDF 文件为空")
    try:
        document = fitz.open(path)
    except Exception as exc:
        raise PdfCorruptError(f"无法打开 PDF：{exc}") from exc
    try:
        if document.needs_pass:
            raise PdfEncryptedError("PDF 已加密，需要密码")
        page_count = document.page_count
        if page_count <= 0:
            raise PdfCorruptError("PDF 没有页面")
        sample_indices = sorted({0, page_count // 2, page_count - 1})
        text_chars = 0
        image_pages = 0
        for index in sample_indices:
            page = document.load_page(index)
            text_chars += len(page.get_text("text").strip())
            if page.get_images(full=True):
                image_pages += 1
        metadata = {key: value for key, value in (document.metadata or {}).items() if value}
        toc = document.get_toc(simple=True) or []
        return {
            "path": str(path),
            "size": path.stat().st_size,
            "page_count": page_count,
            "metadata": metadata,
            "toc_entries": len(toc),
            "text_layer": text_chars > 80,
            "scanned_likely": text_chars < 80 and image_pages > 0,
            "encrypted": False,
            "sha256": sha256_file(path),
        }
    finally:
        document.close()


def _pixmap_image(page: fitz.Page, dpi: int) -> Image.Image:
    pixmap = page.get_pixmap(dpi=dpi, alpha=False)
    return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)


def _extract_text_blocks(page: fitz.Page) -> tuple[str, list[dict[str, Any]], list[TextLine], bool]:
    data = page.get_text("dict")
    lines: list[TextLine] = []
    blocks_out: list[dict[str, Any]] = []
    formula = False
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        block_lines = []
        for line in block.get("lines", []):
            spans = [span for span in line.get("spans", []) if str(span.get("text", "")).strip()]
            if not spans:
                continue
            text = "".join(str(span.get("text", "")) for span in spans).strip()
            if not text:
                continue
            size = max(float(span.get("size", 0.0)) for span in spans)
            bold = any(
                (int(span.get("flags", 0)) & 16)
                or "bold" in str(span.get("font", "")).lower()
                or "hei" in str(span.get("font", "")).lower()
                for span in spans
            )
            bbox = tuple(float(v) for v in line.get("bbox", (0, 0, 0, 0)))
            line_text = text
            lines.append(
                TextLine(
                    page=page.number + 1,
                    text=line_text,
                    bbox=bbox,
                    size=size,
                    bold=bold,
                    source="text",
                )
            )
            if MATH_RE.search(line_text) or any(
                token in str(span.get("font", "")).lower()
                for span in spans
                for token in ("math", "cmmi", "cmsy", "symbol")
            ):
                formula = True
            block_lines.append(
                {
                    "text": line_text,
                    "bbox": bbox,
                    "size": size,
                    "bold": bold,
                    "spans": [
                        {"text": str(span.get("text", "")), "font": str(span.get("font", ""))}
                        for span in spans
                    ],
                }
            )
        if block_lines:
            blocks_out.append(
                {
                    "type": "text",
                    "bbox": [float(v) for v in block.get("bbox", (0, 0, 0, 0))],
                    "lines": block_lines,
                }
            )
    text = page.get_text("text")
    return text, blocks_out, lines, formula


def _image_coverage(page: fitz.Page) -> float:
    page_area = max(1.0, abs(page.rect.width * page.rect.height))
    covered = 0.0
    for image in page.get_images(full=True):
        try:
            for rect in page.get_image_rects(image[0]):
                covered += abs(rect.width * rect.height)
        except Exception:
            continue
    return min(1.0, covered / page_area)


def _save_extracted_images(
    document: fitz.Document,
    page: fitz.Page,
    out_dir: Path,
    *,
    max_images: int = 6,
) -> list[str]:
    saved: list[str] = []
    seen: set[str] = set()
    out_dir.mkdir(parents=True, exist_ok=True)
    for image in page.get_images(full=True)[: max_images * 2]:
        xref = image[0]
        try:
            info = document.extract_image(xref)
        except Exception:
            continue
        width = int(info.get("width", 0))
        height = int(info.get("height", 0))
        if width < 90 or height < 90 or width * height < 14000:
            continue
        digest = hashlib.sha1(info["image"]).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        extension = info.get("ext", "png")
        if extension not in {"png", "jpg", "jpeg", "webp", "bmp"}:
            extension = "png"
        destination = out_dir / f"page{page.number + 1:04d}_img{len(saved) + 1:02d}_{digest[:8]}.{extension}"
        destination.write_bytes(info["image"])
        saved.append(str(destination))
        if len(saved) >= max_images:
            break
    return saved


class PdfParser:
    def __init__(
        self,
        *,
        ocr_engine: str = "auto",
        render_dpi: int = 150,
        ocr_dpi: int = 200,
        min_text_chars: int = 80,
    ):
        self.ocr = OcrEngine(ocr_engine)
        self.render_dpi = render_dpi
        self.ocr_dpi = ocr_dpi
        self.min_text_chars = min_text_chars

    def parse(
        self,
        path: Path,
        output_dir: Path,
        *,
        progress: Callable[[int, int, str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> PdfParseResult:
        if not path.exists():
            raise PdfError(f"文件不存在：{path}")
        output_dir.mkdir(parents=True, exist_ok=True)
        pages_dir = output_dir / "pages"
        page_images_dir = output_dir / "page_images"
        extracted_dir = output_dir / "images"
        for directory in (pages_dir, page_images_dir, extracted_dir):
            directory.mkdir(parents=True, exist_ok=True)

        try:
            document = fitz.open(path)
        except Exception as exc:
            raise PdfCorruptError(f"无法打开 PDF：{exc}") from exc

        try:
            if document.needs_pass:
                raise PdfEncryptedError("PDF 已加密，无法读取。请先移除密码后重新导入。")
            total = document.page_count
            if total <= 0:
                raise PdfCorruptError("PDF 没有页面")
            metadata = {key: value for key, value in (document.metadata or {}).items() if value}
            toc_raw = document.get_toc(simple=False) or []
            toc = [
                {
                    "level": int(entry[0]),
                    "title": str(entry[1]).strip(),
                    "page": int(entry[2]),
                    "dest": str(entry[3]) if len(entry) > 3 else "",
                }
                for entry in toc_raw
                if int(entry[2]) > 0
            ]
            pages: list[PageArtifact] = []
            lines: list[TextLine] = []
            scanned_pages: list[int] = []
            backend_used = ""

            for index in range(total):
                if should_stop and should_stop():
                    raise PdfError("用户取消了解析")
                page = document.load_page(index)
                page_no = index + 1
                text, blocks, page_lines, formula = _extract_text_blocks(page)
                coverage = _image_coverage(page)
                is_scanned = len(text.strip()) < self.min_text_chars and coverage >= 0.35
                confidence = 1.0
                if is_scanned:
                    image = _pixmap_image(page, self.ocr_dpi)
                    ocr_lines, backend_used = self.ocr.recognize_page(image)
                    if ocr_lines:
                        text = lines_to_text(ocr_lines)
                        scale = 72.0 / self.ocr_dpi
                        for ocr_line in ocr_lines:
                            box = ocr_line.bbox
                            line = TextLine(
                                page=page_no,
                                text=ocr_line.text,
                                bbox=(
                                    box[0] * scale,
                                    box[1] * scale,
                                    box[2] * scale,
                                    box[3] * scale,
                                ),
                                size=0.0,
                                bold=False,
                                source="ocr",
                            )
                            page_lines.append(line)
                            lines.append(line)
                        confidence = (
                            sum(line.confidence for line in ocr_lines) / len(ocr_lines)
                            if ocr_lines
                            else 0.0
                        )
                        blocks = [
                            {
                                "type": "ocr",
                                "lines": [
                                    {
                                        "text": line.text,
                                        "bbox": list(line.bbox),
                                        "confidence": line.confidence,
                                    }
                                    for line in ocr_lines
                                ],
                            }
                        ]
                    scanned_pages.append(page_no)
                else:
                    lines.extend(page_lines)

                formula = formula or bool(MATH_RE.search(text))
                snapshot = page_images_dir / f"page_{page_no:04d}.jpg"
                image = _pixmap_image(page, self.render_dpi)
                image.save(snapshot, "JPEG", quality=84, optimize=True)

                extracted = _save_extracted_images(document, page, extracted_dir)
                pages.append(
                    PageArtifact(
                        page_number=page_no,
                        width=float(page.rect.width),
                        height=float(page.rect.height),
                        text=text,
                        blocks=blocks,
                        is_scanned=is_scanned,
                        ocr_confidence=confidence,
                        image_path=str(snapshot),
                        has_images=bool(page.get_images(full=True)),
                        has_formula=formula,
                        extracted_images=extracted,
                    )
                )
                if progress:
                    progress(page_no, total, f"解析第 {page_no}/{total} 页")
                if page_no % 25 == 0:
                    logger.info("parsed %s/%s pages", page_no, total)

            sha = sha256_file(path)
            result = PdfParseResult(
                path=path,
                sha256=sha,
                page_count=total,
                metadata=metadata,
                toc=toc,
                pages=pages,
                lines=lines,
                scanned_pages=scanned_pages,
                ocr_backend=backend_used,
                output_dir=output_dir,
            )
            self._write_artifacts(result)
            return result
        finally:
            document.close()

    @staticmethod
    def _write_artifacts(result: PdfParseResult) -> None:
        meta = {
            "source": str(result.path),
            "sha256": result.sha256,
            "page_count": result.page_count,
            "metadata": result.metadata,
            "toc": result.toc,
            "scanned_pages": result.scanned_pages,
            "ocr_backend": result.ocr_backend,
            "text_layer": result.has_text_layer,
        }
        (result.output_dir / "document.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with (result.output_dir / "pages.jsonl").open("w", encoding="utf-8") as handle:
            for page in result.pages:
                handle.write(
                    json.dumps(
                        {
                            "page": page.page_number,
                            "is_scanned": page.is_scanned,
                            "ocr_confidence": page.ocr_confidence,
                            "text": page.text,
                            "image": page.image_path,
                            "images": page.extracted_images,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        with (result.output_dir / "lines.jsonl").open("w", encoding="utf-8") as handle:
            for line in result.lines:
                handle.write(
                    json.dumps(
                        {
                            "page": line.page,
                            "text": line.text,
                            "bbox": list(line.bbox),
                            "size": line.size,
                            "bold": line.bold,
                            "source": line.source,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )


def page_text_map(result: PdfParseResult) -> dict[int, str]:
    return {page.page_number: page.text for page in result.pages}
