"""OCR backends for scanned textbooks.

The desktop build ships RapidOCR (ONNX Runtime) because it installs reliably on
Windows without a CUDA toolchain.  PaddleOCR is used automatically when the
owner installs it, and the interface is intentionally small so another engine
can be added without touching the parser.
"""

from __future__ import annotations

import importlib.util
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from PIL import Image, ImageEnhance, ImageOps

from .logging_setup import get_logger

logger = get_logger("ocr")


@dataclass
class OcrLine:
    text: str
    confidence: float
    box: list[list[float]]

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        xs = [point[0] for point in self.box]
        ys = [point[1] for point in self.box]
        return min(xs), min(ys), max(xs), max(ys)


class OcrBackend(Protocol):
    name: str

    def recognize(self, image: np.ndarray) -> list[OcrLine]: ...


def _prepare(image: Image.Image, max_side: int = 2600) -> np.ndarray:
    image = ImageOps.exif_transpose(image).convert("RGB")
    if min(image.size) < 900:
        scale = min(900 / max(1, min(image.size)), 2.0)
        image = image.resize(
            (int(image.width * scale), int(image.height * scale)), Image.Resampling.LANCZOS
        )
    if max(image.size) > max_side:
        ratio = max_side / max(image.size)
        image = image.resize(
            (int(image.width * ratio), int(image.height * ratio)), Image.Resampling.LANCZOS
        )
    image = ImageEnhance.Contrast(image).enhance(1.12)
    return np.asarray(image)


class RapidOcrBackend:
    name = "rapidocr"

    def __init__(self) -> None:
        self._engine: Any = None
        self._lock = threading.Lock()

    def _get_engine(self) -> Any:
        if self._engine is None:
            from rapidocr import RapidOCR

            self._engine = RapidOCR()
        return self._engine

    def recognize(self, image: np.ndarray) -> list[OcrLine]:
        engine = self._get_engine()
        with self._lock:
            result = engine(image)
        lines = _normalise_result(result)
        return lines


class PaddleOcrBackend:
    name = "paddleocr"

    def __init__(self) -> None:
        self._engine: Any = None
        self._lock = threading.Lock()

    def _get_engine(self) -> Any:
        if self._engine is None:
            from paddleocr import PaddleOCR  # type: ignore

            self._engine = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
        return self._engine

    def recognize(self, image: np.ndarray) -> list[OcrLine]:
        engine = self._get_engine()
        with self._lock:
            raw = engine.ocr(image, cls=True)
        lines: list[OcrLine] = []
        for page in raw or []:
            for item in page or []:
                try:
                    box, (text, confidence) = item
                except (TypeError, ValueError):
                    continue
                lines.append(OcrLine(str(text), float(confidence), box))
        return lines


def _normalise_result(result: Any) -> list[OcrLine]:
    """Accept both RapidOCR 2.x tuples and 3.x result objects."""

    lines: list[OcrLine] = []
    txts = boxes = scores = None
    if hasattr(result, "txts"):
        txts = result.txts
        scores = result.scores
        boxes = result.boxes
    elif isinstance(result, tuple) and len(result) >= 1:
        payload = result[0]
        if isinstance(payload, list):
            for item in payload:
                try:
                    box, text, score = item
                except (TypeError, ValueError):
                    continue
                lines.append(OcrLine(str(text), float(score), box))
            return lines
    if txts is None:
        return lines
    for index, text in enumerate(txts or []):
        score = float(scores[index]) if scores is not None and index < len(scores) else 0.0
        box = boxes[index] if boxes is not None and index < len(boxes) else []
        lines.append(OcrLine(str(text), score, _box_to_list(box)))
    return lines


def _box_to_list(box: Any) -> list[list[float]]:
    if box is None:
        return []
    try:
        return [[float(point[0]), float(point[1])] for point in box]
    except (TypeError, ValueError, IndexError):
        return []


class OcrEngine:
    """Configured OCR facade with lazy loading and failure reporting."""

    def __init__(self, engine: str = "auto") -> None:
        self.preferred = engine
        self._backend: OcrBackend | None = None
        self.last_error = ""

    def available(self) -> bool:
        try:
            self._resolve()
            return True
        except Exception as exc:  # pragma: no cover - depends on optional packages
            self.last_error = str(exc)
            return False

    def _resolve(self) -> OcrBackend:
        if self._backend is not None:
            return self._backend
        if self.preferred in ("paddleocr", "paddle"):
            self._backend = PaddleOcrBackend()
        elif self.preferred == "rapidocr":
            self._backend = RapidOcrBackend()
        elif importlib.util.find_spec("paddleocr") is not None:
            self._backend = PaddleOcrBackend()
        else:
            self._backend = RapidOcrBackend()
        return self._backend

    def recognize_page(self, image: Image.Image | str | Path) -> tuple[list[OcrLine], str]:
        """Return recognised lines and the backend actually used.

        A path may be supplied for diagnostics; production passes a page image.
        Failures are returned instead of guessed text.  The caller keeps the
        original page screenshot so a human or a multimodal model can inspect
        the unreadable region.
        """

        if isinstance(image, (str, Path)):
            with Image.open(image) as opened:
                image = opened.copy()
        if not isinstance(image, Image.Image):
            raise TypeError(f"OCR image must be a PIL image or path, got {type(image).__name__}")
        backend = self._resolve()
        try:
            lines = backend.recognize(_prepare(image))
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("OCR failed with %s: %s", backend.name, exc)
            if self.preferred == "auto" and backend.name != "rapidocr":
                rapid = RapidOcrBackend()
                try:
                    lines = rapid.recognize(_prepare(image))
                    return self._finish(lines, rapid.name)
                except Exception as rapid_exc:  # pragma: no cover - engine install
                    self.last_error += f"; rapidocr: {rapid_exc}"
            return [], backend.name
        return self._finish(lines, backend.name)

    @staticmethod
    def _finish(lines: list[OcrLine], backend_name: str) -> tuple[list[OcrLine], str]:
        lines = [line for line in lines if line.text.strip()]
        lines.sort(key=lambda line: (line.bbox[1], line.bbox[0]))
        return lines, backend_name


def lines_to_text(lines: list[OcrLine]) -> str:
    return "\n".join(line.text.strip() for line in lines if line.text.strip())
