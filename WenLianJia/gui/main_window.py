"""Main window and navigation."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from gui.context import AppContext
from gui.pages.dashboard import DashboardPage
from gui.pages.monitor import MonitorPage
from gui.pages.new_product import NewProductPage
from gui.pages.product_center import ProductCenterPage
from gui.pages.products import ProductsPage
from gui.pages.review import ReviewPage
from gui.pages.settings import SettingsPage
from gui.workers import PipelineWorker


class MainWindow(QMainWindow):
    def __init__(self, context: AppContext):
        super().__init__()
        self.context = context
        self.worker: PipelineWorker | None = None
        self.setWindowTitle("文链家 WenLianJia · 教材习题解析生产工具")
        self.resize(
            int(context.settings.get("ui.window_width", 1440)),
            int(context.settings.get("ui.window_height", 900)),
        )
        self.setMinimumSize(1180, 760)

        root = QWidget()
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(214)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 26, 18, 18)
        side.setSpacing(6)
        brand = QLabel("文链家")
        brand.setObjectName("Brand")
        brand_sub = QLabel("WenLianJia · 习题解析生产")
        brand_sub.setObjectName("BrandSub")
        side.addWidget(brand)
        side.addWidget(brand_sub)
        side.addSpacing(26)

        self.stack = QStackedWidget()
        self.dashboard = DashboardPage(context)
        self.new_product = NewProductPage(context)
        self.monitor = MonitorPage(context)
        self.review = ReviewPage(context)
        self.products = ProductsPage(context)
        self.product_center = ProductCenterPage(context)
        self.settings = SettingsPage(context)
        self.pages = [
            self.dashboard,
            self.new_product,
            self.monitor,
            self.review,
            self.products,
            self.product_center,
            self.settings,
        ]
        for page in self.pages:
            self.stack.addWidget(page)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        labels = ["工作台", "新建商品", "生产监控", "习题复核", "商品管理", "商品中心", "设置"]
        self.nav_buttons: list[QPushButton] = []
        for index, text in enumerate(labels):
            button = QPushButton(text)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setMinimumHeight(43)
            button.clicked.connect(lambda checked=False, i=index: self.go(i))
            self.nav_group.addButton(button, index)
            self.nav_buttons.append(button)
            side.addWidget(button)
        side.addStretch(1)
        version = QLabel("v1.1 · 商品中心与万级商品库")
        version.setObjectName("BrandSub")
        version.setWordWrap(True)
        side.addWidget(version)
        layout.addWidget(sidebar)

        content = QFrame()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        content_layout.addWidget(self.stack)
        layout.addWidget(content, 1)

        self.nav_buttons[0].setChecked(True)
        self.go(0)

        self.dashboard.new_product_requested.connect(lambda: self.go(1))
        self.dashboard.review_requested.connect(lambda: self.go(3))
        self.dashboard.products_requested.connect(lambda: self.go(4))
        self.new_product.start_requested.connect(self.start_pipeline)
        self.products.continue_cloud.connect(lambda product_id, options: self.start_pipeline(product_id, options))
        self.monitor.job_finished.connect(self._job_finished)
        self.review.changed.connect(self._refresh_read_only)
        self.settings.saved.connect(self._settings_saved)

        self.ai_status = QLabel()
        self.ai_status.setContentsMargins(8, 0, 0, 0)
        self.cloud_status = QLabel()
        self.data_status = QLabel(str(context.paths.root))
        self.statusBar().setContentsMargins(8, 2, 8, 2)
        spacer = QLabel("")
        spacer.setFixedWidth(10)
        self.statusBar().addWidget(spacer)
        self.statusBar().addWidget(self.ai_status)
        self.statusBar().addPermanentWidget(self.cloud_status)
        self.statusBar().addPermanentWidget(self.data_status)
        self._refresh_status()
        self.statusBar().showMessage("就绪", 4000)

    # -------------------------------------------------------------- navigation
    def go(self, index: int) -> None:
        if not 0 <= index < len(self.pages):
            return
        self.stack.setCurrentIndex(index)
        self.nav_buttons[index].setChecked(True)
        page = self.pages[index]
        refresh = getattr(page, "refresh", None)
        if callable(refresh):
            try:
                refresh()
            except Exception as exc:  # noqa: BLE001 - a page refresh must not kill the window
                self.statusBar().showMessage(f"刷新页面失败：{exc}", 8000)
        if index == 0:
            self.dashboard.refresh()

    # ---------------------------------------------------------------- pipeline
    def start_pipeline(self, product_id: str, options: dict | None = None, *, job_id: str = "") -> None:
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, "已有任务", "当前已有生产任务在运行。请等待完成或先取消。")
            return
        worker = PipelineWorker(self.context, product_id=product_id, job_id=job_id, options=options)
        self.worker = worker
        self.monitor.attach_worker(worker)
        worker.succeeded.connect(lambda result: self._pipeline_succeeded(result))
        worker.failed.connect(lambda message: self._pipeline_failed(message))
        worker.finished.connect(self._worker_finished)
        self.go(2)
        self.statusBar().showMessage("生产任务已启动", 5000)
        worker.start()

    def _pipeline_succeeded(self, result) -> None:
        job = getattr(result, "job", None)
        if job is not None:
            self.statusBar().showMessage(job.message or "生产任务完成", 15000)
        self._refresh_read_only()

    def _pipeline_failed(self, message: str) -> None:
        self.statusBar().showMessage(f"生产任务失败：{message}", 20000)

    def _worker_finished(self) -> None:
        self.worker = None

    def _job_finished(self, result) -> None:
        self._refresh_read_only()
        if result is not None:
            job = getattr(result, "job", None)
            if job is not None and getattr(job, "message", ""):
                self.statusBar().showMessage(job.message, 12000)

    def _settings_saved(self) -> None:
        self._refresh_status()
        self.new_product._load_settings()
        self.dashboard.refresh()

    def _refresh_read_only(self) -> None:
        try:
            self.dashboard.refresh()
            self.products.refresh()
            self.product_center.refresh()
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"刷新失败：{exc}", 8000)
        self._refresh_status()

    def _refresh_status(self) -> None:
        ai_ok, ai_reason = self.context.ai_configured()
        model = self.context.settings.get("openai.model", "")
        self.ai_status.setText(f"模型：{'已配置' if ai_ok else '未配置'} · {model or '—'}")
        self.ai_status.setToolTip(ai_reason or "API Key 已加密保存")
        try:
            netdisk = self.context.netdisk()
            configured = netdisk.configured()
            reason = "" if configured else "百度网盘应用信息不完整"
            if not configured:
                state = "未配置"
            elif netdisk.has_token():
                state = "已授权"
            else:
                state = "待授权"
            self.cloud_status.setText(f"百度网盘：{state}")
            self.cloud_status.setToolTip(reason or "上传与分享以云端返回为准")
        except Exception as exc:  # noqa: BLE001
            self.cloud_status.setText("百度网盘：状态读取失败")
            self.cloud_status.setToolTip(str(exc))

    # ------------------------------------------------------------------ close
    def closeEvent(self, event: QCloseEvent) -> None:
        if self.worker is not None and self.worker.isRunning():
            answer = QMessageBox.question(
                self,
                "任务仍在运行",
                "生产任务仍在运行。是否请求取消并等待安全退出？\n"
                "当前模型调用可能还需要一段时间；强制结束可能丢失正在进行的结果。",
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.cancel()
            self.statusBar().showMessage("正在等待任务安全退出…")
            if not self.worker.wait(15000):
                QMessageBox.warning(
                    self,
                    "仍在运行",
                    "任务尚未退出，程序不会强制终止线程。请稍后再次关闭。",
                )
                event.ignore()
                return
        event.accept()


def build_window(context: AppContext) -> MainWindow:
    return MainWindow(context)
