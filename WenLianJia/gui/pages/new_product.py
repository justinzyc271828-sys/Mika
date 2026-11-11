"""New product import page."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.pdf_parser import PdfError, pdf_inspect
from gui.context import AppContext
from gui.widgets import Card, PageHeader, label, set_variant
from models.schemas import Product, Subject


SUBJECT_LABELS = [
    ("数学", Subject.MATHEMATICS),
    ("物理", Subject.PHYSICS),
    ("化学", Subject.CHEMISTRY),
    ("计算机", Subject.COMPUTER_SCIENCE),
    ("其他", Subject.OTHER),
]


class NewProductPage(QWidget):
    start_requested = Signal(str, dict)

    def __init__(self, context: AppContext, parent: QWidget | None = None):
        super().__init__(parent)
        self.context = context
        self._inspection: dict | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 22, 26, 18)
        layout.setSpacing(16)
        layout.addWidget(
            PageHeader(
                "新建商品",
                "先导入教材 PDF。系统会如实标出文本层、扫描页、页数和目录；扫描页随后走本地 OCR，不会凭空补字。",
            )
        )

        body = QHBoxLayout()
        body.setSpacing(14)

        form_card = Card("商品信息")
        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.sku_edit = QLineEdit()
        self.sku_edit.setPlaceholderText("例如 CJ-001")
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("商品展示名称，例如 线性代数 习题详解")
        self.subject_combo = QComboBox()
        for text, value in SUBJECT_LABELS:
            self.subject_combo.addItem(text, value)
        self.source_title_edit = QLineEdit()
        self.source_title_edit.setPlaceholderText("教材原名")
        self.author_edit = QLineEdit()
        self.edition_edit = QLineEdit()
        self.edition_edit.setPlaceholderText("版次 / 出版社，可留空")
        self.version_edit = QLineEdit("1.0")
        form.addRow("商品编号 *", self.sku_edit)
        form.addRow("商品名称 *", self.title_edit)
        form.addRow("学科", self.subject_combo)
        form.addRow("教材名称 *", self.source_title_edit)
        form.addRow("作者", self.author_edit)
        form.addRow("版本", self.edition_edit)
        form.addRow("解析版本", self.version_edit)
        form_widget = QWidget()
        form_widget.setLayout(form)
        form_widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        form_card.body.addWidget(form_widget)

        pdf_row = QHBoxLayout()
        self.pdf_edit = QLineEdit()
        self.pdf_edit.setPlaceholderText("选择本地教材 PDF")
        browse = QPushButton("选择 PDF…")
        browse.clicked.connect(self._choose_pdf)
        pdf_row.addWidget(self.pdf_edit, 1)
        pdf_row.addWidget(browse)
        form_card.body.addWidget(QLabel("教材 PDF *"))
        form_card.body.addLayout(pdf_row)

        inspect_row = QHBoxLayout()
        self.inspect_button = QPushButton("检查 PDF")
        self.inspect_button.clicked.connect(self._inspect_pdf)
        inspect_row.addWidget(self.inspect_button)
        inspect_row.addStretch(1)
        form_card.body.addLayout(inspect_row)

        self.inspect_box = Card("PDF 检查结果")
        self.inspect_label = label("尚未选择文件。", "Hint", True)
        self.inspect_label.setMinimumHeight(74)
        self.inspect_box.body.addWidget(self.inspect_label)
        form_card.body.addWidget(self.inspect_box)
        form_card.body.addStretch(1)
        body.addWidget(form_card, 3)

        options_card = Card("生产选项")
        options_card.body.setSpacing(10)
        self.upload_check = QCheckBox("完成后上传百度网盘")
        self.share_check = QCheckBox("上传后创建分享链接")
        self.cleanup_check = QCheckBox("满足条件时暂存待清理文件")
        options_card.body.addWidget(self.upload_check)
        options_card.body.addWidget(self.share_check)
        options_card.body.addWidget(self.cleanup_check)

        concurrency_row = QHBoxLayout()
        concurrency_row.addWidget(QLabel("并发题数"))
        self.concurrency_spin = QSpinBox()
        self.concurrency_spin.setRange(1, 12)
        self.concurrency_spin.setValue(int(self.context.settings.get("openai.max_concurrency", 4)))
        concurrency_row.addWidget(self.concurrency_spin)
        concurrency_row.addStretch(1)
        options_card.body.addLayout(concurrency_row)

        options_card.body.addWidget(
            label(
                "云端步骤需要先在设置中填写百度应用信息并完成授权。未配置时会明确跳过，商品仍会完成本地打包，不会伪造上传或分享结果。",
                "Hint",
                True,
            )
        )
        options_card.body.addSpacing(8)
        self.start_button = set_variant(QPushButton("开始生产"), "primary")
        self.start_button.setMinimumHeight(42)
        self.start_button.clicked.connect(self._start)
        options_card.body.addWidget(self.start_button)
        options_card.body.addStretch(1)
        body.addWidget(options_card, 2)

        layout.addLayout(body, 1)

        self._load_settings()
        if not self.context.ai_configured()[0]:
            self.start_button.setEnabled(False)
            self.start_button.setToolTip("先在设置中配置 API Key 与模型")

    def _load_settings(self) -> None:
        self.upload_check.setChecked(bool(self.context.settings.get("netdisk.auto_upload", False)))
        self.share_check.setChecked(bool(self.context.settings.get("netdisk.auto_share", False)))
        self.cleanup_check.setChecked(bool(self.context.settings.get("cleanup.enabled", False)))
        share_ok, _ = self.context.netdisk_configured()
        self.upload_check.setEnabled(share_ok)
        self.share_check.setEnabled(share_ok)
        if not share_ok:
            self.upload_check.setToolTip("百度网盘未配置")
            self.share_check.setToolTip("百度网盘未配置")

    def _choose_pdf(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择教材 PDF", "", "PDF 文件 (*.pdf)")
        if path:
            self.pdf_edit.setText(path)
            self._inspect_pdf()

    def _inspect_pdf(self) -> None:
        raw = self.pdf_edit.text().strip()
        if not raw:
            QMessageBox.information(self, "尚未选择", "请先选择教材 PDF。")
            return
        path = Path(raw)
        try:
            inspection = pdf_inspect(path)
        except PdfError as exc:
            self._inspection = None
            self.inspect_label.setText(f"检查失败：{exc}")
            self.inspect_label.setObjectName("Danger")
            return
        self._inspection = inspection
        mode = "扫描版（将使用 OCR）" if inspection["scanned_likely"] and not inspection["text_layer"] else "文本层可用"
        self.inspect_label.setObjectName("Hint")
        self.inspect_label.setText(
            f"文件：{path.name}\n"
            f"页数：{inspection['page_count']}　　大小：{inspection['size'] / 1024 / 1024:.1f} MB\n"
            f"目录项：{inspection['toc_entries']}　　PDF 类型：{mode}\n"
            f"SHA-256：{inspection['sha256'][:24]}…"
        )
        if not self.source_title_edit.text().strip():
            self.source_title_edit.setText(path.stem)

    def _start(self) -> None:
        if not self.context.ai_configured()[0]:
            QMessageBox.warning(self, "尚未配置模型", "请先在“设置”中填写 OpenAI API Key 和主模型，再开始生产。")
            return
        if self._inspection is None:
            QMessageBox.information(self, "尚未检查", "请先点击“检查 PDF”，确认文件可读。")
            return
        sku = self.sku_edit.text().strip()
        title = self.title_edit.text().strip()
        source_title = self.source_title_edit.text().strip()
        if not sku or not title or not source_title:
            QMessageBox.information(self, "信息不完整", "商品编号、商品名称和教材名称都必须填写。")
            return

        existing = self.context.db.get_product_by_sha(self._inspection["sha256"])
        if existing is not None:
            QMessageBox.warning(
                self,
                "教材已存在",
                f"同一 PDF 已经用于商品“{existing.title}”（{existing.sku}）。\n"
                "请到“商品管理”继续该商品，避免重复付费。",
            )
            return

        product = Product(
            sku=sku,
            title=title,
            subject=self.subject_combo.currentData(),
            source_title=source_title,
            source_author=self.author_edit.text().strip(),
            source_edition=self.edition_edit.text().strip(),
            source_path=str(Path(self.pdf_edit.text().strip()).resolve()),
            source_sha256=self._inspection["sha256"],
            total_pages=int(self._inspection["page_count"]),
            version=self.version_edit.text().strip() or "1.0",
            status="queued",
            metadata={"imported_via": "gui"},
        )
        self.context.db.add_product(product)
        options = {
            "upload": self.upload_check.isChecked(),
            "share": self.share_check.isChecked(),
            "cleanup": self.cleanup_check.isChecked(),
            "max_concurrency": self.concurrency_spin.value(),
        }
        self.start_requested.emit(product.id, options)
        self._reset()

    def _reset(self) -> None:
        self.sku_edit.clear()
        self.title_edit.clear()
        self.source_title_edit.clear()
        self.author_edit.clear()
        self.edition_edit.clear()
        self.pdf_edit.clear()
        self.inspect_label.setText("尚未选择文件。")
        self._inspection = None
