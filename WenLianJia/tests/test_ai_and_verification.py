"""Solver/verifier contract tests with a deterministic fake model.

The fake proves the application wiring only.  It is always labelled
``fake-deterministic`` and is never presented as a real OpenAI answer.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from core.ai_client import AiClient, AiResult
from core.ai_solver import SolverService
from core.answer_verifier import AnswerVerifier
from models.schemas import (
    Chapter,
    Exercise,
    ExerciseStatus,
    ExerciseType,
    Product,
    Solution,
    SolutionContent,
    Subject,
)


def _solution_payload() -> dict:
    return {
        "question_restatement": "证明零向量唯一。",
        "analysis": "设两个零向量，利用零向量的加法性质比较。",
        "knowledge_points": ["零向量", "向量加法"],
        "approach": "从定义出发，证明两个对象相等。",
        "detailed_steps": [
            "设 0 与 0' 都是零向量。",
            "由零向量的性质，0 + 0' = 0。",
            "同理 0' + 0 = 0'。",
            "加法交换，因此 0 = 0'。",
        ],
        "final_answer": "零向量唯一。",
        "method_summary": "利用零向量的加法性质。",
        "pitfalls": ["不要只验证一个特例。"],
        "answer_latex": "",
        "confidence": 0.9,
        "needs_figure": False,
        "figure_note": "",
    }


class FakeAI:
    def __init__(self, *, verdict: str = "pass", blocker: bool = False):
        self.verdict = verdict
        self.blocker = blocker
        self.calls: list[str] = []

    def complete_json(self, **kwargs) -> AiResult:
        self.calls.append(kwargs["schema_name"])
        if kwargs["schema_name"] == "exercise_solution":
            data = _solution_payload()
        elif kwargs["schema_name"] == "independent_review":
            data = {
                "independent_answer": "零向量唯一。",
                "reasoning": "假设两个零向量，利用加法性质即可证明相等。",
                "key_checks": ["零向量定义", "加法交换律"],
                "issues": [],
                "confidence": 0.9,
            }
        else:
            data = {
                "verdict": self.verdict,
                "correct_final_answer": "零向量唯一。",
                "candidate_issues": (
                    [
                        {
                            "severity": "blocker",
                            "problem": "测试用的阻断问题",
                            "fix": "需要重新求解",
                        }
                    ]
                    if self.blocker
                    else []
                ),
                "comparison": "独立答案与候选答案一致。",
                "checks": [
                    {"name": "结论", "result": "passed", "evidence": "两边均为零向量唯一。"}
                ],
                "confidence": 0.9,
            }
        return AiResult(
            text=json.dumps(data, ensure_ascii=False),
            data=data,
            model=kwargs["model"],
            tokens_in=100,
            tokens_out=80,
            cost=0.001,
            cost_known=True,
            latency_ms=10,
        )


def _seed(app) -> tuple[Product, Exercise]:
    product = Product(
        sku="TST",
        title="测试商品",
        subject=Subject.MATHEMATICS,
        source_title="测试教材",
    )
    app.db.add_product(product)
    chapter = Chapter(product_id=product.id, number=1, title="线性空间", start_page=1, end_page=2)
    exercise = Exercise(
        id="TST-CH01-Q001",
        product_id=product.id,
        chapter_id=chapter.id,
        number="1",
        global_number=1,
        label="第 1 章 第 1 题",
        text="证明线性空间中的零向量唯一。",
        exercise_type=ExerciseType.PROOF,
        status=ExerciseStatus.PENDING,
        raw={"chapter_title": "线性空间", "chapter_number": 1},
    )
    app.db.replace_chapters(product.id, [chapter])
    app.db.replace_exercises(product.id, [exercise])
    return product, exercise


def _content() -> SolutionContent:
    return SolutionContent.model_validate(_solution_payload())


def test_solver_persists_structured_solution(app) -> None:
    product, exercise = _seed(app)
    fake = FakeAI()
    outcome = SolverService(app.settings, app.db, fake).solve(exercise, product)
    assert outcome.reused is False
    assert outcome.solution.content.final_answer == "零向量唯一。"
    assert outcome.solution.model == "gpt-4.1"
    assert fake.calls == ["exercise_solution"]
    stored = app.db.latest_solution(exercise.id)
    assert stored is not None
    assert stored.status == ExerciseStatus.AI_DRAFT
    assert app.db.list_exercises(product.id)[0].status == ExerciseStatus.AI_DRAFT


def test_verifier_passes_consistent_independent_answer(app) -> None:
    product, exercise = _seed(app)
    solution = Solution(
        exercise_id=exercise.id,
        product_id=product.id,
        model="fake-deterministic",
        content=_content(),
        status=ExerciseStatus.AI_DRAFT,
    )
    app.db.add_solution(solution)
    app.db.update_exercise(exercise.id, status=ExerciseStatus.AI_DRAFT)
    fake = FakeAI()
    outcome = AnswerVerifier(app.settings, app.db, fake).verify(exercise, product, solution)
    assert outcome.status == ExerciseStatus.VERIFIED
    assert fake.calls == ["independent_review", "reconcile_review"]
    verification = app.db.latest_verification(exercise.id)
    assert verification is not None
    assert verification.verdict.value == "pass"
    assert app.db.list_exercises(product.id)[0].status == ExerciseStatus.VERIFIED


def test_verifier_blocks_high_severity_finding(app) -> None:
    product, exercise = _seed(app)
    solution = Solution(
        exercise_id=exercise.id,
        product_id=product.id,
        model="fake-deterministic",
        content=_content(),
        status=ExerciseStatus.AI_DRAFT,
    )
    app.db.add_solution(solution)
    app.db.update_exercise(exercise.id, status=ExerciseStatus.AI_DRAFT)
    outcome = AnswerVerifier(app.settings, app.db, FakeAI(blocker=True)).verify(
        exercise, product, solution
    )
    assert outcome.status == ExerciseStatus.NEEDS_REVIEW
    verification = app.db.latest_verification(exercise.id)
    assert verification is not None
    assert verification.details["candidate_issues"][0]["severity"] == "blocker"


def test_cost_is_unknown_until_owner_configures_prices(app) -> None:
    client = AiClient(app.settings, app.secrets, db=app.db, client=object())
    assert client.estimate_cost("gpt-4.1", 1_000_000, 1_000_000) == (0.0, False)
    app.settings.set(
        "budget.prices",
        {"gpt-4.1": {"input_per_million": 2.0, "output_per_million": 8.0}},
    )
    cost, known = client.estimate_cost("gpt-4.1", 1_000_000, 1_000_000)
    assert known is True
    assert cost == 10.0
