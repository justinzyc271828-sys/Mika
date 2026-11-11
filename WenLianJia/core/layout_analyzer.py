"""Layout primitives shared by chapter and exercise detection.

The detector works on line-level data instead of one large page string.  This
keeps original page numbers, catches headings by font size, and lets exercise
questions span page boundaries without inventing content.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from typing import Any, Iterable

CHAPTER_RE = re.compile(
    r"^\s*(?:第\s*([0-9一二三四五六七八九十百]+)\s*[章篇部]|Chapter\s+([0-9IVXLC]+)|([0-9]{1,2})\s+[^\d\s].{0,30}$)",
    re.IGNORECASE,
)
SECTION_RE = re.compile(
    r"^\s*(?:第\s*([0-9一二三四五六七八九十百]+)\s*节|([0-9]{1,2}(?:\.[0-9]{1,2}){1,2})\s+\S|§\s*[0-9.]+)",
    re.IGNORECASE,
)
EXERCISE_HEADING_RE = re.compile(
    r"(习题|练习|思考题|复习题|作业题|问题与思考|Exercises?|Problems?|Questions?|Review\s+Questions?)",
    re.IGNORECASE,
)
PAGE_NUMBER_RE = re.compile(r"^\s*[-—–]?\s*\d{1,4}\s*[-—–]?\s*$")
FORMULA_LINE_RE = re.compile(r"^[\s\d\W=+\-×÷<>≤≥∫∑√().,，。；:：]{0,12}$")


@dataclass(frozen=True)
class TextLine:
    page: int
    text: str
    bbox: tuple[float, float, float, float]
    size: float = 0.0
    bold: bool = False
    source: str = "text"

    @property
    def starts_sentence(self) -> bool:
        text = self.text.lstrip()
        return bool(re.match(r"^[\[（(【]?\s*\d{1,3}\s*[\.、．)）]", text))


@dataclass(frozen=True)
class Heading:
    page: int
    text: str
    level: int
    bbox: tuple[float, float, float, float]
    size: float
    kind: str  # chapter | section | exercise | unknown


def normalise_text(text: str) -> str:
    text = text.replace("\u00a0", " ").replace("\u3000", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def clean_heading(text: str) -> str:
    return normalise_text(text).strip(" ：:.-—")


def body_font_size(lines: Iterable[TextLine]) -> float:
    sizes: list[float] = []
    weights: list[int] = []
    for line in lines:
        if line.size <= 0:
            continue
        weight = max(1, len(line.text))
        sizes.append(round(line.size, 1))
        weights.append(weight)
    if not sizes:
        return 10.5
    weighted: dict[float, int] = {}
    for size, weight in zip(sizes, weights):
        weighted[size] = weighted.get(size, 0) + weight
    return max(weighted.items(), key=lambda item: item[1])[0]


def _chapter_number(text: str) -> int | None:
    match = re.match(
        r"^\s*第\s*([0-9一二三四五六七八九十百]+)\s*[章篇部]", text
    )
    if match:
        return _chinese_to_int(match.group(1))
    match = re.match(r"^\s*Chapter\s+([0-9IVXLC]+)", text, re.IGNORECASE)
    if match:
        token = match.group(1).upper()
        if token.isdigit():
            return int(token)
        return _roman_to_int(token)
    return None


def _chinese_to_int(value: str) -> int:
    if value.isdigit():
        return int(value)
    digits = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if "十" in value:
        left, _, right = value.partition("十")
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    total = 0
    for char in value:
        total = total * 10 + digits.get(char, 0)
    return total


def _roman_to_int(value: str) -> int:
    mapping = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
    total = 0
    previous = 0
    for char in reversed(value):
        current = mapping.get(char, 0)
        total += -current if current < previous else current
        previous = max(previous, current)
    return total


def classify_heading(text: str) -> tuple[str, int]:
    cleaned = clean_heading(text)
    if not cleaned or len(cleaned) > 90:
        return "unknown", 99
    if EXERCISE_HEADING_RE.search(cleaned) and len(cleaned) <= 40:
        return "exercise", 3
    if _chapter_number(cleaned) is not None and len(cleaned) <= 60:
        return "chapter", 1
    if SECTION_RE.match(cleaned) and len(cleaned) <= 70:
        return "section", 2
    return "unknown", 99


def detect_headings(
    lines: Iterable[TextLine], *, body_size: float | None = None
) -> list[Heading]:
    line_list = list(lines)
    body = body_size or body_font_size(line_list)
    candidates: list[Heading] = []
    for line in line_list:
        text = clean_heading(line.text)
        if not text or len(text) > 90 or PAGE_NUMBER_RE.match(text):
            continue
        kind, regex_level = classify_heading(text)
        size_ratio = (line.size / body) if body and line.size else 1.0
        is_large = line.size > 0 and size_ratio >= 1.18
        is_medium = line.size > 0 and size_ratio >= 1.06
        looks_bold = line.bold and size_ratio >= 1.0
        if kind == "unknown":
            if not (is_large or (looks_bold and is_medium)):
                continue
            if len(text) < 2 or FORMULA_LINE_RE.match(text):
                continue
            kind = "heading"
            level = 4 if size_ratio < 1.25 else 3
        else:
            level = regex_level
            if is_large and level > 2:
                level = max(2, level - 1)
        candidates.append(
            Heading(
                page=line.page,
                text=text,
                level=level,
                bbox=line.bbox,
                size=line.size,
                kind=kind,
            )
        )
    # Remove repeated headers/footers and exact duplicates on the same page.
    seen: set[tuple[int, str]] = set()
    result: list[Heading] = []
    for heading in candidates:
        key = (heading.page, heading.text)
        if key in seen:
            continue
        seen.add(key)
        result.append(heading)
    return result


def heading_text_key(value: str) -> str:
    return re.sub(r"\s+", "", clean_heading(value)).lower()


def median_size(lines: Iterable[TextLine]) -> float:
    sizes = [line.size for line in lines if line.size > 0]
    return statistics.median(sizes) if sizes else 0.0


def find_line_index(lines: list[TextLine], page: int, text: str) -> int:
    target = normalise_text(text)
    for index, line in enumerate(lines):
        if line.page == page and normalise_text(line.text) == target:
            return index
    return -1


def blocks_to_lines(blocks: list[dict[str, Any]], page: int) -> list[TextLine]:
    lines: list[TextLine] = []
    for block in blocks:
        for line in block.get("lines", []) if isinstance(block, dict) else []:
            text = normalise_text(str(line.get("text", "")))
            if not text:
                continue
            bbox = tuple(float(v) for v in line.get("bbox", (0, 0, 0, 0)))
            lines.append(
                TextLine(
                    page=page,
                    text=text,
                    bbox=bbox,
                    size=float(line.get("size", 0.0)),
                    bold=bool(line.get("bold", False)),
                    source="text" if block.get("type") == "text" else "ocr",
                )
            )
    return lines
