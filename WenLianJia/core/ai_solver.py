"""Per-exercise solving service.

One question in, one structured solution out.  The service owns prompt
selection, image attachment, JSON validation, persistence and status changes;
it deliberately does not perform independent verification (that is a separate
call path in :mod:`core.answer_verifier`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from models.schemas import (
    Exercise,
    ExerciseStatus,
    Product,
    Solution,
    SolutionContent,
    Subject,
    utc_now,
)
from pydantic import ValidationError

from .ai_client import AiClient, AiError, AiResult
from .config import SettingsManager
from .db import Database, content_hash, find_reusable_solution
from .logging_setup import get_logger
from .prompts import PromptLibrary

logger = get_logger("solver")

SOLUTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "question_restatement": {"type": "string"},
        "analysis": {"type": "string"},
        "knowledge_points": {"type": "array", "items": {"type": "string"}},
        "approach": {"type": "string"},
        "detailed_steps": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "final_answer": {"type": "string"},
        "method_summary": {"type": "string"},
        "pitfalls": {"type": "array", "items": {"type": "string"}},
        "answer_latex": {"type": "string"},
        "confidence": {"type": "number"},
        "needs_figure": {"type": "boolean"},
        "figure_note": {"type": "string"},
    },
    "required": [
        "question_restatement",
        "analysis",
        "knowledge_points",
        "approach",
        "detailed_steps",
        "final_answer",
        "method_summary",
        "pitfalls",
        "answer_latex",
        "confidence",
        "needs_figure",
        "figure_note",
    ],
}


class SolveError(RuntimeError):
    pass


def select_images(settings: SettingsManager, exercise: Exercise) -> list[str]:
    """Attach only the images the model actually needs, capped by configuration."""
    limit = max(0, int(settings.get("openai.max_images", 2)))
    if limit == 0:
        return []
    candidates: list[str] = []
    scanned = bool(exercise.raw.get("is_scanned"))
    needs_figure = bool(exercise.raw.get("needs_figure"))
    mentions_figure = any(token in exercise.text for token in ("图", "表", "如图", "见图"))
    if scanned or needs_figure or mentions_figure:
        for path in exercise.images:
            if path and Path(path).exists():
                candidates.append(path)
    for page_image in exercise.raw.get("page_images", []) or []:
        if scanned and page_image and Path(str(page_image)).exists():
            candidates.append(str(page_image))
    seen: set[str] = set()
    unique: list[str] = []
    for path in candidates:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique[:limit]


@dataclass
class SolveOutcome:
    solution: Solution
    reused: bool = False
    error: str = ""


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [part.strip(" -•\t") for part in value.splitlines() if part.strip(" -•\t")]
        return parts or ([value.strip()] if value.strip() else [])
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value)]


def normalise_solution_payload(payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload)
    for key in ("knowledge_points", "pitfalls", "detailed_steps"):
        data[key] = _as_list(data.get(key))
    for key in (
        "question_restatement",
        "analysis",
        "approach",
        "final_answer",
        "method_summary",
        "answer_latex",
        "figure_note",
    ):
        data[key] = str(data.get(key, "") or "").strip()
    data["needs_figure"] = bool(data.get("needs_figure", False))
    try:
        data["confidence"] = float(data.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        data["confidence"] = 0.0
    data["confidence"] = max(0.0, min(1.0, data["confidence"]))
    return data


def format_candidate(solution: Solution) -> str:
    content = solution.content
    lines = [
        "最终答案：",
        content.final_answer or "（空）",
        "",
        "解题步骤：",
    ]
    for index, step in enumerate(content.detailed_steps, start=1):
        lines.append(f"{index}. {step}")
    if content.answer_latex:
        lines.extend(["", "答案 LaTeX：", content.answer_latex])
    return "\n".join(lines)


class SolverService:
    def __init__(
        self,
        settings: SettingsManager,
        db: Database,
        ai: AiClient,
        prompts: PromptLibrary | None = None,
    ):
        self.settings = settings
        self.db = db
        self.ai = ai
        self.prompts = prompts or PromptLibrary()

    def solve(
        self,
        exercise: Exercise,
        product: Product,
        *,
        force: bool = False,
        model: str | None = None,
        prompt_version: str | None = None,
    ) -> SolveOutcome:
        prompt_version = prompt_version or str(
            self.settings.get("openai.prompt_version", "1.0")
        )
        model = model or str(self.settings.get("openai.model", "")).strip()
        if not model:
            raise SolveError("未配置主模型 ID")
        source_hash = content_hash(exercise)
        if exercise.source_hash != source_hash:
            self.db.update_exercise(exercise.id, source_hash=source_hash)
            exercise = exercise.model_copy(update={"source_hash": source_hash})
        if not force and bool(self.settings.get("pipeline.reuse_by_hash", True)):
            reusable = find_reusable_solution(self.db, exercise, model, prompt_version)
            if reusable is not None:
                self.db.update_exercise(
                    exercise.id,
                    status=reusable.status,
                )
                logger.info("reused verified solution for %s", exercise.id)
                return SolveOutcome(solution=reusable, reused=True)

        self.db.update_exercise(exercise.id, status=ExerciseStatus.SOLVING)
        system_name, user = self._build_prompt(exercise, product, prompt_version)
        images = select_images(self.settings, exercise)

        last_error = ""
        for attempt in range(2):
            try:
                user_text = user
                if attempt:
                    user_text += (
                        "\n\n注意：上一次输出不是合法 JSON 或缺少必需字段。"
                        "请只返回符合字段要求的 JSON 对象，不要代码围栏。"
                    )
                result = self.ai.complete_json(
                    model=model,
                    system=system_name,
                    user=user_text,
                    schema_name="exercise_solution",
                    schema=SOLUTION_SCHEMA,
                    images=images,
                    purpose="solve",
                    product_id=product.id,
                    exercise_id=exercise.id,
                )
                content = self._validate(result)
                status = (
                    ExerciseStatus.AI_DRAFT
                    if content.final_answer and content.detailed_steps
                    else ExerciseStatus.NEEDS_REVIEW
                )
                solution = Solution(
                    exercise_id=exercise.id,
                    product_id=product.id,
                    model=model,
                    prompt_version=prompt_version,
                    content=content,
                    status=status,
                    tokens_in=result.tokens_in,
                    tokens_out=result.tokens_out,
                    cost=result.cost,
                    cost_known=result.cost_known,
                    latency_ms=result.latency_ms,
                    content_hash=content_hash(exercise),
                )
                self.db.add_solution(solution)
                self.db.update_exercise(exercise.id, status=status)
                return SolveOutcome(solution=solution)
            except (ValidationError, json.JSONDecodeError) as exc:
                last_error = f"输出结构不合格：{exc}"
                logger.warning("solution validation failed for %s: %s", exercise.id, exc)
            except AiError as exc:
                last_error = str(exc)
                logger.error("AI solve failed for %s: %s", exercise.id, exc)
                break
        self.db.update_exercise(exercise.id, status=ExerciseStatus.FAILED)
        raise SolveError(last_error or "解答失败")

    def _build_prompt(
        self, exercise: Exercise, product: Product, prompt_version: str
    ) -> tuple[str, str]:
        subject = product.subject
        if subject == Subject.MATHEMATICS or self._looks_mathematical(exercise):
            name = "mathematics_solver.md"
        else:
            name = "science_solver.md"
        chapter = str(exercise.raw.get("chapter_title", "") or "")
        section = str(exercise.raw.get("section_title", "") or "")
        chapter_text = chapter + (f" / {section}" if section else "")
        image_note = "无" if not exercise.images else f"共 {len(exercise.images)} 张，涉及题目页或原图。"
        text = exercise.text
        if exercise.latex:
            text += f"\n\n已识别 LaTeX：{exercise.latex}"
        user = self.prompts.render(
            name,
            source_title=product.source_title or product.title,
            chapter=chapter_text or "未识别",
            number=exercise.number or "未编号",
            exercise_type=exercise.exercise_type.value,
            question=text,
            image_note=image_note,
        )
        system = (
            "你是严谨的教材习题解答专家。必须只返回 JSON，字段严格遵守提示词；"
            "不得编造题目条件，不得用模型自评代替验证。"
        )
        return system, user

    @staticmethod
    def _looks_mathematical(exercise: Exercise) -> bool:
        text = exercise.text
        tokens = ("证明", "求", "计算", "方程", "函数", "矩阵", "向量", "积分", "导数", "概率", "=")
        return any(token in text for token in tokens)

    @staticmethod
    def _validate(result: AiResult) -> SolutionContent:
        if result.data is None:
            raise SolveError("模型没有返回结构化数据")
        payload = normalise_solution_payload(result.data)
        content = SolutionContent.model_validate(payload)
        if not content.final_answer.strip() and not content.needs_figure:
            raise ValidationError.from_exception_data(
                "SolutionContent",
                [
                    {
                        "type": "missing",
                        "loc": ("final_answer",),
                        "msg": "最终答案为空",
                        "input": "",
                    }
                ],
            )
        return content
