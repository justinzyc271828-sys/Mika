"""Background workers.

The GUI must remain responsive while PDF pages are parsed and model calls run.
No widget is touched from these threads; only Qt signals cross the boundary.
"""

from __future__ import annotations

import traceback
from typing import Any

from PySide6.QtCore import QThread, Signal

from core.ai_solver import SolveError
from core.answer_verifier import VerificationError
from core.pipeline import Pipeline, PipelineControl, PipelineResult
from gui.context import AppContext


class PipelineWorker(QThread):
    progress = Signal(dict)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, context: AppContext, *, product_id: str = "", job_id: str = "", options: dict[str, Any] | None = None):
        super().__init__()
        self.context = context
        self.product_id = product_id
        self.job_id = job_id
        self.options = dict(options or {})
        self.control = PipelineControl()
        self.result: PipelineResult | None = None

    def run(self) -> None:  # pragma: no cover - exercised through the GUI
        try:
            client = self.context.ai_client()
            pipeline = Pipeline(
                self.context.settings,
                self.context.db,
                self.context.paths,
                ai=client,
                on_progress=self.progress.emit,
            )
            if self.job_id:
                self.result = pipeline.run(self.job_id, self.control, self.options)
            else:
                self.result = pipeline.start(self.product_id, self.options, self.control)
            self.succeeded.emit(self.result)
        except Exception as exc:  # noqa: BLE001 - report the real failure to the user
            detail = f"{type(exc).__name__}: {exc}"
            if not str(exc).strip():
                detail = traceback.format_exc(limit=3)
            self.failed.emit(detail)

    def pause(self) -> None:
        self.control.pause()

    def resume(self) -> None:
        self.control.resume()

    def cancel(self) -> None:
        self.control.cancel()


class ExerciseWorker(QThread):
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(self, context: AppContext, *, mode: str, product_id: str, exercise_id: str):
        super().__init__()
        self.context = context
        self.mode = mode
        self.product_id = product_id
        self.exercise_id = exercise_id

    def run(self) -> None:  # pragma: no cover - exercised through the GUI
        try:
            product = self.context.db.get_product(self.product_id)
            exercise = next(
                (item for item in self.context.db.list_exercises(self.product_id) if item.id == self.exercise_id),
                None,
            )
            if product is None or exercise is None:
                raise RuntimeError("商品或习题不存在")
            if self.mode == "solve":
                solver = self.context.solver()
                outcome = solver.solve(exercise, product, force=True)
                if outcome.error:
                    raise SolveError(outcome.error)
                self.succeeded.emit(f"第 {exercise.global_number or exercise.number} 题已生成新答案")
            elif self.mode == "verify":
                solution = self.context.db.latest_solution(exercise.id)
                if solution is None:
                    raise VerificationError("该题还没有解答，无法复核")
                verifier = self.context.verifier()
                outcome = verifier.verify(exercise, product, solution)
                if outcome.error:
                    raise VerificationError(outcome.error)
                self.succeeded.emit(f"第 {exercise.global_number or exercise.number} 题复核完成：{outcome.status.value}")
            else:
                raise RuntimeError(f"未知操作：{self.mode}")
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class ConnectionWorker(QThread):
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(self, context: AppContext, *, mode: str = "openai"):
        super().__init__()
        self.context = context
        self.mode = mode

    def run(self) -> None:  # pragma: no cover - exercised through the GUI
        try:
            if self.mode == "openai":
                client = self.context.ai_client()
                ok, reason = client.configured()
                if not ok:
                    raise RuntimeError(reason)
                model = self.context.settings.get("openai.model", "")
                result = client.check_connection(model)
                if not result.get("ok"):
                    raise RuntimeError(result.get("reason") or "连接失败")
                self.succeeded.emit(
                    f"连接成功，当前 Key 可用模型 {result.get('models_count', 0)} 个"
                )
            else:
                status = self.context.netdisk().status(fetch_user=True)
                if not status.configured:
                    raise RuntimeError(status.message or "百度网盘未配置")
                if status.authorized:
                    self.succeeded.emit(
                        f"已授权：{status.user_name or '未知账号'}（{status.vip_type or '普通用户'}）"
                    )
                else:
                    self.failed.emit(status.message or "尚未完成百度网盘授权")
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class OAuthWorker(QThread):
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(self, context: AppContext, code: str):
        super().__init__()
        self.context = context
        self.code = code

    def run(self) -> None:  # pragma: no cover - exercised through the GUI
        try:
            client = self.context.netdisk()
            client.exchange_code(self.code)
            status = client.status(fetch_user=True)
            self.succeeded.emit(f"授权成功：{status.user_name or '百度网盘账号'}")
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")
