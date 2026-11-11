"""PDF parsing and exercise-detection regression tests."""

from __future__ import annotations

from pathlib import Path

from core.chapter_detector import ChapterDetector
from core.exercise_detector import ExerciseDetector
from core.layout_analyzer import detect_headings
from core.pdf_parser import PdfParser, pdf_inspect

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample_linear_algebra.pdf"


def test_pdf_inspect_reports_text_layer_and_outline() -> None:
    info = pdf_inspect(FIXTURE)
    assert info["page_count"] == 5
    assert info["text_layer"] is True
    assert info["scanned_likely"] is False
    assert info["toc_entries"] >= 2


def test_parse_fixture_preserves_pages_and_text(tmp_path: Path) -> None:
    result = PdfParser().parse(FIXTURE, tmp_path / "parsed")
    assert result.page_count == 5
    assert len(result.pages) == 5
    assert result.scanned_pages == []
    assert len(result.lines) >= 20
    assert (tmp_path / "parsed" / "document.json").is_file()
    assert (tmp_path / "parsed" / "lines.jsonl").is_file()
    assert all(page.image_path for page in result.pages)


def test_chapter_and_exercise_detection_matches_fixture(tmp_path: Path) -> None:
    parsed = PdfParser().parse(FIXTURE, tmp_path / "parsed")
    headings = detect_headings(parsed.lines)
    chapters = ChapterDetector().detect(
        parsed.pages, parsed.toc, parsed.lines, headings
    )
    assert len(chapters.chapters) == 2
    assert [chapter.number for chapter in chapters.chapters] == [1, 2]
    assert all(chapter.title for chapter in chapters.chapters)

    exercises = ExerciseDetector().detect(
        sku="TEST",
        chapters=chapters.chapters,
        pages=parsed.pages,
        lines=parsed.lines,
        headings=headings,
    )
    assert len(exercises.exercises) == 8
    assert exercises.per_chapter == {1: 5, 2: 3}
    assert exercises.low_confidence == 0
    assert exercises.warnings == []
    assert [item.number for item in exercises.exercises] == [
        "1",
        "2",
        "3",
        "4",
        "5",
        "1",
        "2",
        "3",
    ]


def test_exercise_detector_does_not_swallow_summary(tmp_path: Path) -> None:
    parsed = PdfParser().parse(FIXTURE, tmp_path / "parsed")
    headings = detect_headings(parsed.lines)
    chapters = ChapterDetector().detect(
        parsed.pages, parsed.toc, parsed.lines, headings
    )
    exercises = ExerciseDetector().detect(
        sku="TEST",
        chapters=chapters.chapters,
        pages=parsed.pages,
        lines=parsed.lines,
        headings=headings,
    )
    assert "本章小结" not in exercises.exercises[-1].text
