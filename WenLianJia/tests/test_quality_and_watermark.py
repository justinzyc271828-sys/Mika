"""Quality gates, LaTeX text handling and watermark tests."""

from __future__ import annotations

from pathlib import Path

import pymupdf

from core.latex_utils import latex_display, latex_multiline, latex_text
from core.quality_control import structure_check
from core.watermark import Watermarker
from models.schemas import Exercise, ExerciseType, SolutionContent


def _exercise(kind: ExerciseType = ExerciseType.CALCULATION) -> Exercise:
    return Exercise(
        id="T-1",
        product_id="p1",
        chapter_id="c1",
        number="1",
        text="求 1+1 的值。",
        exercise_type=kind,
        confidence=1.0,
    )


def test_structure_check_rejects_placeholders_and_short_steps() -> None:
    content = SolutionContent(
        analysis="略",
        approach="同上",
        detailed_steps=["略"],
        final_answer="无法确定",
    )
    checks = structure_check(content, _exercise())
    details = {check.name: check for check in checks}
    assert details["final_answer"].status == "passed"
    assert details["steps"].status == "failed"
    assert details["placeholders"].status == "failed"
    assert any(check.status == "failed" for check in checks)


def test_structure_check_passes_a_complete_solution() -> None:
    content = SolutionContent(
        question_restatement="计算两个整数之和。",
        analysis="这是基础加法，直接计算即可，并检查结果。",
        knowledge_points=["整数加法"],
        approach="按定义相加。",
        detailed_steps=["识别两个加数都是 1。", "执行加法得到 2。", "检查 2 是整数。"],
        final_answer="2",
        method_summary="直接相加。",
        pitfalls=["不要漏写进位。"],
    )
    checks = structure_check(content, _exercise())
    assert not [check for check in checks if check.status == "fail"]


def test_numeric_check_detects_false_arithmetic() -> None:
    content = SolutionContent(
        detailed_steps=["1 + 1 = 3"],
        final_answer="3",
    )
    details = {check.name: check for check in structure_check(content, _exercise())}
    assert details["numeric_equations"].status == "failed"


def test_chinese_answer_is_not_wrapped_in_math_mode() -> None:
    rendered = latex_display("最终答案：$x=1$，且答案唯一。")
    assert "\\[" not in rendered
    assert "x=1" in rendered
    assert "最终答案" in rendered


def test_plain_text_escaping_keeps_math() -> None:
    assert latex_text(r"设 $a_1 > 0$，则 100% 成立。") == r"设 $a_1 > 0$，则 100\% 成立。"


def test_multiline_renders_bullets_and_preserves_display_math() -> None:
    rendered = latex_multiline(
        "先做两步：\n- 第一项 $a_1$\n- 第二项 100%\n得到：\n$$\nx = 1\n$$"
    )
    assert "\\begin{itemize}" in rendered
    assert "\\item 第一项 $a_1$" in rendered
    assert "第二项 100\\%" in rendered
    assert "\\[" in rendered and "x = 1" in rendered and "\\]" in rendered
    assert "\n\n" in rendered


def test_watermark_is_applied_to_every_page(tmp_path: Path, app) -> None:
    source = tmp_path / "plain.pdf"
    document = pymupdf.open()
    for index in range(2):
        page = document.new_page(width=595, height=842)
        page.insert_text((72, 100), f"Page {index + 1}", fontsize=18)
    document.save(source)
    document.close()

    result = Watermarker(app.settings).apply(source, tmp_path / "watermarked.pdf")
    assert result.path == tmp_path / "watermarked.pdf"
    assert result.pages == 2
    assert result.text == "全网同名爱做题的大肥鱼"
    with pymupdf.open(result.path) as watermarked:
        assert watermarked.page_count == 2
        for page in watermarked:
            assert page.get_images(full=False)
    assert result.path.stat().st_size > source.stat().st_size
