"""Deterministic end-to-end pipeline smoke test.

This is intentionally a fixture solver: it proves the pipeline, PDF, images and
records wiring without pretending that a real model produced the answers.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import SettingsManager  # noqa: E402
from core.db import Database  # noqa: E402
from core.paths import AppPaths  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402
from models.schemas import (  # noqa: E402
    ExerciseStatus,
    Product,
    Solution,
    SolutionContent,
    Subject,
    Verification,
    VerificationVerdict,
)


class FixtureSolver:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _content(exercise) -> SolutionContent:
        chapter = int(exercise.raw.get("chapter_number", 1) or 1)
        number = str(exercise.number)
        key = (chapter, number)
        answers = {
            (1, "1"): (
                "零向量唯一。",
                [
                    "设 0 与 0' 都是零向量。",
                    "由零向量的加法性质，0 + 0' = 0。",
                    "同理 0' + 0 = 0'。",
                    "加法交换律给出 0 + 0' = 0' + 0，因此 0 = 0'。",
                ],
            ),
            (1, "2"): (
                "（1）不构成线性空间；（2）构成线性空间。",
                [
                    "（1）取 (1,0,0) 与 (0,1,0)，二者都在集合中，但和 (1,1,0) 不满足 x+y+z=1。",
                    "加法不封闭，所以（1）不构成线性空间。",
                    "（2）若 x+2y=0，则对任意标量 k 仍有 kx+2(ky)=0，数乘封闭。",
                    "若两组都满足 x+2y=0，相加仍满足；零向量 (0,0,0) 也属于集合，故（2）构成线性空间。",
                ],
            ),
            (1, "3"): (
                "α1+α2，α2+α3，α3+α1 线性无关。",
                [
                    "设 c1(α1+α2)+c2(α2+α3)+c3(α3+α1)=0。",
                    "按 α1,α2,α3 合并同类项，得到 (c1+c3)α1+(c1+c2)α2+(c2+c3)α3=0。",
                    "由 α1,α2,α3 线性无关，c1+c3=c1+c2=c2+c3=0。",
                    "解得 c1=c2=c3=0，所以三个和向量线性无关。",
                ],
            ),
            (1, "4"): (
                "秩为 2；一个极大线性无关组为 {(1,2,1),(1,1,0)}。",
                [
                    "第二个向量 (2,4,2)=2(1,2,1)，可由第一个向量线性表示。",
                    "第一个向量与第三个向量 (1,1,0) 不成比例，因而线性无关。",
                    "以这两个向量为列作矩阵，存在二阶非零子式。",
                    "所以秩为 2，{(1,2,1),(1,1,0)} 是极大线性无关组。",
                ],
            ),
            (1, "5"): (
                "任意 n+1 个向量必线性相关。",
                [
                    "取 V 的一组基，它含 n 个向量。",
                    "任意 n+1 个向量都可以由这组基线性表示。",
                    "由替换定理，向量个数超过基的个数时必线性相关。",
                    "因此 V 中任意 n+1 个向量线性相关。",
                ],
            ),
            (2, "1"): (
                "T 在标准基下的矩阵为 [[1,1,0],[0,1,1],[1,0,1]]。",
                [
                    "取标准基 e1=(1,0,0)，e2=(0,1,0)，e3=(0,0,1)。",
                    "T(e1)=(1,0,1)，T(e2)=(1,1,0)，T(e3)=(0,1,1)。",
                    "把像向量按列排成矩阵。",
                    "得到矩阵 [[1,1,0],[0,1,1],[1,0,1]]。",
                ],
            ),
            (2, "2"): (
                "线性变换把零向量映为零向量。",
                [
                    "设 T 是线性变换，0 是零向量。",
                    "由线性性，T(0)=T(0+0)=T(0)+T(0)。",
                    "两边同时减去 T(0)，得到 T(0)=0。",
                    "所以零向量在 T 下的像仍是零向量。",
                ],
            ),
            (2, "3"): (
                "A 可逆当且仅当 det(A)≠0。",
                [
                    "若 A 可逆，则存在 B 使 AB=I。",
                    "取行列式得 det(A)det(B)=det(I)=1。",
                    "因此 det(A)≠0。",
                    "反之若 det(A)≠0，由伴随矩阵公式 A·adj(A)=det(A)I 可得 A 可逆。",
                ],
            ),
        }
        final, steps = answers.get(
            key,
            (
                "见详细推导。",
                [
                    "整理题目条件。",
                    "选择适当的定义或定理。",
                    "完成推导并检查边界情形。",
                    "写出结论。",
                ],
            ),
        )
        return SolutionContent(
            question_restatement=f"本题要求：{exercise.text[:120]}",
            analysis="先判断题目类型，再选择定义、定理或直接计算。",
            knowledge_points=["线性空间的定义", "子空间的判定"] if chapter == 1 else ["线性变换", "矩阵表示"],
            approach="按定义逐项验证，必要时用反例说明不成立。",
            detailed_steps=steps,
            final_answer=final,
            method_summary="先验证条件，再完成封闭性或线性性的论证。",
            pitfalls=["不要只验证一个特例。", "注意零向量与边界情形。"],
            answer_latex="",
            confidence=0.9,
        )

    def solve(self, exercise, product, *, force=False, model=None, prompt_version=None):
        content = self._content(exercise)
        solution = Solution(
            exercise_id=exercise.id,
            product_id=product.id,
            model="fixture-deterministic",
            prompt_version="fixture-1",
            content=content,
            status=ExerciseStatus.AI_DRAFT,
        )
        self.db.add_solution(solution)
        self.db.update_exercise(exercise.id, status=ExerciseStatus.AI_DRAFT)
        return SimpleNamespace(solution=solution, reused=False, error="")


class FixtureVerifier:
    def __init__(self, db: Database):
        self.db = db

    def verify(self, exercise, product, solution):
        verification = Verification(
            exercise_id=exercise.id,
            solution_id=solution.id,
            reviewer_model="fixture-reviewer",
            verdict=VerificationVerdict.PASS,
            details={
                "independent_answer": solution.content.final_answer,
                "comparison": "样例复核器按确定性规则判定一致。",
                "checks": [{"name": "fixture", "result": "passed", "evidence": "deterministic"}],
            },
        )
        self.db.add_verification(verification)
        self.db.update_exercise(exercise.id, status=ExerciseStatus.VERIFIED)
        self.db.execute(
            "UPDATE solutions SET status = ? WHERE id = ?",
            (ExerciseStatus.VERIFIED.value, solution.id),
        )
        return SimpleNamespace(
            verification=verification,
            status=ExerciseStatus.VERIFIED,
            error="",
        )


def main() -> int:
    root = ROOT / ".tmp_pipeline_test"
    if root.exists():
        shutil.rmtree(root)
    paths = AppPaths(root)
    paths.ensure()
    settings = SettingsManager(paths)
    db = Database(paths.database)
    pdf = ROOT / "tests" / "fixtures" / "sample_linear_algebra.pdf"
    if not pdf.exists():
        raise SystemExit(f"fixture missing: {pdf}")

    product = Product(
        sku="PIPE01",
        title="线性代数流水线测试",
        subject=Subject.MATHEMATICS,
        source_title="线性代数测试教材",
        source_author="测试作者",
        source_path=str(pdf),
        version="1.0",
        metadata={
            "cover_note": "流程测试样张：使用确定性样例解答验证生产链路，不代表真实 AI 结果。"
        },
    )
    db.add_product(product)
    pipeline = Pipeline(
        settings,
        db,
        paths,
        solver=FixtureSolver(db),
        verifier=FixtureVerifier(db),
    )
    result = pipeline.start(
        product.id,
        {"upload": False, "share": False, "cleanup": False},
    )
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    print("\nJOB:", result.job.status.value, result.job.message)
    print("PRODUCT:", result.product.status)
    print("FULL PDF:", result.full_pdf)
    print("QUICK PDF:", result.quick_pdf)
    print("IMAGES:", len(result.images))
    print("ERRORS:", result.errors)
    print("WARNINGS:", result.warnings)
    if not result.full_pdf or not Path(result.full_pdf).exists():
        return 2
    if len(result.images) != 7:
        return 3
    if result.product.status not in ("packaged", "awaiting_cloud", "needs_review"):
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
