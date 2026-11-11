"""Dashboard: honest counters and shortcuts into the workflow."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from gui.context import AppContext
from gui.table_utils import prepare_table, set_rows
from gui.widgets import Card, PageHeader, StatCard, label, set_variant

STATUS_LABELS = {
    "draft": "草稿",
    "queued": "排队中",
    "processing": "生产中",
    "completed": "已完成",
    "failed": "失败",
    "paused": "已暂停",
}

JOB_LABELS = {
    "pending": "等待中",
    "running": "运行中",
    "paused": "已暂停",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}


def _money(value: float, currency: str) -> str:
    symbol = {"USD": "$", "CNY": "¥", "EUR": "€"}.get(currency.upper(), "")
    return f"{symbol}{value:,.2f}"


class DashboardPage(QWidget):
    new_product_requested = Signal()
    review_requested = Signal()
    products_requested = Signal()

    def __init__(self, context: AppContext, parent: QWidget | None = None):
        super().__init__(parent)
        self.context = context
        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 22, 26, 18)
        layout.setSpacing(16)
        layout.addWidget(
            PageHeader(
                "工作台",
                "从教材 PDF 到可交付商品：解析、逐题解答、独立复核、排版与云端交付都留有记录。",
            )
        )

        stats = QHBoxLayout()
        stats.setSpacing(12)
        self.products_card = StatCard("商品总数")
        self.exercises_card = StatCard("习题总数")
        self.verified_card = StatCard("已复核习题")
        self.cost_card = StatCard("API 费用")
        for card in (self.products_card, self.exercises_card, self.verified_card, self.cost_card):
            stats.addWidget(card)
        layout.addLayout(stats)

        body = QHBoxLayout()
        body.setSpacing(14)
        recent = Card("最近生产任务")
        self.jobs_table = QTableWidget()
        prepare_table(self.jobs_table, ["商品", "阶段", "状态", "进度", "更新时间"])
        recent.body.addWidget(self.jobs_table)
        body.addWidget(recent, 3)

        side = Card("开始工作")
        side.body.setSpacing(12)
        side.body.addWidget(
            label(
                "新建商品会先检查 PDF 页数与文本层，再进入解析与逐题解答。所有失败都会留在任务记录里，不会伪装成成功。",
                "Hint",
                True,
            )
        )
        new_button = set_variant(QPushButton("新建商品"), "primary")
        new_button.clicked.connect(self.new_product_requested)
        side.body.addWidget(new_button)
        review_button = QPushButton("检查待复核答案")
        review_button.clicked.connect(self.review_requested)
        side.body.addWidget(review_button)
        products_button = QPushButton("打开商品管理")
        products_button.clicked.connect(self.products_requested)
        side.body.addWidget(products_button)

        self.config_label = label("", "Hint", True)
        side.body.addSpacing(8)
        side.body.addWidget(self.config_label)
        side.body.addStretch(1)
        body.addWidget(side, 2)
        layout.addLayout(body, 1)

        self.refresh()

    def refresh(self) -> None:
        stats = self.context.db.dashboard_stats()
        self.products_card.set_value(str(stats["products_total"]), f"生产中 {stats['products_in_progress']} · 已完成 {stats['products_completed']}")
        self.exercises_card.set_value(str(stats["exercises_total"]))
        self.verified_card.set_value(str(stats["exercises_verified"]), f"待人工复核 {stats['exercises_review']}")
        currency = str(self.context.settings.get("budget.currency", "USD"))
        if not stats.get("api_calls"):
            self.cost_card.set_value("—", "尚无 API 调用")
        elif stats["cost_unknown_calls"]:
            hint = f"{stats['cost_unknown_calls']} 次调用未配置价格"
            self.cost_card.set_value(_money(stats["cost"], currency), hint)
        else:
            hint = f"输入 {stats['tokens_in']:,} · 输出 {stats['tokens_out']:,}"
            self.cost_card.set_value(_money(stats["cost"], currency), hint)

        products = {product.id: product for product in self.context.db.list_products()}
        rows = []
        for job in self.context.db.latest_jobs(limit=15):
            product = products.get(job.product_id)
            rows.append(
                [
                    f"{product.sku} {product.title}" if product else job.product_id,
                    job.current_stage or "—",
                    JOB_LABELS.get(job.status.value if hasattr(job.status, "value") else str(job.status), str(job.status)),
                    f"{round(job.progress * 100)}%",
                    (job.finished_at or job.created_at or "")[:16].replace("T", " "),
                ]
            )
        set_rows(self.jobs_table, rows)
        self.jobs_table.resizeColumnsToContents()

        main_model = str(self.context.settings.get("openai.model", "") or "未填写")
        review_model = str(self.context.settings.get("openai.review_model", "") or "未填写")
        ai_ok, ai_reason = self.context.ai_configured()
        secret_state = "已配置" if ai_ok else ai_reason
        self.config_label.setText(
            f"主模型：{main_model}\n复核模型：{review_model}\nAPI 状态：{secret_state}\n"
            f"费用口径：按设置中的每百万 token 价格计算；未填价格时只记 token，不猜金额。"
        )
