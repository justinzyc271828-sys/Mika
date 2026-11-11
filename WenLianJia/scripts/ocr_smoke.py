"""Optional OCR smoke test against one page of a scanned PDF.

Usage:
    python scripts/ocr_smoke.py "D:\\path\\book.pdf" 36

This reports OCR lines and confidence only.  It does not create a product,
does not call any model, and does not copy the page into a deliverable.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pymupdf  # noqa: E402

from core.ocr_engine import OcrEngine  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    pdf_path = Path(sys.argv[1])
    page_number = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    if not pdf_path.is_file():
        print(f"PDF not found: {pdf_path}")
        return 2
    document = pymupdf.open(pdf_path)
    try:
        page = document[page_number - 1]
        pixmap = page.get_pixmap(dpi=200, alpha=False)
        image_path = ROOT / ".tmp_ocr_page.png"
        pixmap.save(image_path)
        print(f"rendered page {page_number}: {pixmap.width}x{pixmap.height} -> {image_path}")
        started = time.perf_counter()
        engine = OcrEngine("rapidocr")
        lines, backend = engine.recognize_page(image_path)
        elapsed = time.perf_counter() - started
        print(f"backend: {backend}; lines: {len(lines)}; elapsed: {elapsed:.2f}s")
        for line in lines[:15]:
            print(f"  [{line.confidence:.3f}] {line.text[:100]}")
        if not lines:
            print("OCR returned no lines. Do not treat this page as parsed text.")
            return 1
        return 0
    finally:
        document.close()


if __name__ == "__main__":
    raise SystemExit(main())
