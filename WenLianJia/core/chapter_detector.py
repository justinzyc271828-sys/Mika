"""Chapter and section structure detection.

The PDF outline is the most reliable source.  When it is absent or incomplete
the detector falls back to the heading lines that layout analysis recovered.
Every chapter carries an explicit page range so exercise detection can never
leak questions from the next chapter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .layout_analyzer import Heading, TextLine, classify_heading, heading_text_key, normalise_text

CHAPTER_TITLE_RE = re.compile(
    r"^\s*(?:第\s*[0-9一二三四五六七八九十百]+\s*[章篇部]|Chapter\s+[0-9IVXLC]+)",
    re.IGNORECASE,
)
APPENDIX_RE = re.compile(r"^\s*(附录|Appendix|后记|参考文献|索引)", re.IGNORECASE)


@dataclass
class DetectedChapter:
    number: int
    title: str
    start_page: int
    end_page: int
    level: int = 1
    source: str = "outline"
    section_headings: list[Heading] = field(default_factory=list)


@dataclass
class ChapterDetectionResult:
    chapters: list[DetectedChapter]
    method: str
    warnings: list[str] = field(default_factory=list)


def _title_from_toc(title: str) -> str:
    return normalise_text(re.sub(r"[.．·…]{2,}\s*\d+\s*$", "", title))


class ChapterDetector:
    def __init__(self, *, min_chapters_for_outline: int = 1):
        self.min_chapters_for_outline = min_chapters_for_outline

    def detect(
        self,
        pages: Iterable[Any],
        toc: list[dict[str, Any]],
        lines: list[TextLine],
        headings: list[Heading] | None = None,
    ) -> ChapterDetectionResult:
        page_list = list(pages)
        total_pages = len(page_list)
        warnings: list[str] = []
        chapters = self._from_outline(toc, total_pages)
        method = "outline"
        if len(chapters) < self.min_chapters_for_outline:
            chapters = self._from_headings(headings or [], total_pages)
            method = "headings"
        if not chapters:
            chapters = [
                DetectedChapter(
                    number=1,
                    title="全书",
                    start_page=1,
                    end_page=max(1, total_pages),
                    source="fallback",
                )
            ]
            method = "fallback"
            warnings.append("未能识别章节结构，已按全书一个章节处理，请人工检查。")

        chapters.sort(key=lambda chapter: (chapter.start_page, chapter.number))
        for index, chapter in enumerate(chapters):
            chapter.start_page = max(1, min(chapter.start_page, total_pages))
            end = chapter.end_page
            if index + 1 < len(chapters):
                end = min(end, chapters[index + 1].start_page - 1)
            chapter.end_page = max(chapter.start_page, min(end, total_pages))

        # Attach section headings that actually live inside each chapter.
        all_headings = headings or []
        for chapter in chapters:
            chapter.section_headings = [
                heading
                for heading in all_headings
                if chapter.start_page <= heading.page <= chapter.end_page
                and heading.kind in {"section", "exercise"}
                and heading_text_key(heading.text) not in {heading_text_key(chapter.title)}
            ]

        gaps = [
            chapter
            for chapter in chapters
            if chapter.end_page < chapter.start_page
        ]
        if gaps:
            warnings.append(f"{len(gaps)} 个章节页范围异常，请检查目录或标题识别结果。")
        if method == "outline" and len(chapters) == 1 and total_pages > 60:
            warnings.append("仅识别到 1 个章节，可能目录不完整，建议人工检查。")
        return ChapterDetectionResult(chapters=chapters, method=method, warnings=warnings)

    def _from_outline(
        self, toc: list[dict[str, Any]], total_pages: int
    ) -> list[DetectedChapter]:
        if not toc:
            return []
        chapter_entries = [
            entry
            for entry in toc
            if CHAPTER_TITLE_RE.match(_title_from_toc(str(entry.get("title", ""))))
            or APPENDIX_RE.match(_title_from_toc(str(entry.get("title", ""))))
        ]
        if not chapter_entries:
            # Some outlines only carry sections; accept level-1 entries if they
            # look like major divisions.
            min_level = min((int(entry.get("level", 1)) for entry in toc), default=1)
            chapter_entries = [
                entry
                for entry in toc
                if int(entry.get("level", 1)) == min_level
                and len(_title_from_toc(str(entry.get("title", "")))) <= 60
            ]
        if not chapter_entries:
            return []
        entries: list[DetectedChapter] = []
        seen: set[str] = set()
        for index, entry in enumerate(chapter_entries):
            title = _title_from_toc(str(entry.get("title", "")))
            start = int(entry.get("page", 1))
            if start <= 0 or start > total_pages or not title:
                continue
            key = heading_text_key(title)
            if key in seen:
                continue
            seen.add(key)
            entries.append(
                DetectedChapter(
                    number=index + 1,
                    title=title,
                    start_page=start,
                    end_page=total_pages,
                    level=int(entry.get("level", 1)),
                    source="outline",
                )
            )
        return entries

    def _from_headings(self, headings: list[Heading], total_pages: int) -> list[DetectedChapter]:
        chapters: list[DetectedChapter] = []
        seen: set[str] = set()
        for heading in headings:
            kind, _ = classify_heading(heading.text)
            if kind != "chapter" and not CHAPTER_TITLE_RE.match(heading.text):
                continue
            key = heading_text_key(heading.text)
            if key in seen:
                continue
            # A repeated running header is normally shorter and appears many
            # times; the first occurrence after the front matter is the chapter.
            seen.add(key)
            chapters.append(
                DetectedChapter(
                    number=len(chapters) + 1,
                    title=heading.text,
                    start_page=heading.page,
                    end_page=total_pages,
                    source="heading",
                )
            )
        return chapters
