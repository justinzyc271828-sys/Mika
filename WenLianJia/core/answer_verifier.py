"""Independent answer verification.

Two model passes plus deterministic checks:

1. the reviewer solves the question without seeing the candidate;
2. a reconciliation pass compares the two answers and judges the candidate;
3. local checks look for missing structure, placeholders and false arithmetic.

A model's self-reported confidence is never treated as correctness.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from models.schemas import (
    Exercise,
    ExerciseStatus,
    ExerciseType,
    Product,
    Solution,
    Verification,
    VerificationVerdict,
    utc_now,
)
from pydantic import ValidationError

from .ai_client import AiClient, AiError, AiResult
from .ai_solver import format_candidate, select_images
from .config import SettingsManager
from .db import Database
from .logging_setup import get_logger
from .prompts import PromptLibrary
from .quality_control import QualityCheck, QualityReport, structure_check, text_similarity

logger = get_logger("verifier")

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "independent_answer": {"type": "string"},
        "reasoning": {"type": "string"},
        "key_checks": {"type": "array", "items": {"type": "string"}},
        "issues": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
    "required": ["independent_answer", "reasoning", "key_checks", "issues", "confidence"],
}

RECONCILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["pass", "needs_revision", "reject", "inconclusive"],
        },
        "correct_final_answer": {"type": "string"},
        "candidate_issues": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "severity": {"type": "string", "enum": ["low", "medium", "high", "blocker"]},
                    "problem": {"type": "string"},
                    "fix": {"type": "string"},
                },
                "required": ["severity", "problem", "fix"],
            },
        },
        "comparison": {"type": "string"},
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "result": {
                        "type": "string",
                        "enum": ["passed", "failed", "not_applicable"],
                    },
                    "evidence": {"type": "string"},
                },
                "required": ["name", "result", "evidence"],
            },
        },
        "confidence": {"type": "number"},
    },
    "required": [
        "verdict",
        "correct_final_answer",
        "candidate_issues",
        "comparison",
        "checks",
        "confidence",
    ],
}


class VerificationError(RuntimeError):
    pass


@dataclass
class VerifyOutcome:
    verification: Verification | None
    status: ExerciseStatus
    error: str = ""


def _question_block(exercise: Exercise, product: Product) -> dict[str, str]:
    chapter = str(exercise.raw.get("chapter_title", "") or "")
    section = str(exercise.raw.get("section_title", "") or "")
    return {
        "source_title": product.source_title or product.title,
        "chapter": chapter + (f" / {section}" if section else "") or "未识别",
        "number": exercise.number or "未编号",
        "exercise_type": exercise.exercise_type.value,
        "question": exercise.text + (f"\n\nLaTeX：{exercise.latex}" if exercise.latex else ""),
        "image_note": "无" if not exercise.images else f"共 {len(exercise.images)} 张图片",
    }


class AnswerVerifier:
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

    def verify(
        self,
        exercise: Exercise,
        product: Product,
        solution: Solution,
    ) -> VerifyOutcome:
        reviewer_model = str(self.settings.get("openai.review_model", "") or "").strip()
        if not reviewer_model:
            reviewer_model = str(self.settings.get("openai.model", "")).strip()
        if not reviewer_model:
            raise VerificationError("未配置复核模型 ID")
        images = select_images(self.settings, exercise)
        block = _question_block(exercise, product)
        quality = QualityReport(checks=structure_check(solution.content, exercise))

        try:
            independent = self.ai.complete_json(
                model=reviewer_model,
                system=(
                    "你是独立复核教师。你只根据题目本身作答，不能看到候选答案。"
                    "只返回 JSON；不确定时如实说明，不得编造条件。"
                ),
                user=self.prompts.render(
                    "independent_review.md",
                    **{**block, "image_note": block["image_note"]},
                ),
                schema_name="independent_review",
                schema=REVIEW_SCHEMA,
                images=images,
                purpose="review",
                product_id=product.id,
                exercise_id=exercise.id,
            )
            independent_data = dict(independent.data or {})
            independent_text = json.dumps(independent_data, ensure_ascii=False, indent=2)
        except (AiError, ValueError) as exc:
            logger.error("independent review failed for %s: %s", exercise.id, exc)
            return self._record_failure(exercise, solution, quality, f"独立复核失败：{exc}")

        candidate_text = format_candidate(solution)
        reconcile_prompt = self.prompts.render(
            "reconcile_review.md",
            **block,
            candidate=candidate_text,
            independent=independent_text,
        )
        if exercise.exercise_type == ExerciseType.PROOF:
            reconcile_prompt += "\n\n" + self.prompts.load("proof_review.md")

        try:
            reconciled = self.ai.complete_json(
                model=reviewer_model,
                system=(
                    "你是裁决复核员。逐行检查候选答案，不因文风或篇幅判分。"
                    "只返回 JSON。"
                ),
                user=reconcile_prompt,
                schema_name="reconcile_review",
                schema=RECONCILE_SCHEMA,
                images=images,
                purpose="reconcile",
                product_id=product.id,
                exercise_id=exercise.id,
            )
            reconcile_data = dict(reconciled.data or {})
            verdict = self._parse_verdict(reconcile_data.get("verdict"))
        except (AiError, ValueError) as exc:
            logger.error("reconcile failed for %s: %s", exercise.id, exc)
            return self._record_failure(
                exercise,
                solution,
                quality,
                f"对照裁决失败：{exc}",
                independent=independent_data,
                tokens=(independent.tokens_in, independent.tokens_out),
            )

        similarity = text_similarity(
            solution.content.final_answer,
            str(independent_data.get("independent_answer", "")),
        )
        if similarity < 0.32:
            quality.checks.append(
                QualityCheck(
                    "answer_agreement",
                    "warning",
                    f"候选与独立答案文本相似度较低（{similarity:.2f}），请人工确认",
                )
            )

        candidate_issues = list(reconcile_data.get("candidate_issues") or [])
        blockers = [
            issue
            for issue in candidate_issues
            if isinstance(issue, dict) and issue.get("severity") in ("high", "blocker")
        ]
        details: dict[str, Any] = {
            "independent": independent_data,
            "reconcile": reconcile_data,
            "quality": quality.as_dict(),
            "similarity": round(similarity, 3),
            "candidate_issues": candidate_issues,
            "reviewer_model": reviewer_model,
            "image_count": len(images),
        }
        escalate_model = str(self.settings.get("openai.escalation_model", "") or "").strip()
        escalated = False
        if (
            bool(self.settings.get("openai.auto_escalate", False))
            and escalate_model
            and escalate_model != reviewer_model
            and verdict in (VerificationVerdict.NEEDS_REVISION, VerificationVerdict.REJECT)
        ):
            escalated = True
            details["escalation"] = self._escalate(
                exercise, product, reconcile_prompt, escalate_model, images
            )

        status = self._decide(verdict, quality, blockers, bool(escalated))
        if escalated:
            status = ExerciseStatus.NEEDS_REVIEW
        verification = Verification(
            exercise_id=exercise.id,
            solution_id=solution.id,
            method="independent_model+programmatic",
            reviewer_model=reviewer_model,
            verdict=verdict,
            details=details,
            tokens_in=independent.tokens_in + reconciled.tokens_in,
            tokens_out=independent.tokens_out + reconciled.tokens_out,
        )
        self.db.add_verification(verification)
        self.db.update_exercise(exercise.id, status=status)
        self.db.execute(
            "UPDATE solutions SET status=?, updated_at=? WHERE id=?",
            (status.value, utc_now(), solution.id),
        )
        logger.info(
            "verified %s: verdict=%s status=%s similarity=%.2f",
            exercise.id,
            verdict.value,
            status.value,
            similarity,
        )
        return VerifyOutcome(verification=verification, status=status)

    def _escalate(
        self,
        exercise: Exercise,
        product: Product,
        prompt: str,
        model: str,
        images: list[str],
    ) -> dict[str, Any]:
        try:
            result = self.ai.complete_json(
                model=model,
                system="你是更高级别的复核专家，只裁决数学与逻辑正确性。只返回 JSON。",
                user=prompt,
                schema_name="reconcile_review",
                schema=RECONCILE_SCHEMA,
                images=images,
                purpose="escalate",
                product_id=product.id,
                exercise_id=exercise.id,
            )
            data = dict(result.data or {})
            data["_model"] = model
            return data
        except (AiError, ValueError) as exc:
            logger.error("escalation failed for %s: %s", exercise.id, exc)
            return {"error": str(exc), "_model": model}

    def _record_failure(
        self,
        exercise: Exercise,
        solution: Solution,
        quality: QualityReport,
        message: str,
        independent: dict[str, Any] | None = None,
        tokens: tuple[int, int] = (0, 0),
    ) -> VerifyOutcome:
        verification = Verification(
            exercise_id=exercise.id,
            solution_id=solution.id,
            method="independent_model+programmatic",
            reviewer_model=str(self.settings.get("openai.review_model", "") or ""),
            verdict=VerificationVerdict.INCONCLUSIVE,
            details={
                "error": message,
                "independent": independent or {},
                "quality": quality.as_dict(),
            },
            tokens_in=tokens[0],
            tokens_out=tokens[1],
        )
        self.db.add_verification(verification)
        self.db.update_exercise(exercise.id, status=ExerciseStatus.NEEDS_REVIEW)
        self.db.execute(
            "UPDATE solutions SET status=?, updated_at=? WHERE id=?",
            (ExerciseStatus.NEEDS_REVIEW.value, utc_now(), solution.id),
        )
        return VerifyOutcome(
            verification=verification,
            status=ExerciseStatus.NEEDS_REVIEW,
            error=message,
        )

    @staticmethod
    def _parse_verdict(value: Any) -> VerificationVerdict:
        try:
            return VerificationVerdict(str(value))
        except ValueError:
            return VerificationVerdict.INCONCLUSIVE

    @staticmethod
    def _decide(
        verdict: VerificationVerdict,
        quality: QualityReport,
        blockers: list[dict[str, Any]],
        escalated: bool,
    ) -> ExerciseStatus:
        if quality.hard_failures:
            return ExerciseStatus.NEEDS_REVIEW
        if verdict == VerificationVerdict.PASS and not blockers and not escalated:
            return ExerciseStatus.VERIFIED
        return ExerciseStatus.NEEDS_REVIEW
