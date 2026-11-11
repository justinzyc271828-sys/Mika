"""Product management: real folders, manifests and cloud continuation."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from gui.context import AppContext
from gui.table_utils import prepare_table, selected_key, set_rows
from gui.widgets import Card, PageHeader, label, set_variant

STATUS_LABELS = {
    "draft": "草稿",
    "queued": "排队中",
    "processing": "生产中",
    "completed": "已完成",
    "failed": "失败",
    "needs_review": "待复核",
    "packaged": "本地已打包",
    "awaiting_cloud": "待云端交付",
    "awaiting_verification": "待人工确认分享",
}


class ProductsPage(QWidget):
    continue_cloud = Signal(str, dict)

    def __init__(self, context: AppContext, parent: QWidget | None = None):
        super().__init__(parent)
        self.context = context
        self.current_product_id = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 22, 26, 18)
        layout.setSpacing(14)
        layout.addWidget(
            PageHeader(
                "商品管理",
                "这里只显示真实存在的目录和文件。云端状态以百度返回为准；分享链接需要人工确认接收者能打开。",
            )
        )

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = Card("商品列表")
        self.table = QTableWidget()
        prepare_table(self.table, ["编号", "商品", "状态", "习题", "已复核", "费用", "更新"])
        self.table.itemSelectionChanged.connect(self._selection_changed)
        left.body.addWidget(self.table)
        splitter.addWidget(left)

        right = Card("商品详情")
        self.title_label = label("尚未选择商品", "CardTitle", True)
        right.body.addWidget(self.title_label)
        self.meta_label = label("", "Hint", True)
        right.body.addWidget(self.meta_label)
        buttons = QHBoxLayout()
        self.open_button = QPushButton("打开商品文件夹")
        self.record_button = QPushButton("打开交付记录")
        self.cloud_button = set_variant(QPushButton("继续云端交付"), "gold")
        catalog_button = QPushButton("书目与销售 SKU")
        refresh_button = QPushButton("刷新")
        self.open_button.clicked.connect(self._open_folder)
        self.record_button.clicked.connect(self._open_record)
        self.cloud_button.clicked.connect(self._continue_cloud)
        catalog_button.clicked.connect(self._open_catalog)
        refresh_button.clicked.connect(self.refresh)
        buttons.addWidget(self.open_button)
        buttons.addWidget(self.record_button)
        buttons.addWidget(self.cloud_button)
        buttons.addWidget(catalog_button)
        buttons.addWidget(refresh_button)
        buttons.addStretch(1)
        right.body.addLayout(buttons)

        self.detail = QTextBrowser()
        right.body.addWidget(self.detail, 1)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([620, 760])
        layout.addWidget(splitter, 1)

        self.refresh()

    def refresh(self) -> None:
        products = self.context.db.list_products()
        current = self.current_product_id
        rows = []
        for product in products:
            usage = self.context.db.usage_for_product(product.id)
            cost = f"${usage.get('cost', 0):.2f}" if not usage.get("unknown_cost") else "未计价"
            rows.append(
                [
                    product.sku,
                    product.title,
                    STATUS_LABELS.get(product.status, product.status),
                    product.exercise_count,
                    product.verified_count,
                    cost,
                    (product.updated_at or "")[:16].replace("T", " "),
                ]
            )
        set_rows(self.table, rows)
        for row, product in enumerate(products):
            item = self.table.item(row, 0)
            if item is not None:
                item.setData(Qt.ItemDataRole.UserRole, product.id)
                item.setToolTip(product.id)
        self.table.resizeColumnsToContents()
        if products:
            target = current or products[0].id
            for row, product in enumerate(products):
                if product.id == target:
                    self.table.selectRow(row)
                    break
            self._selection_changed()
        else:
            self.title_label.setText("还没有商品")
            self.meta_label.clear()
            self.detail.setPlainText("从“新建商品”导入第一本教材。")
            self._set_buttons(False)

    def _selection_changed(self) -> None:
        product_id = selected_key(self.table)
        if not product_id:
            return
        self.current_product_id = product_id
        product = self.context.db.get_product(product_id)
        if product is None:
            return
        self._set_buttons(True)
        self.title_label.setText(f"{product.sku} · {product.title}")
        self.meta_label.setText(
            f"教材：《{product.source_title or '未填写'}》　版本：{product.source_edition or '未填写'}\n"
            f"状态：{STATUS_LABELS.get(product.status, product.status)}　"
            f"章节 {product.chapter_count}　习题 {product.exercise_count}　复核 {product.verified_count}　"
            f"待人工 {product.review_required}"
        )
        directory = self.context.product_dir(product)
        usage = self.context.db.usage_for_product(product.id)
        files = self.context.db.list_files(product.id)
        acceptance = self._read_acceptance(product)
        detail = {
            "商品编号": product.sku,
            "商品名称": product.title,
            "生产目录": str(directory),
            "源文件": product.source_path,
            "源文件页数": product.total_pages,
            "源文件 SHA-256": product.source_sha256,
            "版本": product.version,
            "API 调用": {
                "次数": usage.get("calls", 0),
                "输入 token": usage.get("tin", 0),
                "输出 token": usage.get("tout", 0),
                "已知费用": usage.get("cost", 0.0),
                "未知价格调用": usage.get("unknown_cost", 0),
            },
            "已登记文件": [f"{item.kind} · {item.path}" for item in files],
            "验收记录": acceptance,
        }
        self.detail.setPlainText(json.dumps(detail, ensure_ascii=False, indent=2))

    def _read_acceptance(self, product) -> dict:
        path = self.context.product_dir(product) / "07_交付记录" / "验收记录.json"
        if not path.exists():
            return {"状态": "尚未生成", "路径": str(path)}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {"状态": "读取失败", "错误": str(exc), "路径": str(path)}

    def _set_buttons(self, enabled: bool) -> None:
        self.open_button.setEnabled(enabled)
        self.record_button.setEnabled(enabled)
        self.cloud_button.setEnabled(enabled)

    def _open_folder(self) -> None:
        product = self.context.db.get_product(self.current_product_id)
        if product is None:
            return
        directory = self.context.product_dir(product)
        directory.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    def _open_record(self) -> None:
        product = self.context.db.get_product(self.current_product_id)
        if product is None:
            return
        path = self.context.product_dir(product) / "07_交付记录" / "商品交付信息.txt"
        if not path.exists():
            QMessageBox.information(self, "还没有交付记录", "该商品尚未生成交付记录。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _continue_cloud(self) -> None:
        product = self.context.db.get_product(self.current_product_id)
        if product is None:
            return
        ok, reason = self.context.netdisk_configured()
        if not ok:
            QMessageBox.warning(self, "百度网盘未配置", f"{reason}\n请先在设置中填写并完成授权。")
            return
        answer = QMessageBox.question(
            self,
            "继续云端交付",
            "将复用已有解析、答案和 PDF，只执行上传与分享检查。\n分享创建后仍需人工确认链接可用。是否继续？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.continue_cloud.emit(product.id, {"upload": True, "share": True, "cleanup": False})

    def _open_catalog(self) -> None:
        from gui.catalog_dialog import CatalogDialog

        dialog = CatalogDialog(self.context, self)
        dialog.exec()
        self.refresh()
