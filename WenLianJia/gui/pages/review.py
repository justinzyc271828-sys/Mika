"""Human review of AI drafts and independent verification results."""

from __future__ import annotations

import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from gui.context import AppContext
from gui.table_utils import prepare_table, selected_key, set_rows
from gui.widgets import Card, PageHeader, label, set_variant
from gui.workers import ExerciseWorker
from models.schemas import ExerciseStatus, SolutionContent, Verification, VerificationVerdict, utc_now

STATUS_LABELS = {
    "pending": "待处理",
    "solving": "解答中",
    "ai_draft": "AI 初稿",
    "verified": "已自动复核",
    "needs_review": "待人工复核",
    "failed": "失败",
    "approved": "已人工确认",
}

TYPE_LABELS = {
    "calculation": "计算题",
    "proof": "证明题",
    "choice": "选择题",
    "fill_blank": "填空题",
    "short_answer": "简答题",
    "programming": "编程题",
    "experiment": "实验题",
    "unknown": "未分类",
}


class ReviewPage(QWidget):
    changed = Signal()

    def __init__(self, context: AppContext, parent: QWidget | None = None):
        super().__init__(parent)
        self.context = context
        self.current_product_id = ""
        self.current_exercise_id = ""
        self.current_solution = None
        self._worker: ExerciseWorker | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 22, 26, 18)
        layout.setSpacing(14)
        layout.addWidget(
            PageHeader(
                "习题复核",
                "先独立复核，再与候选答案比对。人工修改会把题目重新标为“待人工复核”，不会保留旧的通过结论。",
            )
        )

        selector = QHBoxLayout()
        selector.addWidget(QLabel("商品"))
        self.product_combo = QComboBox()
        self.product_combo.setMinimumWidth(330)
        self.product_combo.currentIndexChanged.connect(self._product_changed)
        selector.addWidget(self.product_combo)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        selector.addWidget(refresh)
        selector.addStretch(1)
        self.summary_label = label("", "Hint")
        selector.addWidget(self.summary_label)
        layout.addLayout(selector)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        left = Card("题目列表")
        self.table = QTableWidget()
        prepare_table(self.table, ["题号", "原书页", "题型", "状态"])
        self.table.itemSelectionChanged.connect(self._exercise_changed)
        left.body.addWidget(self.table)
        splitter.addWidget(left)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        right_widget = QWidget()
        right = QVBoxLayout(right_widget)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(12)

        head = Card("当前题目")
        self.heading_label = label("尚未选择题目", "CardTitle", True)
        head.body.addWidget(self.heading_label)
        self.meta_label = label("", "Hint", True)
        head.body.addWidget(self.meta_label)
        self.question_view = QTextBrowser()
        self.question_view.setMinimumHeight(125)
        self.question_view.setMaximumHeight(210)
        head.body.addWidget(self.question_view)
        right.addWidget(head)

        edit_card = Card("答案内容（可人工修改）")
        edit_card.body.addWidget(QLabel("最终答案 *"))
        self.final_edit = QPlainTextEdit()
        self.final_edit.setMaximumHeight(80)
        edit_card.body.addWidget(self.final_edit)
        edit_card.body.addWidget(QLabel("考点分析"))
        self.analysis_edit = QPlainTextEdit()
        self.analysis_edit.setMaximumHeight(72)
        edit_card.body.addWidget(self.analysis_edit)
        edit_card.body.addWidget(QLabel("解题思路"))
        self.approach_edit = QPlainTextEdit()
        self.approach_edit.setMaximumHeight(72)
        edit_card.body.addWidget(self.approach_edit)
        edit_card.body.addWidget(QLabel("详细解答（每行一步）"))
        self.steps_edit = QPlainTextEdit()
        self.steps_edit.setMinimumHeight(135)
        edit_card.body.addWidget(self.steps_edit)
        edit_card.body.addWidget(QLabel("核心知识点（每行一个）"))
        self.knowledge_edit = QPlainTextEdit()
        self.knowledge_edit.setMaximumHeight(62)
        edit_card.body.addWidget(self.knowledge_edit)
        edit_card.body.addWidget(QLabel("方法总结"))
        self.summary_edit = QPlainTextEdit()
        self.summary_edit.setMaximumHeight(62)
        edit_card.body.addWidget(self.summary_edit)
        edit_card.body.addWidget(QLabel("常见错误（每行一条）"))
        self.pitfalls_edit = QPlainTextEdit()
        self.pitfalls_edit.setMaximumHeight(62)
        edit_card.body.addWidget(self.pitfalls_edit)
        right.addWidget(edit_card)

        verify_card = Card("独立复核记录")
        self.verification_view = QTextBrowser()
        self.verification_view.setMinimumHeight(140)
        self.verification_view.setMaximumHeight(240)
        verify_card.body.addWidget(self.verification_view)
        right.addWidget(verify_card)

        actions = Card()
        row = QHBoxLayout()
        self.save_button = set_variant(QPushButton("保存人工修改"), "gold")
        self.approve_button = set_variant(QPushButton("人工确认通过"), "primary")
        self.solve_button = QPushButton("重新 AI 解答")
        self.verify_button = QPushButton("重新独立复核")
        self.save_button.clicked.connect(self._save)
        self.approve_button.clicked.connect(self._approve)
        self.solve_button.clicked.connect(lambda: self._run_ai("solve"))
        self.verify_button.clicked.connect(lambda: self._run_ai("verify"))
        row.addWidget(self.save_button)
        row.addWidget(self.approve_button)
        row.addWidget(self.solve_button)
        row.addWidget(self.verify_button)
        row.addStretch(1)
        actions.body.addLayout(row)
        self.action_status = label("", "Hint", True)
        actions.body.addWidget(self.action_status)
        right.addWidget(actions)
        right.addStretch(1)

        scroll.setWidget(right_widget)
        splitter.addWidget(scroll)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([360, 900])
        layout.addWidget(splitter, 1)

        self.refresh()

    # -------------------------------------------------------------------- load
    def refresh(self) -> None:
        previous_product = self.current_product_id
        self.product_combo.blockSignals(True)
        self.product_combo.clear()
        for product in self.context.db.list_products():
            self.product_combo.addItem(f"{product.sku} · {product.title}", product.id)
        self.product_combo.blockSignals(False)
        if self.product_combo.count() == 0:
            self._clear_all()
            self.summary_label.setText("还没有商品。")
            return
        index = self.product_combo.findData(previous_product)
        self.product_combo.setCurrentIndex(index if index >= 0 else 0)
        self._product_changed()

    def _product_changed(self, preserve: bool = False) -> None:
        product_id = self.product_combo.currentData()
        if not product_id:
            return
        previous = self.current_exercise_id
        self.current_product_id = str(product_id)
        exercises = self.context.db.list_exercises(self.current_product_id)
        rows = []
        counts: dict[str, int] = {}
        for exercise in exercises:
            counts[exercise.status.value] = counts.get(exercise.status.value, 0) + 1
            rows.append(
                [
                    exercise.global_number or exercise.number,
                    exercise.page_start,
                    TYPE_LABELS.get(exercise.exercise_type.value, exercise.exercise_type.value),
                    STATUS_LABELS.get(exercise.status.value, exercise.status.value),
                ]
            )
            # Keep ids outside the visible columns.
        set_rows(self.table, rows)
        for row, exercise in enumerate(exercises):
            item = self.table.item(row, 0)
            if item is not None:
                item.setData(Qt.ItemDataRole.UserRole, exercise.id)
        self.table.resizeColumnsToContents()
        verified = counts.get("verified", 0) + counts.get("approved", 0)
        self.summary_label.setText(f"共 {len(exercises)} 题　已复核 {verified}　待人工 {counts.get('needs_review', 0)}")
        self._clear_detail()
        if not exercises:
            return
        target = previous if preserve else ""
        selected = 0
        if target:
            for row, exercise in enumerate(exercises):
                if exercise.id == target:
                    selected = row
                    break
        self.table.selectRow(selected)
        self._exercise_changed()

    def _exercise_changed(self) -> None:
        exercise_id = selected_key(self.table)
        if not exercise_id:
            return
        self.current_exercise_id = exercise_id
        exercise = next((e for e in self.context.db.list_exercises(self.current_product_id) if e.id == exercise_id), None)
        if exercise is None:
            return
        self.heading_label.setText(
            f"第 {exercise.global_number or exercise.number} 题　{TYPE_LABELS.get(exercise.exercise_type.value, '未分类')}"
        )
        self.meta_label.setText(
            f"原书第 {exercise.page_start} 页　状态：{STATUS_LABELS.get(exercise.status.value, exercise.status.value)}　"
            f"识别置信度：{exercise.confidence:.2f}"
        )
        self.question_view.setPlainText(exercise.text or "（题目文字为空，请检查解析结果）")
        solution = self.context.db.latest_solution(exercise.id)
        self.current_solution = solution
        if solution is None:
            for widget in (
                self.final_edit,
                self.analysis_edit,
                self.approach_edit,
                self.steps_edit,
                self.knowledge_edit,
                self.summary_edit,
                self.pitfalls_edit,
            ):
                widget.clear()
            self.action_status.setText("该题还没有解答，可点击“重新 AI 解答”。")
        else:
            content = solution.content
            self.final_edit.setPlainText(content.final_answer)
            self.analysis_edit.setPlainText(content.analysis)
            self.approach_edit.setPlainText(content.approach)
            self.steps_edit.setPlainText("\n".join(content.detailed_steps))
            self.knowledge_edit.setPlainText("\n".join(content.knowledge_points))
            self.summary_edit.setPlainText(content.method_summary)
            self.pitfalls_edit.setPlainText("\n".join(content.pitfalls))
            self.action_status.setText(
                f"当前答案：{solution.model or '未知模型'}　状态：{STATUS_LABELS.get(solution.status.value, solution.status.value)}"
            )
        verification = self.context.db.latest_verification(exercise.id)
        if verification is None:
            self.verification_view.setPlainText("尚无复核记录。")
        else:
            details = verification.details
            pretty = json.dumps(details, ensure_ascii=False, indent=2) if isinstance(details, dict) else str(details)
            self.verification_view.setPlainText(
                f"结论：{verification.verdict.value}\n复核方式：{verification.method}\n"
                f"复核模型：{verification.reviewer_model}\n创建：{verification.created_at}\n\n{pretty}"
            )
        enabled = solution is not None
        self.save_button.setEnabled(enabled)
        self.approve_button.setEnabled(enabled)
        self.verify_button.setEnabled(enabled)

    # ----------------------------------------------------------------- actions
    def _edited_content(self) -> SolutionContent:
        if self.current_solution is None:
            raise RuntimeError("没有可修改的解答")
        content = self.current_solution.content.model_copy(deep=True)
        content.final_answer = self.final_edit.toPlainText().strip()
        content.analysis = self.analysis_edit.toPlainText().strip()
        content.approach = self.approach_edit.toPlainText().strip()
        content.detailed_steps = [line.strip() for line in self.steps_edit.toPlainText().splitlines() if line.strip()]
        content.knowledge_points = [line.strip() for line in self.knowledge_edit.toPlainText().splitlines() if line.strip()]
        content.method_summary = self.summary_edit.toPlainText().strip()
        content.pitfalls = [line.strip() for line in self.pitfalls_edit.toPlainText().splitlines() if line.strip()]
        return content

    def _save(self) -> None:
        if self.current_solution is None:
            return
        content = self._edited_content()
        if not content.final_answer:
            QMessageBox.information(self, "缺少最终答案", "最终答案不能为空。")
            return
        self.context.db.update_solution(self.current_solution.id, content=content, status=ExerciseStatus.NEEDS_REVIEW)
        self.context.db.update_exercise(self.current_exercise_id, status=ExerciseStatus.NEEDS_REVIEW.value)
        self.action_status.setText("已保存。该题已重新标为“待人工复核”，需要再次复核或人工确认。")
        self._refresh_rows()
        self.changed.emit()

    def _approve(self) -> None:
        if self.current_solution is None:
            return
        content = self._edited_content()
        if not content.final_answer or not content.detailed_steps:
            QMessageBox.information(self, "内容不完整", "人工确认前必须填写最终答案和至少一步详细解答。")
            return
        answer = QMessageBox.question(
            self,
            "确认人工通过",
            "确认已亲自核对题目、推导和最终答案？\n该操作会记录为人工复核证据。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.context.db.update_solution(self.current_solution.id, content=content, status=ExerciseStatus.APPROVED)
        self.context.db.update_exercise(self.current_exercise_id, status=ExerciseStatus.APPROVED.value)
        verification = Verification(
            exercise_id=self.current_exercise_id,
            solution_id=self.current_solution.id,
            method="human_review",
            reviewer_model="owner",
            verdict=VerificationVerdict.PASS,
            details={"note": "人工核对题目、推导与最终答案后通过", "reviewed_at": utc_now()},
        )
        self.context.db.add_verification(verification)
        self.action_status.setText("已记录人工确认通过。")
        self._refresh_rows()
        self.changed.emit()

    def _run_ai(self, mode: str) -> None:
        if not self.current_exercise_id:
            return
        if not self.context.ai_configured()[0]:
            QMessageBox.warning(self, "尚未配置模型", "请先在“设置”中配置 API Key 和模型。")
            return
        self._set_busy(True, "正在调用模型…")
        worker = ExerciseWorker(
            self.context,
            mode=mode,
            product_id=self.current_product_id,
            exercise_id=self.current_exercise_id,
        )
        self._worker = worker
        worker.succeeded.connect(self._ai_success)
        worker.failed.connect(self._ai_failed)
        worker.finished.connect(lambda: self._set_busy(False, ""))
        worker.start()

    def _ai_success(self, message: str) -> None:
        self.action_status.setText(message)
        self._refresh_rows()
        self._exercise_changed()
        self.changed.emit()

    def _ai_failed(self, message: str) -> None:
        self.action_status.setText(f"失败：{message}")
        QMessageBox.warning(self, "模型操作失败", message)

    def _set_busy(self, busy: bool, message: str) -> None:
        for button in (self.save_button, self.approve_button, self.solve_button, self.verify_button):
            button.setEnabled(not busy)
        self.table.setEnabled(not busy)
        if message:
            self.action_status.setText(message)

    def _refresh_rows(self) -> None:
        self._product_changed(preserve=True)

    def _clear_detail(self) -> None:
        self.current_exercise_id = ""
        self.current_solution = None
        self.heading_label.setText("尚未选择题目")
        self.meta_label.clear()
        self.question_view.clear()
        self.verification_view.clear()
        for widget in (
            self.final_edit,
            self.analysis_edit,
            self.approach_edit,
            self.steps_edit,
            self.knowledge_edit,
            self.summary_edit,
            self.pitfalls_edit,
        ):
            widget.clear()
        self.action_status.clear()

    def _clear_all(self) -> None:
        self.current_product_id = ""
        self.table.setRowCount(0)
        self._clear_detail()
