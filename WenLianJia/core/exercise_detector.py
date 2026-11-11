"""Exercise detection.

Textbooks number exercises in a handful of stable ways.  The detector first
locates *exercise sections* (习题/练习/思考题 ...), then segments numbered
questions inside those sections.  It never treats a bare question mark as an
exercise and it keeps an explicit low-confidence path instead of dropping a
section that could not be segmented.

The output is deliberately traceable: page range, raw numbering, confidence,
the surrounding section title and the page screenshots a human can check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from models.schemas import ExerciseType

from .layout_analyzer import (
    CHAPTER_RE,
    EXERCISE_HEADING_RE,
    PAGE_NUMBER_RE,
    SECTION_RE,
    Heading,
    TextLine,
    body_font_size,
    normalise_text,
)

DOT_NUMBER_RE = re.compile(
    r"^\s*(\d{1,3})(?:\s*[.．]\s*(\d{1,3}))?\s*([.．、])\s*(?=\S)"
)
DOT_SPACE_NUMBER_RE = re.compile(r"^\s*(\d{1,3})[.．](\d{1,3})\s+(?=\S)")
PAREN_NUMBER_RE = re.compile(r"^\s*[（(]\s*(\d{1,3})\s*[)）]\s*(?=\S)")
CIRCLED_NUMBER_RE = re.compile(r"^\s*([①-⑳])\s*(?=\S)")
EXERCISE_SECTION_RE = re.compile(
    r"^\s*(习题|练习|思考题|复习题|作业题|问题与思考|Exercises?|Problems?|Questions?|Review\s+Questions?)"
    r"\s*(?:[0-9０-９]+(?:\.[0-9]+)*|[一二三四五六七八九十]+)?\s*(?:[：:.．、-]\s*)?[^。！？!?]{0,26}$",
    re.IGNORECASE,
)
SUBHEADING_RE = re.compile(
    r"^\s*(?:[（(]\s*[一二三四五六七八九十]+\s*[)）]|[一二三四五六七八九十]+\s*[、.．])"
)
OPTION_RE = re.compile(r"(?:^|\s)[A-DＡ-Ｄ][.．、)）]\s*\S")
PROOF_RE = re.compile(r"证明|求证|试证|验证|prove|show\s+that", re.IGNORECASE)
CHOICE_RE = re.compile(r"选择题|单项选择|多项选择|下列.{0,12}(?:正确|错误|成立)")
FILL_RE = re.compile(r"填空|____|_{3,}")
PROGRAMMING_RE = re.compile(r"编写.{0,8}(?:程序|算法|代码)|程序|算法|复杂度|伪代码", re.IGNORECASE)
EXPERIMENT_RE = re.compile(r"实验|观察|测量|动手|上机")
CALC_RE = re.compile(r"计算|求|化简|解方程|解不等式|积分|导数|极限|证明.*等于", re.IGNORECASE)


@dataclass
class DetectedExercise:
    number: str
    text: str
    page_start: int
    page_end: int
    chapter_number: int
    chapter_title: str
    section_title: str
    exercise_type: ExerciseType
    confidence: float
    images: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    context_before: str = ""
    context_after: str = ""


@dataclass
class ExerciseSection:
    chapter_number: int
    title: str
    page_start: int
    page_end: int
    question_count: int
    method: str


@dataclass
class ExerciseDetectionResult:
    exercises: list[DetectedExercise]
    sections: list[ExerciseSection]
    per_chapter: dict[int, int]
    warnings: list[str] = field(default_factory=list)
    low_confidence: int = 0

    @property
    def total(self) -> int:
        return len(self.exercises)


def _is_cjk(char: str) -> bool:
    return bool(char) and "\u4e00" <= char <= "\u9fff"


def _clean_line(text: str) -> str:
    return normalise_text(text.replace("\u00ad", ""))


def _join_lines(lines: list[str]) -> str:
    output = ""
    for raw in lines:
        line = _clean_line(raw)
        if not line:
            continue
        if not output:
            output = line
            continue
        if output.endswith("-") and line[:1].islower():
            output = output[:-1] + line
        elif SUBHEADING_RE.match(line) or line.startswith(("解：", "证明：", "答案：", "提示：")):
            output += "\n" + line
        elif _is_cjk(output[-1]) and _is_cjk(line[0]):
            output += line
        else:
            output += " " + line
    return output.strip()


def classify_exercise_type(text: str) -> ExerciseType:
    if PROGRAMMING_RE.search(text):
        return ExerciseType.PROGRAMMING
    if EXPERIMENT_RE.search(text):
        return ExerciseType.EXPERIMENT
    if PROOF_RE.search(text):
        return ExerciseType.PROOF
    if FILL_RE.search(text):
        return ExerciseType.FILL_BLANK
    if CHOICE_RE.search(text) or len(OPTION_RE.findall(text)) >= 2:
        return ExerciseType.CHOICE
    if CALC_RE.search(text):
        return ExerciseType.CALCULATION
    if len(text) <= 40:
        return ExerciseType.SHORT_ANSWER
    return ExerciseType.UNKNOWN


def _heading_like(line: TextLine, body_size: float, *, chapter_title: str = "") -> bool:
    text = line.text.strip()
    if not text or len(text) > 55 or PAGE_NUMBER_RE.match(text):
        return False
    if _is_exercise_heading(text):
        return True
    if chapter_title and normalise_text(text) == normalise_text(chapter_title):
        return True
    if CHAPTER_RE.match(text) or SECTION_RE.match(text):
        return True
    # Generic end-of-chapter headings such as 本章小结 or 关键词 do not match a
    # numbering pattern but are visibly larger than body text.  They still must
    # terminate the exercise block so summary paragraphs never join the last
    # question.
    if (
        body_size > 0
        and line.size >= body_size * 1.15
        and len(text) <= 40
        and not text.endswith(("。", "？", "?", "！", "!"))
    ):
        return True
    return False


def _is_exercise_heading(text: str) -> bool:
    text = normalise_text(text)
    if not text or len(text) > 50 or text.endswith(("。", "？", "?", "！", "!")):
        return False
    if not EXERCISE_HEADING_RE.search(text):
        return False
    return bool(EXERCISE_SECTION_RE.match(text)) or len(text) <= 18


@dataclass
class _QuestionStart:
    index: int
    number: str
    label: str


class ExerciseDetector:
    def __init__(self, *, min_question_chars: int = 8, fallback_cluster: int = 3):
        self.min_question_chars = min_question_chars
        self.fallback_cluster = fallback_cluster

    def detect(
        self,
        *,
        sku: str,
        chapters: Iterable[Any],
        pages: Iterable[Any],
        lines: list[TextLine],
        headings: list[Heading] | None = None,
    ) -> ExerciseDetectionResult:
        chapter_list = list(chapters)
        page_list = list(pages)
        page_map = {int(page.page_number): page for page in page_list}
        headings = headings or []
        warnings: list[str] = []
        all_exercises: list[DetectedExercise] = []
        sections_out: list[ExerciseSection] = []
        per_chapter: dict[int, int] = {}
        global_index = 0

        for chapter in chapter_list:
            chapter_number = int(chapter.number)
            chapter_lines = [
                line
                for line in lines
                if int(chapter.start_page) <= line.page <= int(chapter.end_page)
            ]
            if not chapter_lines:
                per_chapter[chapter_number] = 0
                warnings.append(f"第 {chapter_number} 章没有可解析文本，无法识别习题。")
                continue

            body = body_font_size(chapter_lines)
            heading_indices = self._heading_indices(
                chapter_lines, body, chapter_title=str(chapter.title)
            )
            section_heading_indices = self._section_indices(
                chapter_lines, heading_indices, headings, chapter
            )
            if not section_heading_indices:
                fallback = self._fallback_section(chapter_lines)
                if fallback is None:
                    per_chapter[chapter_number] = 0
                    warnings.append(
                        f"第 {chapter_number} 章未找到习题小节，可能该章没有课后习题，请人工确认。"
                    )
                    continue
                section_heading_indices = [fallback]
                warnings.append(
                    f"第 {chapter_number} 章未找到明确的习题标题，已按编号题目聚集区识别，置信度较低。"
                )

            for position, start_index in enumerate(section_heading_indices):
                end_index = len(chapter_lines)
                for candidate in section_heading_indices[position + 1 :]:
                    if candidate > start_index:
                        end_index = candidate
                        break
                # A following section/chapter heading also ends the block.
                for candidate in heading_indices:
                    if start_index < candidate < end_index:
                        end_index = candidate
                        break
                block = chapter_lines[start_index:end_index]
                section_title = _clean_line(block[0].text) if block else "习题"
                question_starts = self._question_starts(block[1:])
                if not question_starts:
                    body_text = _join_lines([line.text for line in block[1:]])
                    if len(body_text) < self.min_question_chars:
                        continue
                    exercise = self._make_exercise(
                        sku=sku,
                        chapter=chapter,
                        section_title=section_title,
                        number="",
                        text=body_text,
                        page_start=block[0].page,
                        page_end=block[-1].page,
                        confidence=0.3,
                        page_map=page_map,
                        lines=[line for line in block[1:] if not PAGE_NUMBER_RE.match(line.text)],
                        raw_reason="section_without_numbering",
                    )
                    all_exercises.append(exercise)
                    sections_out.append(
                        ExerciseSection(
                            chapter_number=chapter_number,
                            title=section_title,
                            page_start=block[0].page,
                            page_end=block[-1].page,
                            question_count=1,
                            method="section_fallback",
                        )
                    )
                    continue

                method = "numbered"
                count = 0
                for index, start in enumerate(question_starts):
                    stop = (
                        question_starts[index + 1].index
                        if index + 1 < len(question_starts)
                        else len(block) - 1
                    )
                    chunk = block[start.index + 1 : stop + 1]
                    text = _join_lines([line.text for line in chunk if not PAGE_NUMBER_RE.match(line.text)])
                    if len(text) < self.min_question_chars:
                        continue
                    page_start = chunk[0].page if chunk else block[start.index].page
                    page_end = chunk[-1].page if chunk else page_start
                    context_before = _join_lines(
                        [line.text for line in block[max(0, start.index - 3) : start.index]]
                    )
                    context_after = _join_lines(
                        [line.text for line in block[stop + 1 : stop + 4]]
                    )
                    exercise = self._make_exercise(
                        sku=sku,
                        chapter=chapter,
                        section_title=section_title,
                        number=start.number,
                        text=text,
                        page_start=page_start,
                        page_end=page_end,
                        confidence=self._confidence(
                            text=text,
                            numbering=start.label,
                            page_lines=chunk,
                            method=method,
                            page_map=page_map,
                        ),
                        page_map=page_map,
                        lines=chunk,
                        raw_reason="numbered_start",
                        context_before=context_before,
                        context_after=context_after,
                    )
                    all_exercises.append(exercise)
                    count += 1
                sections_out.append(
                    ExerciseSection(
                        chapter_number=chapter_number,
                        title=section_title,
                        page_start=block[0].page,
                        page_end=block[-1].page,
                        question_count=count,
                        method=method,
                    )
                )
            per_chapter[chapter_number] = sum(
                1 for exercise in all_exercises if exercise.chapter_number == chapter_number
            )

        # Global numbering and stable IDs are assigned in reading order.
        for exercise in all_exercises:
            global_index += 1
            chapter_no = exercise.chapter_number
            local_no = sum(
                1
                for previous in all_exercises[: global_index - 1]
                if previous.chapter_number == chapter_no
            )
            exercise.raw["global_number"] = global_index
            exercise.raw["local_number"] = local_no
            exercise.raw["exercise_id"] = f"{sku}-CH{chapter_no:02d}-Q{local_no:03d}"

        low_confidence = sum(1 for exercise in all_exercises if exercise.confidence < 0.6)
        if not all_exercises:
            warnings.append("全书未识别到任何习题。请检查 PDF 文本层或手动添加。")
        return ExerciseDetectionResult(
            exercises=all_exercises,
            sections=sections_out,
            per_chapter=per_chapter,
            warnings=warnings,
            low_confidence=low_confidence,
        )

    def _heading_indices(
        self, lines: list[TextLine], body_size: float, *, chapter_title: str
    ) -> list[int]:
        indices: list[int] = []
        for index, line in enumerate(lines):
            if index == 0:
                continue
            if _heading_like(line, body_size, chapter_title=chapter_title):
                indices.append(index)
        return indices

    def _section_indices(
        self,
        lines: list[TextLine],
        heading_indices: list[int],
        headings: list[Heading],
        chapter: Any,
    ) -> list[int]:
        wanted = {
            (heading.page, normalise_text(heading.text))
            for heading in headings
            if heading.kind == "exercise"
            and int(chapter.start_page) <= heading.page <= int(chapter.end_page)
        }
        indices: list[int] = []
        for index in heading_indices:
            text = normalise_text(lines[index].text)
            if _is_exercise_heading(text) or (
                lines[index].page,
                text,
            ) in wanted:
                indices.append(index)
        # Include explicitly detected headings even if their line failed the
        # length heuristic.
        for page, text in wanted:
            if any(lines[index].page == page and normalise_text(lines[index].text) == text for index in indices):
                continue
            for index, line in enumerate(lines):
                if line.page == page and normalise_text(line.text) == text:
                    indices.append(index)
                    break
        return sorted(set(indices))

    def _fallback_section(self, lines: list[TextLine]) -> int | None:
        candidates = [
            index
            for index, line in enumerate(lines)
            if self._parse_start(line.text) is not None
        ]
        if len(candidates) < self.fallback_cluster:
            return None
        clusters: list[list[int]] = []
        current = [candidates[0]]
        for index in candidates[1:]:
            if index - current[-1] <= 40:
                current.append(index)
            else:
                clusters.append(current)
                current = [index]
        clusters.append(current)
        best = max(clusters, key=len)
        if len(best) < self.fallback_cluster:
            return None
        # Start a few lines before the first numbered question so instructions
        # and shared figure references stay in the first question's context.
        return max(0, best[0] - 2)

    def _question_starts(self, lines: list[TextLine]) -> list[_QuestionStart]:
        dot: list[_QuestionStart] = []
        paren: list[_QuestionStart] = []
        circled: list[_QuestionStart] = []
        for index, line in enumerate(lines):
            text = line.text
            if PAGE_NUMBER_RE.match(text):
                continue
            parsed = self._parse_start(text)
            if parsed is None:
                continue
            number, label, style = parsed
            item = _QuestionStart(index=index, number=number, label=label)
            if style == "dot":
                dot.append(item)
            elif style == "paren":
                paren.append(item)
            else:
                circled.append(item)
        # Main questions almost always use 1. / 1.1 / 1、 style.  Parenthesised
        # numbers are normally sub-questions and must stay inside the text.
        if len(dot) >= 2 or (dot and len(dot) >= len(paren)):
            return dot
        if len(paren) >= 3:
            return paren
        if len(circled) >= 3:
            return circled
        return dot

    @staticmethod
    def _parse_start(text: str) -> tuple[str, str, str] | None:
        match = DOT_NUMBER_RE.match(text)
        if match:
            major, minor, punct = match.groups()
            number = f"{major}.{minor}" if minor else major
            return number, f"{number}{punct}", "dot"
        match = DOT_SPACE_NUMBER_RE.match(text)
        if match:
            number = f"{match.group(1)}.{match.group(2)}"
            return number, number, "dot"
        match = PAREN_NUMBER_RE.match(text)
        if match:
            return match.group(1), f"({match.group(1)})", "paren"
        match = CIRCLED_NUMBER_RE.match(text)
        if match:
            return match.group(1), match.group(1), "circled"
        return None

    def _make_exercise(
        self,
        *,
        sku: str,
        chapter: Any,
        section_title: str,
        number: str,
        text: str,
        page_start: int,
        page_end: int,
        confidence: float,
        page_map: dict[int, Any],
        lines: list[TextLine],
        raw_reason: str,
        context_before: str = "",
        context_after: str = "",
    ) -> DetectedExercise:
        images: list[str] = []
        references_figure = bool(re.search(r"[图表]\s*\d|如图|见图|下图|上图|表格", text))
        for page_number in range(page_start, page_end + 1):
            page = page_map.get(page_number)
            if page is None:
                continue
            if references_figure or getattr(page, "has_images", False):
                snapshot = str(getattr(page, "image_path", "") or "")
                if snapshot and snapshot not in images:
                    images.append(snapshot)
            for image in getattr(page, "extracted_images", []) or []:
                if image not in images:
                    images.append(image)
            if len(images) >= 6:
                break
        exercise_id = f"{sku}-CH{int(chapter.number):02d}-Q{0:03d}"
        return DetectedExercise(
            number=number,
            text=text,
            page_start=page_start,
            page_end=page_end,
            chapter_number=int(chapter.number),
            chapter_title=str(chapter.title),
            section_title=section_title,
            exercise_type=classify_exercise_type(text),
            confidence=round(max(0.2, min(0.95, confidence)), 2),
            images=images,
            raw={
                "reason": raw_reason,
                "exercise_id": exercise_id,
                "chapter_title": str(chapter.title),
                "section_title": section_title,
                "page_images": [
                    str(getattr(page_map[p], "image_path", ""))
                    for p in range(page_start, page_end + 1)
                    if p in page_map
                ],
                "is_scanned": any(
                    bool(getattr(page_map[p], "is_scanned", False))
                    for p in range(page_start, page_end + 1)
                    if p in page_map
                ),
                "line_count": len(lines),
            },
            context_before=context_before,
            context_after=context_after,
        )

    def _confidence(
        self,
        *,
        text: str,
        numbering: str,
        page_lines: list[TextLine],
        method: str,
        page_map: dict[int, Any],
    ) -> float:
        score = 0.62
        if numbering:
            score += 0.12
        if len(text) >= 60:
            score += 0.08
        if method == "numbered":
            score += 0.06
        if any(getattr(page_map.get(line.page), "is_scanned", False) for line in page_lines):
            score -= 0.22
        if len(text) > 3000:
            score -= 0.15
        return score
