"""Deterministic quality checks for generated solutions.

These checks cannot prove a proof is correct.  They catch missing structure,
placeholder answers and plainly false arithmetic, and they provide objective
evidence that is recorded alongside the model-based review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from models.schemas import Exercise, ExerciseType, SolutionContent

_NUMERIC_EQUATION = re.compile(
    r"(?<![\w\\])([0-9][0-9\s\.\+\-\*/\(\)\^]*)=\s*([0-9][0-9\s\.\+\-\*/\(\)\^]*)"
)
_PLACEHOLDERS = ("略", "同上", "无法确定", "待补充", "TODO", "此处省略")


@dataclass
class QualityCheck:
    name: str
    status: str  # passed | failed | warning | not_applicable
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class QualityReport:
    checks: list[QualityCheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(check.status == "failed" for check in self.checks)

    @property
    def hard_failures(self) -> list[QualityCheck]:
        return [check for check in self.checks if check.status == "failed"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": [check.as_dict() for check in self.checks],
        }


def _plain(text: str) -> str:
    text = text.replace("\\left", "").replace("\\right", "")
    text = text.replace("\\,", " ").replace("\\;", " ").replace("\\!", "")
    text = re.sub(r"\\text\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\mathrm\{([^{}]*)\}", r"\1", text)
    return text


def _eval_simple(expr: str) -> float | None:
    expr = _plain(expr)
    if not re.fullmatch(r"[0-9\s\.\+\-\*/\(\)\^]+", expr):
        return None
    try:
        value = eval(expr.replace("^", "**"), {"__builtins__": {}}, {})  # noqa: S307 - digit-only expression
        if isinstance(value, (int, float)) and abs(value) < 1e15:
            return float(value)
    except Exception:  # noqa: BLE001 - malformed arithmetic is simply not checkable
        return None
    return None


def numeric_check(content: SolutionContent) -> QualityCheck:
    texts = [content.final_answer, *content.detailed_steps]
    checked = 0
    failures: list[str] = []
    for text in texts:
        for match in _NUMERIC_EQUATION.finditer(_plain(text)):
            left = _eval_simple(match.group(1))
            right = _eval_simple(match.group(2))
            if left is None or right is None:
                continue
            checked += 1
            if abs(left - right) > 1e-9 * max(1.0, abs(left), abs(right)):
                failures.append(match.group(0).strip())
    if checked == 0:
        return QualityCheck("numeric_equations", "not_applicable", "未发现可独立计算的纯数值等式")
    if failures:
        return QualityCheck(
            "numeric_equations",
            "failed",
            f"检查 {checked} 条，{len(failures)} 条不成立：" + "；".join(failures[:5]),
        )
    return QualityCheck("numeric_equations", "passed", f"检查 {checked} 条纯数值等式，均成立")


def structure_check(content: SolutionContent, exercise: Exercise) -> list[QualityCheck]:
    checks: list[QualityCheck] = []
    checks.append(
        QualityCheck(
            "final_answer",
            "passed" if content.final_answer.strip() else "failed",
            "最终答案非空" if content.final_answer.strip() else "最终答案为空",
        )
    )
    minimum = 4 if exercise.exercise_type == ExerciseType.PROOF else 2
    checks.append(
        QualityCheck(
            "steps",
            "passed" if len(content.detailed_steps) >= minimum else "failed",
            f"{len(content.detailed_steps)} 个步骤（最低 {minimum}）",
        )
    )
    total_chars = sum(len(step) for step in content.detailed_steps)
    checks.append(
        QualityCheck(
            "detail_length",
            "passed" if total_chars >= 80 else "warning",
            f"解答正文 {total_chars} 字",
        )
    )
    placeholders = [word for word in _PLACEHOLDERS if word in content.final_answer]
    if placeholders and not content.needs_figure:
        checks.append(
            QualityCheck(
                "placeholders",
                "failed",
                "最终答案包含占位内容：" + "、".join(placeholders),
            )
        )
    else:
        checks.append(QualityCheck("placeholders", "passed", "未发现占位答案"))
    if content.needs_figure:
        checks.append(
            QualityCheck(
                "figure_dependency",
                "warning",
                content.figure_note or "答案依赖题图，需要人工确认图形",
            )
        )
    checks.append(numeric_check(content))
    return checks


def text_similarity(left: str, right: str) -> float:
    """Rough overlap used only to flag suspicious divergence, never as a verdict."""
    import difflib

    a = re.sub(r"\s+", "", _plain(left or ""))
    b = re.sub(r"\s+", "", _plain(right or ""))
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()
