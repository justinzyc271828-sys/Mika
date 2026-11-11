"""Production monitor: durable job state, progress and an honest log tail."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from gui.context import AppContext
from gui.table_utils import prepare_table, selected_key, set_rows
from gui.widgets import Card, PageHeader, label, set_variant
from gui.workers import PipelineWorker

STAGE_LABELS = {
    "parse": "解析 PDF",
    "chapters": "识别章节",
    "exercises": "识别习题",
    "solve": "AI 逐题解答",
    "verify": "独立复核",
    "pdf": "生成 PDF",
    "images": "生成商品图片",
    "records": "商品信息与清单",
    "upload": "上传网盘",
    "share": "创建分享",
    "cleanup": "清理暂存",
}

JOB_LABELS = {
    "pending": "等待中",
    "running": "运行中",
    "paused": "已暂停",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}


class MonitorPage(QWidget):
    job_finished = Signal(object)

    def __init__(self, context: AppContext, parent: QWidget | None = None):
        super().__init__(parent)
        self.context = context
        self.worker: PipelineWorker | None = None
        self._active_job_id = ""
        self._last_result = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 22, 26, 18)
        layout.setSpacing(16)
        layout.addWidget(
            PageHeader(
                "生产监控",
                "每个阶段都写入 SQLite 检查点。暂停会停在当前批次之后；取消会保留已完成结果，可稍后重试。",
            )
        )

        body = QHBoxLayout()
        body.setSpacing(14)

        left = Card("任务列表")
        self.jobs_table = QTableWidget()
        prepare_table(self.jobs_table, ["商品", "阶段", "状态", "进度", "更新时间"])
        self.jobs_table.itemSelectionChanged.connect(self._show_selected_job)
        left.body.addWidget(self.jobs_table)
        body.addWidget(left, 2)

        right = Card("当前任务")
        self.product_label = label("尚未选择任务", "CardTitle", True)
        self.meta_label = label("", "Hint", True)
        right.body.addWidget(self.product_label)
        right.body.addWidget(self.meta_label)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        right.body.addWidget(self.progress)
        self.progress_text = label("0%", "Hint")
        right.body.addWidget(self.progress_text)
        self.message_label = label("", "", True)
        right.body.addWidget(self.message_label)

        buttons = QHBoxLayout()
        self.pause_button = QPushButton("暂停")
        self.resume_button = QPushButton("继续")
        self.cancel_button = set_variant(QPushButton("取消任务"), "danger")
        self.pause_button.clicked.connect(self._pause)
        self.resume_button.clicked.connect(self._resume)
        self.cancel_button.clicked.connect(self._cancel)
        self.pause_button.setEnabled(False)
        self.resume_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        buttons.addWidget(self.pause_button)
        buttons.addWidget(self.resume_button)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        right.body.addLayout(buttons)

        right.body.addWidget(QLabel("阶段状态"))
        self.stage_table = QTableWidget()
        prepare_table(self.stage_table, ["阶段", "状态"])
        self.stage_table.setMinimumHeight(210)
        right.body.addWidget(self.stage_table)

        right.body.addWidget(QLabel("运行日志（只读）"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(400)
        font = QFont("Consolas", 9)
        self.log_view.setFont(font)
        self.log_view.setMinimumHeight(150)
        right.body.addWidget(self.log_view, 1)
        body.addWidget(right, 3)
        layout.addLayout(body, 1)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tail_log)
        self._timer.start(2000)
        self.refresh()

    # ------------------------------------------------------------------ worker
    def attach_worker(self, worker: PipelineWorker) -> None:
        self.worker = worker
        self._active_job_id = ""
        worker.progress.connect(self._on_progress)
        worker.succeeded.connect(self._on_success)
        worker.failed.connect(self._on_failure)
        worker.finished.connect(self._on_thread_finished)
        self.pause_button.setEnabled(True)
        self.resume_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.setValue(0)
        self.message_label.setText("正在启动…")
        self.refresh()

    def _on_progress(self, data: dict) -> None:
        self._active_job_id = str(data.get("job_id", ""))
        fraction = float(data.get("fraction", 0.0))
        self.progress.setValue(int(max(0.0, min(1.0, fraction)) * 1000))
        self.progress_text.setText(f"{round(fraction * 100)}%")
        stage = STAGE_LABELS.get(str(data.get("stage", "")), str(data.get("stage", "")))
        self.message_label.setText(f"{stage}：{data.get('message', '')}")
        stage_status = data.get("stage_status") or {}
        self._fill_stages(stage_status)
        self.refresh(preserve_selection=True)

    def _on_success(self, result) -> None:
        self._last_result = result
        self.worker = None
        job = getattr(result, "job", None)
        self._active_job_id = job.id if job else ""
        self.progress.setValue(int(float(getattr(job, "progress", 1.0)) * 1000))
        self.progress_text.setText(f"{round(float(getattr(job, 'progress', 1.0)) * 100)}%")
        self.message_label.setText(getattr(job, "message", "任务完成") if job else "任务完成")
        self.pause_button.setEnabled(False)
        self.resume_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.refresh()
        self.job_finished.emit(result)

    def _on_failure(self, message: str) -> None:
        self.worker = None
        self.message_label.setObjectName("Danger")
        self.message_label.setText(f"任务失败：{message}")
        self.pause_button.setEnabled(False)
        self.resume_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.refresh()
        self.job_finished.emit(None)

    def _on_thread_finished(self) -> None:
        self.worker = None

    def _pause(self) -> None:
        if self.worker:
            self.worker.pause()
            self.pause_button.setEnabled(False)
            self.resume_button.setEnabled(True)
            self.message_label.setText("已请求暂停：当前批次结束后停在检查点。")

    def _resume(self) -> None:
        if self.worker:
            self.worker.resume()
            self.pause_button.setEnabled(True)
            self.resume_button.setEnabled(False)
            self.message_label.setText("已继续。")

    def _cancel(self) -> None:
        if self.worker:
            self.worker.cancel()
            self.cancel_button.setEnabled(False)
            self.message_label.setText("已请求取消：正在等待当前步骤安全退出。")

    # -------------------------------------------------------------------- data
    def refresh(self, preserve_selection: bool = False) -> None:
        current = self._active_job_id or selected_key(self.jobs_table)
        products = {product.id: product for product in self.context.db.list_products()}
        rows = []
        jobs = self.context.db.latest_jobs(limit=50)
        for job in jobs:
            product = products.get(job.product_id)
            rows.append(
                [
                    f"{product.sku} {product.title}" if product else job.product_id,
                    STAGE_LABELS.get(job.current_stage, job.current_stage or "—"),
                    JOB_LABELS.get(job.status.value if hasattr(job.status, "value") else str(job.status), str(job.status)),
                    f"{round(job.progress * 100)}%",
                    (job.finished_at or job.created_at or "")[:16].replace("T", " "),
                ]
            )
            if not self._active_job_id:
                self._active_job_id = job.id
        set_rows(self.jobs_table, rows)
        for row, job in enumerate(jobs):
            item = self.jobs_table.item(row, 0)
            if item is not None:
                item.setData(Qt.ItemDataRole.UserRole, job.id)
        if current:
            for row in range(self.jobs_table.rowCount()):
                if self.jobs_table.item(row, 0).data(Qt.ItemDataRole.UserRole) == current:
                    self.jobs_table.selectRow(row)
                    break
        self.jobs_table.resizeColumnsToContents()
        if self._active_job_id:
            self._show_job(self._active_job_id)

    def _show_selected_job(self) -> None:
        job_id = selected_key(self.jobs_table)
        if job_id:
            self._active_job_id = job_id
            self._show_job(job_id)

    def _show_job(self, job_id: str) -> None:
        job = self.context.db.get_job(job_id)
        if job is None:
            return
        self._active_job_id = job.id
        product = self.context.db.get_product(job.product_id)
        self.product_label.setText(f"{product.sku} {product.title}" if product else job.product_id)
        status = JOB_LABELS.get(job.status.value if hasattr(job.status, "value") else str(job.status), str(job.status))
        self.meta_label.setText(
            f"任务 {job.id[:18]}…　状态：{status}　阶段：{STAGE_LABELS.get(job.current_stage, job.current_stage)}\n"
            f"创建：{job.created_at[:19].replace('T', ' ')}　进度：{round(job.progress * 100)}%"
        )
        self.progress.setValue(int(job.progress * 1000))
        self.progress_text.setText(f"{round(job.progress * 100)}%")
        self.message_label.setObjectName("")
        self.message_label.setText(job.message or "")
        self._fill_stages(job.stage_status)
        if job.status.value == "running":
            self.pause_button.setEnabled(True)
            self.resume_button.setEnabled(False)
            self.cancel_button.setEnabled(True)
        elif job.status.value == "paused":
            self.pause_button.setEnabled(False)
            self.resume_button.setEnabled(True)
            self.cancel_button.setEnabled(True)
        else:
            self.pause_button.setEnabled(False)
            self.resume_button.setEnabled(False)
            self.cancel_button.setEnabled(False)

    def _fill_stages(self, stage_status: dict) -> None:
        if not stage_status:
            self.stage_table.setRowCount(0)
            return
        rows = []
        for stage, status in stage_status.items():
            rows.append([STAGE_LABELS.get(stage, stage), str(status)])
        set_rows(self.stage_table, rows)
        self.stage_table.resizeColumnsToContents()

    def _tail_log(self) -> None:
        path = self.context.paths.log_dir / "wenlianjia.log"
        if not path.exists():
            return
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()[-120:]
        except OSError:
            return
        text = "".join(lines)
        if text != self.log_view.toPlainText():
            self.log_view.setPlainText(text)
            self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())
