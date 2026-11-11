"""Catalogue dialogs: books, productions, sale SKUs and prices."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.catalog import CatalogError, CatalogManager, cents_to_yuan, discount_info, yuan_to_cents
from core.logging_setup import get_logger
from gui.context import AppContext
from gui.table_utils import prepare_table, set_rows
from gui.widgets import Card, label, set_variant
from models.schemas import Book, Product, SalesSKU, SkuItem, Subject

logger = get_logger("gui.catalog")
SUBJECTS = [
    ("数学", Subject.MATHEMATICS),
    ("物理", Subject.PHYSICS),
    ("化学", Subject.CHEMISTRY),
    ("计算机", Subject.COMPUTER_SCIENCE),
    ("其他", Subject.OTHER),
]
SUBJECT_BY_VALUE = {item.value: label for label, item in SUBJECTS}
EDITIONS = {"detailed": "详细解析版", "quick": "答案速查版", "both": "详细+速查"}
BOOK_STATUS = {
    "VERIFIED": "已核验",
    "NEEDS_REVIEW": "待人工核对",
    "UNVERIFIED": "未核验",
    "FAILED": "检索失败",
}
SKU_STATUS = {
    "draft": "草稿",
    "ready": "就绪",
    "packaged": "已打包",
    "uploaded": "已上传",
    "delivered": "已交付",
    "disabled": "已停用",
    "NEEDS_REVIEW": "待核对",
}


class BookDialog(QDialog):
    def __init__(self, context: AppContext, book: Book | None = None, parent=None):
        super().__init__(parent)
        self.context = context
        self.book = book
        self.result_book: Book | None = None
        self.setWindowTitle("编辑书目" if book else "新增书目")
        self.resize(720, 620)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        form = QFormLayout()
        form.setSpacing(10)
        self.code = QLineEdit(book.code if book else "")
        self.title = QLineEdit(book.title if book else "")
        self.authors = QLineEdit("、".join(book.authors) if book else "")
        self.original_title = QLineEdit(book.original_title if book else "")
        self.publisher = QLineEdit(book.publisher if book else "")
        self.edition = QLineEdit(book.edition if book else "")
        self.year = QLineEdit(book.publication_year if book else "")
        self.isbn13 = QLineEdit(book.isbn_13 if book else "")
        self.isbn10 = QLineEdit(book.isbn_10 if book else "")
        self.volumes = QLineEdit(str(book.volumes if book else 1))
        self.subject = QComboBox()
        for name, value in SUBJECTS:
            self.subject.addItem(name, value)
        if book:
            index = self.subject.findData(book.subject)
            if index >= 0:
                self.subject.setCurrentIndex(index)
        self.cover = QLineEdit(book.cover_image if book else "")
        self.cover.setReadOnly(True)
        cover_pick = QPushButton("选择文件")
        cover_pick.clicked.connect(self._pick_cover)
        cover_row = QHBoxLayout()
        cover_row.addWidget(self.cover, 1)
        cover_row.addWidget(cover_pick)
        self.cover_source = QLineEdit(book.cover_source_url if book else "")
        self.cover_license = QLineEdit(book.source_license if book else "")
        self.verification = QLineEdit(book.verification_status if book else "NEEDS_REVIEW")
        self.verification.setReadOnly(True)
        self.manual_verified = QCheckBox("我已人工核对书目、版次与封面来源")
        self.manual_verified.setChecked(bool(book and book.verification_status == "VERIFIED"))
        form.addRow("书目编号 *", self.code)
        form.addRow("书名 *", self.title)
        form.addRow("作者（顿号分隔）", self.authors)
        form.addRow("原文书名", self.original_title)
        form.addRow("出版社", self.publisher)
        form.addRow("版次", self.edition)
        form.addRow("出版年份", self.year)
        form.addRow("ISBN-13", self.isbn13)
        form.addRow("ISBN-10", self.isbn10)
        form.addRow("册数", self.volumes)
        form.addRow("学科", self.subject)
        form.addRow("原版封面", cover_row)
        form.addRow("封面来源", self.cover_source)
        form.addRow("使用许可/说明", self.cover_license)
        form.addRow("核验状态", self.verification)
        form.addRow("人工确认", self.manual_verified)
        layout.addLayout(form)

        tools = QHBoxLayout()
        pdf_button = QPushButton("从 PDF 识别")
        pdf_button.clicked.connect(self._extract_pdf)
        lookup_button = set_variant(QPushButton("联网检索书目/封面"), "gold")
        lookup_button.clicked.connect(self._lookup)
        tools.addWidget(pdf_button)
        tools.addWidget(lookup_button)
        tools.addStretch(1)
        layout.addLayout(tools)
        self.hint = label(
            "联网检索使用 DeepSeek Search/Fetch；未配置 Key 时只读取 PDF 元数据，不伪造结果。",
            "Hint",
            True,
        )
        layout.addWidget(self.hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _apply_payload(self, payload: dict) -> None:
        for field, widget in (
            ("title", self.title),
            ("original_title", self.original_title),
            ("publisher", self.publisher),
            ("edition", self.edition),
            ("publication_year", self.year),
            ("isbn_13", self.isbn13),
            ("isbn_10", self.isbn10),
        ):
            value = payload.get(field)
            if value:
                widget.setText(str(value))
        authors = payload.get("authors") or []
        if authors:
            self.authors.setText("、".join(str(a) for a in authors))
        volumes = payload.get("volumes")
        if volumes:
            self.volumes.setText(str(volumes))
        subject = payload.get("subject")
        if subject:
            index = self.subject.findData(Subject(subject) if isinstance(subject, str) else subject)
            if index >= 0:
                self.subject.setCurrentIndex(index)
        cover = payload.get("cover_image")
        if cover:
            self.cover.setText(str(cover))
            self.verification.setText(str(payload.get("verification_status", "NEEDS_REVIEW")))
        if str(payload.get("verification_status", "")) == "VERIFIED":
            self.manual_verified.setChecked(True)
        if payload.get("cover_source_url"):
            self.cover_source.setText(str(payload["cover_source_url"]))
        if payload.get("source_license"):
            self.cover_license.setText(str(payload["source_license"]))

    def _pick_cover(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择原版封面", "", "图片 (*.png *.jpg *.jpeg *.webp)"
        )
        if path:
            self.cover.setText(path)

    def _extract_pdf(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择教材 PDF", "", "PDF (*.pdf)")
        if not path:
            return
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            from core.book_lookup import extract_identity

            hints = extract_identity(Path(path))
            self._apply_payload(hints.as_dict())
            if not self.code.text().strip():
                self.code.setText("BK" + (hints.isbn_13 or "001")[-6:])
            self.hint.setText("已从 PDF 提取候选信息；联网核验后才能标记为已验证。")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "识别失败", str(exc))
        finally:
            QGuiApplication.restoreOverrideCursor()

    def _lookup(self) -> None:
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            service = self.context.book_lookup()
            outcome = service.lookup(
                title=self.title.text().strip(),
                authors=[a for a in self.authors.text().replace("，", "、").split("、") if a.strip()],
                publisher=self.publisher.text().strip(),
                edition=self.edition.text().strip(),
                isbn=self.isbn13.text().strip() or self.isbn10.text().strip(),
            )
            self._apply_payload(outcome.book)
            self.verification.setText(outcome.status)
            if outcome.candidates:
                labels = [
                    f"{c.title} | {c.publisher or '未知出版社'} | {c.isbn_13 or c.isbn_10 or '无 ISBN'} | 分数 {c.score:.0f}"
                    for c in outcome.candidates[:8]
                ]
                choice, ok = self._choose_candidate(labels)
                if ok and choice >= 0:
                    candidate = outcome.candidates[choice]
                    self.manual_verified.setChecked(False)
                    self.verification.setText("NEEDS_REVIEW")
                    self.cover.setText("")
                    self.title.setText(candidate.title or self.title.text())
                    if candidate.publisher:
                        self.publisher.setText(candidate.publisher)
                    if candidate.edition:
                        self.edition.setText(candidate.edition)
                    if candidate.publication_year:
                        self.year.setText(candidate.publication_year)
                    if candidate.isbn_13:
                        self.isbn13.setText(candidate.isbn_13)
                    if candidate.isbn_10:
                        self.isbn10.setText(candidate.isbn_10)
                    if candidate.cover_url:
                        self.cover_source.setText(candidate.cover_url)
                        self.hint.setText(
                            f"已手动选择候选；封面地址：{candidate.cover_url}。"
                            "请核对后填写封面本地路径并勾选人工确认。"
                        )
            if outcome.cover_path:
                self.cover.setText(str(outcome.cover_path))
            if outcome.warnings:
                self.hint.setText("；".join(outcome.warnings[:3]))
            if outcome.status != "VERIFIED":
                self.hint.setText(
                    (self.hint.text() + "　未达到自动核验条件，保存后仍为 NEEDS_REVIEW。").strip()
                )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "检索失败", str(exc))
        finally:
            QGuiApplication.restoreOverrideCursor()

    def _choose_candidate(self, labels: list[str]) -> tuple[int, bool]:
        from PySide6.QtWidgets import QInputDialog

        value, ok = QInputDialog.getItem(
            self, "选择匹配书目", "存在多个候选，请选择正确版本；不选则保留人工核验：", labels, 0, False
        )
        return (labels.index(value) if ok and value in labels else -1), ok

    def _save(self) -> None:
        code = self.code.text().strip().upper()
        title = self.title.text().strip()
        if not code or not title:
            QMessageBox.warning(self, "缺少信息", "书目编号和书名不能为空。")
            return
        authors = [a for a in self.authors.text().replace("，", "、").split("、") if a.strip()]
        try:
            volumes = max(1, int(self.volumes.text().strip() or "1"))
        except ValueError:
            QMessageBox.warning(self, "册数无效", "册数必须是正整数。")
            return
        payload = dict(
            code=code,
            title=title,
            authors=authors,
            original_title=self.original_title.text().strip(),
            publisher=self.publisher.text().strip(),
            edition=self.edition.text().strip(),
            publication_year=self.year.text().strip(),
            isbn_10=self.isbn10.text().strip(),
            isbn_13=self.isbn13.text().strip(),
            volumes=volumes,
            subject=self.subject.currentData(),
        )
        extra = dict(
            cover_image=self.cover.text().strip(),
            cover_source_url=self.cover_source.text().strip(),
            source_license=self.cover_license.text().strip(),
            verification_status="VERIFIED" if self.manual_verified.isChecked() else "NEEDS_REVIEW",
            source_verified=self.manual_verified.isChecked(),
        )
        if self.manual_verified.isChecked() and not extra["cover_image"]:
            QMessageBox.warning(
                self,
                "缺少封面文件",
                "人工确认同时包含封面来源；请先选择封面文件，或取消人工确认。",
            )
            return
        try:
            manager = self.context.catalog()
            if self.book:
                self.context.db.update_book(self.book.id, **payload, **extra)
                self.result_book = self.context.db.get_book(self.book.id)
            else:
                self.result_book = manager.create_book(**payload)
                self.context.db.update_book(self.result_book.id, **extra)
                self.result_book = self.context.db.get_book(self.result_book.id)
            if self.result_book:
                manager.write_book_metadata(self.result_book)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self.accept()


class SingleSkuDialog(QDialog):
    def __init__(self, context: AppContext, book: Book, parent=None):
        super().__init__(parent)
        self.context = context
        self.book = book
        self.result_sku: SalesSKU | None = None
        self.setWindowTitle("新增单册销售 SKU")
        self.resize(620, 400)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.product = QComboBox()
        self.productions = [
            p for p in context.db.list_productions(book.id) if p.book_id == book.id
        ]
        for product in self.productions:
            self.product.addItem(
                f"{product.volume_label or product.title} · {product.exercise_count} 道 · {product.status}",
                product.id,
            )
        self.edition = QComboBox()
        for key, label_text in EDITIONS.items():
            self.edition.addItem(label_text, key)
        self.name = QLineEdit()
        self.code = QLineEdit()
        self.price = QDoubleSpinBox()
        self.price.setRange(0.01, 99999)
        self.price.setDecimals(2)
        self.price.setPrefix("¥ ")
        self.price.setValue(3.99)
        self.original = QDoubleSpinBox()
        self.original.setRange(0, 99999)
        self.original.setDecimals(2)
        self.original.setPrefix("¥ ")
        form.addRow("生产实体 *", self.product)
        form.addRow("版本", self.edition)
        form.addRow("商品名称", self.name)
        form.addRow("SKU 编号", self.code)
        form.addRow("售价 *", self.price)
        form.addRow("原价（可选）", self.original)
        layout.addLayout(form)
        self.hint = label("价格按人民币两位小数保存；套装价不得高于分册原价合计。", "Hint", True)
        layout.addWidget(self.hint)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if not self.productions:
            self.hint.setText("该书目还没有生产实体。请先把已完成的商品绑定到书目。")

    def _save(self) -> None:
        if not self.productions:
            QMessageBox.warning(self, "没有生产实体", "请先绑定一个已完成的生产实体。")
            return
        try:
            self.result_sku = self.context.catalog().create_single_sku(
                book_id=self.book.id,
                product_id=self.product.currentData(),
                name=self.name.text().strip(),
                price=f"{self.price.value():.2f}",
                original_price=f"{self.original.value():.2f}" if self.original.value() else None,
                edition=self.edition.currentData(),
                sku_code=self.code.text().strip(),
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "创建失败", str(exc))
            return
        self.accept()


class BundleDialog(QDialog):
    def __init__(self, context: AppContext, book: Book, parent=None):
        super().__init__(parent)
        self.context = context
        self.book = book
        self.result_sku: SalesSKU | None = None
        self.setWindowTitle("新增套装销售 SKU")
        self.resize(680, 560)
        layout = QVBoxLayout(self)
        self.list = QListWidget()
        productions = context.db.list_productions(book.id)
        for product in productions:
            if product.book_id != book.id:
                continue
            item = QListWidgetItem(
                f"{product.volume_label or product.title} · {product.exercise_count} 道 · {product.status}"
            )
            item.setData(Qt.ItemDataRole.UserRole, product.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.list.addItem(item)
        layout.addWidget(QLabel("选择套装包含的生产实体（至少两个）："))
        layout.addWidget(self.list, 1)
        form = QFormLayout()
        self.name = QLineEdit(f"{book.title} 上下册完整套装")
        self.code = QLineEdit()
        self.price = QDoubleSpinBox()
        self.price.setRange(0.01, 99999)
        self.price.setDecimals(2)
        self.price.setPrefix("¥ ")
        self.price.setValue(5.00)
        self.original = QDoubleSpinBox()
        self.original.setRange(0, 99999)
        self.original.setDecimals(2)
        self.allow_above = QCheckBox("允许套装价高于分册原价合计（需明确确认）")
        form.addRow("套装名称", self.name)
        form.addRow("SKU 编号", self.code)
        form.addRow("套装售价 *", self.price)
        form.addRow("套装原价（留空自动）", self.original)
        form.addRow("", self.allow_above)
        layout.addLayout(form)
        self.hint = label("套装只关联现有 PDF，不重复调用 AI 或生成 PDF。", "Hint", True)
        layout.addWidget(self.hint)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _save(self) -> None:
        items: list[SkuItem] = []
        for index in range(self.list.count()):
            item = self.list.item(index)
            if item.checkState() != Qt.CheckState.Checked:
                continue
            product = self.context.db.get_product(item.data(Qt.ItemDataRole.UserRole))
            if not product:
                continue
            items.append(
                SkuItem(
                    product_id=product.id,
                    volume_label=product.volume_label,
                    edition="detailed",
                    sort_order=len(items),
                )
            )
        try:
            self.result_sku = self.context.catalog().create_bundle(
                book_id=self.book.id,
                items=items,
                name=self.name.text().strip(),
                price=f"{self.price.value():.2f}",
                original_price=f"{self.original.value():.2f}" if self.original.value() else None,
                sku_code=self.code.text().strip(),
                allow_above_parts=self.allow_above.isChecked(),
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "创建失败", str(exc))
            return
        self.accept()


class PriceDialog(QDialog):
    def __init__(self, sku: SalesSKU, parent=None):
        super().__init__(parent)
        self.sku = sku
        self.setWindowTitle(f"修改价格 · {sku.sku_code}")
        self.resize(420, 220)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.price = QDoubleSpinBox()
        self.price.setRange(0.01, 99999)
        self.price.setDecimals(2)
        self.price.setPrefix("¥ ")
        self.price.setValue(sku.price_cents / 100)
        self.original = QDoubleSpinBox()
        self.original.setRange(0, 99999)
        self.original.setDecimals(2)
        self.original.setPrefix("¥ ")
        self.original.setValue(sku.original_price_cents / 100)
        form.addRow("售价", self.price)
        form.addRow("原价", self.original)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple[str, str]:
        return f"{self.price.value():.2f}", f"{self.original.value():.2f}"


class CatalogDialog(QDialog):
    def __init__(self, context: AppContext, parent=None):
        super().__init__(parent)
        self.context = context
        self.current_book_id = ""
        self.setWindowTitle("书目、分册与销售 SKU")
        self.resize(1280, 820)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)
        head = QHBoxLayout()
        head.addWidget(label("三层结构：教材实体 → 生产实体 → 销售 SKU", "PageTitle"))
        head.addStretch(1)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        head.addWidget(refresh)
        layout.addLayout(head)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = Card("教材书目")
        self.book_table = QTableWidget()
        prepare_table(self.book_table, ["编号", "书名", "作者", "出版社", "版次", "核验", "册数"])
        self.book_table.itemSelectionChanged.connect(self._book_selected)
        left.body.addWidget(self.book_table, 1)
        book_buttons = QHBoxLayout()
        add_book = set_variant(QPushButton("新增书目"), "gold")
        edit_book = QPushButton("编辑")
        add_book.clicked.connect(self._add_book)
        edit_book.clicked.connect(self._edit_book)
        book_buttons.addWidget(add_book)
        book_buttons.addWidget(edit_book)
        book_buttons.addStretch(1)
        left.body.addLayout(book_buttons)
        left.setMinimumWidth(460)
        splitter.addWidget(left)

        right = Card("销售 SKU")
        self.sku_table = QTableWidget()
        prepare_table(self.sku_table, ["SKU", "商品", "类型", "分册", "售价", "原价", "优惠", "状态", "启用"])
        self.sku_table.itemSelectionChanged.connect(self._sku_selected)
        right.body.addWidget(self.sku_table, 1)
        self.sku_hint = label("选择一个书目查看销售 SKU。", "Hint", True)
        right.body.addWidget(self.sku_hint)
        actions = QVBoxLayout()
        primary = QHBoxLayout()
        secondary = QHBoxLayout()
        add_single = QPushButton("新增单册")
        add_bundle = QPushButton("新增套装")
        bind = QPushButton("绑定生产实体")
        price = QPushButton("改价")
        images = QPushButton("生成图片")
        delivery = QPushButton("生成交付清单")
        upload = QPushButton("上传 SKU 到网盘")
        share = QPushButton("生成分享链接")
        open_dir = QPushButton("打开 SKU 目录")
        delete = QPushButton("删除")
        add_single.clicked.connect(self._add_single)
        add_bundle.clicked.connect(self._add_bundle)
        bind.clicked.connect(self._bind_production)
        price.clicked.connect(self._change_price)
        images.clicked.connect(self._generate_images)
        delivery.clicked.connect(self._build_delivery)
        upload.clicked.connect(self._upload_sku)
        share.clicked.connect(self._share_sku)
        open_dir.clicked.connect(self._open_sku_dir)
        delete.clicked.connect(self._delete_sku)
        for button in (add_single, add_bundle, bind, price, images):
            primary.addWidget(button)
        primary.addStretch(1)
        for button in (delivery, upload, share, open_dir, delete):
            secondary.addWidget(button)
        secondary.addStretch(1)
        actions.addLayout(primary)
        actions.addLayout(secondary)
        right.body.addLayout(actions)
        splitter.addWidget(right)
        splitter.setSizes([520, 760])
        layout.addWidget(splitter, 1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        close.rejected.connect(self.reject)
        layout.addWidget(close)
        self.refresh()

    def refresh(self) -> None:
        books = self.context.db.list_books()
        rows = []
        for book in books:
            rows.append(
                [
                    book.code,
                    book.title,
                    "、".join(book.authors),
                    book.publisher,
                    book.edition,
                    BOOK_STATUS.get(book.verification_status, book.verification_status),
                    book.volumes,
                ]
            )
        set_rows(self.book_table, rows)
        for row, book in enumerate(books):
            item = self.book_table.item(row, 0)
            if item:
                item.setData(Qt.ItemDataRole.UserRole, book.id)
        self.book_table.resizeColumnsToContents()
        if books:
            target = self.current_book_id or books[0].id
            for row, book in enumerate(books):
                if book.id == target:
                    self.book_table.selectRow(row)
                    break
            self._book_selected()
        else:
            self.current_book_id = ""
            self.sku_table.setRowCount(0)
            self.sku_hint.setText("还没有书目。点击“新增书目”，从 PDF 识别后保存。")

    def _book_selected(self) -> None:
        row = self.book_table.currentRow()
        if row < 0:
            return
        item = self.book_table.item(row, 0)
        if item is None:
            return
        self.current_book_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        self._refresh_skus()

    def _refresh_skus(self) -> None:
        if not self.current_book_id:
            return
        skus = self.context.db.list_skus(self.current_book_id)
        rows = []
        for sku in skus:
            info = discount_info(sku)
            parts = "、".join(item.volume_label for item in sku.items)
            discount = f"省 ¥{info['discount_amount']}" if info["discount_amount_cents"] else "—"
            rows.append(
                [
                    sku.sku_code,
                    sku.name,
                    "套装" if sku.kind == "bundle" else "单册",
                    parts,
                    f"¥{info['price']}",
                    f"¥{info['original_price']}" if info["original_price"] else "—",
                    discount,
                    SKU_STATUS.get(sku.status, sku.status),
                    "是" if sku.enabled else "否",
                ]
            )
        set_rows(self.sku_table, rows)
        for row, sku in enumerate(skus):
            item = self.sku_table.item(row, 0)
            if item:
                item.setData(Qt.ItemDataRole.UserRole, sku.id)
                item.setToolTip(sku.description)
        self.sku_table.resizeColumnsToContents()
        self.sku_hint.setText(
            f"共 {len(skus)} 个 SKU。套装复用分册 PDF；独立上传与交付记录以 SKU 为单位。"
        )

    def _sku_selected(self) -> None:
        sku = self._selected_sku()
        if not sku:
            return
        info = discount_info(sku)
        parts = "、".join(item.volume_label for item in sku.items)
        self.sku_hint.setText(
            f"{sku.sku_code} · {sku.name} · {parts} · 售价 ¥{info['price']}"
            + (f" · 省 ¥{info['discount_amount']}" if info["discount_amount_cents"] else "")
        )

    def _selected_sku(self) -> SalesSKU | None:
        row = self.sku_table.currentRow()
        if row < 0:
            return None
        item = self.sku_table.item(row, 0)
        if item is None:
            return None
        return self.context.db.get_sku(str(item.data(Qt.ItemDataRole.UserRole) or ""))

    def _selected_book(self) -> Book | None:
        return self.context.db.get_book(self.current_book_id) if self.current_book_id else None

    def _add_book(self) -> None:
        dialog = BookDialog(self.context, parent=self)
        if dialog.exec() and dialog.result_book:
            self.current_book_id = dialog.result_book.id
            self.refresh()

    def _edit_book(self) -> None:
        book = self._selected_book()
        if not book:
            return
        dialog = BookDialog(self.context, book, parent=self)
        if dialog.exec():
            self.refresh()

    def _add_single(self) -> None:
        book = self._selected_book()
        if not book:
            QMessageBox.information(self, "请先选择书目", "先选择或新增一个书目。")
            return
        dialog = SingleSkuDialog(self.context, book, self)
        if dialog.exec() and dialog.result_sku:
            self._refresh_skus()

    def _add_bundle(self) -> None:
        book = self._selected_book()
        if not book:
            return
        dialog = BundleDialog(self.context, book, self)
        if dialog.exec() and dialog.result_sku:
            self._refresh_skus()

    def _bind_production(self) -> None:
        book = self._selected_book()
        if not book:
            return
        productions = [p for p in self.context.db.list_products() if not p.book_id]
        if not productions:
            QMessageBox.information(self, "没有可绑定商品", "所有生产实体都已绑定，或还没有完成的生产任务。")
            return
        labels = [f"{p.sku} · {p.title} · {p.status}" for p in productions]
        from PySide6.QtWidgets import QInputDialog

        choice, ok = QInputDialog.getItem(self, "绑定生产实体", "选择要绑定到本书的生产实体：", labels, 0, False)
        if not ok or choice not in labels:
            return
        product = productions[labels.index(choice)]
        volume, ok = QInputDialog.getText(self, "分册名称", "例如：上册 / 下册 / 第一册：", text=product.volume_label)
        if not ok or not volume.strip():
            return
        number = 0
        for index, text in enumerate(("上册", "下册", "第一册", "第二册", "第三册"), start=1):
            if volume.strip() == text:
                number = index
                break
        try:
            self.context.catalog().attach_production(
                book.id,
                product.id,
                volume_label=volume.strip(),
                volume_number=number,
                volume_code=volume.strip()[:1],
            )
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "绑定失败", str(exc))

    def _change_price(self) -> None:
        sku = self._selected_sku()
        if not sku:
            return
        dialog = PriceDialog(sku, self)
        if not dialog.exec():
            return
        price, original = dialog.values()
        try:
            self.context.catalog().set_price(sku, price, original)
            self._refresh_skus()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "改价失败", str(exc))

    def _generate_images(self) -> None:
        sku = self._selected_sku()
        if not sku:
            return
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = self.context.catalog().generate_images(sku)
            QMessageBox.information(
                self,
                "图片已生成",
                f"已生成 {len(result.files)} 张 SKU 图片。\n" + "\n".join(result.warnings),
            )
            self._refresh_skus()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "生成失败", str(exc))
        finally:
            QGuiApplication.restoreOverrideCursor()

    def _build_delivery(self) -> None:
        sku = self._selected_sku()
        if not sku:
            return
        try:
            result = self.context.catalog().build_delivery(sku)
            QMessageBox.information(
                self,
                "交付清单已生成",
                f"状态：{result.status}\n{result.manifest}\n\n" + "\n".join(result.warnings),
            )
            self._refresh_skus()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "生成失败", str(exc))

    def _upload_sku(self) -> None:
        sku = self._selected_sku()
        if not sku:
            return
        ok, reason = self.context.netdisk_configured()
        if not ok:
            QMessageBox.warning(self, "网盘未配置", reason)
            return
        client = self.context.netdisk()
        if not client.has_token():
            QMessageBox.warning(
                self,
                "尚未授权",
                "请先在“设置 → 百度网盘”完成 OAuth 授权，再上传 SKU。",
            )
            return
        if (
            QMessageBox.question(
                self,
                "上传销售 SKU",
                f"将 {sku.sku_code} 的 PDF、7 张商品图和商品信息上传到网盘，"
                "并按大小/可选 SHA-256 校验。继续？",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            records = self.context.catalog().upload_sku(sku)
            verified = sum(1 for record in records if record.status == "verified")
            failed = [record for record in records if record.status != "verified"]
            text = f"共 {len(records)} 个文件，已校验 {verified} 个。"
            if failed:
                text += "\n未通过：" + "、".join(
                    f"{Path(record.local_path).name}（{record.error or record.status}）"
                    for record in failed[:5]
                )
            QMessageBox.information(self, "SKU 上传结果", text)
            self._refresh_skus()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "上传失败", str(exc))
        finally:
            QGuiApplication.restoreOverrideCursor()

    def _share_sku(self) -> None:
        sku = self._selected_sku()
        if not sku:
            return
        ok, reason = self.context.netdisk_configured()
        if not ok:
            QMessageBox.warning(self, "网盘未配置", reason)
            return
        client = self.context.netdisk()
        if not client.has_token():
            QMessageBox.warning(
                self,
                "尚未授权",
                "请先在“设置 → 百度网盘”完成 OAuth 授权，再创建 SKU 分享。",
            )
            return
        if (
            QMessageBox.question(
                self,
                "创建 SKU 分享",
                "将通过百度网盘官方分享接口创建链接。该接口可能属于付费“文件分享服务”，"
                "且链接仍需人工确认提取码和访问权限。继续？",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            share = self.context.catalog().create_share(sku)
            QMessageBox.information(
                self,
                "分享链接已创建（待人工确认）",
                f"链接：{share.url or '接口未返回链接'}\n"
                f"提取码：{share.password or '无'}\n\n"
                "已写入分享记录，但未标记为已验证；请用另一账号实际打开确认。",
            )
            self._refresh_skus()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "分享失败", str(exc))
        finally:
            QGuiApplication.restoreOverrideCursor()

    def _open_sku_dir(self) -> None:
        sku = self._selected_sku()
        if not sku:
            return
        path = self.context.paths.sku_dir(sku.sku_code)
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _delete_sku(self) -> None:
        sku = self._selected_sku()
        if not sku:
            return
        if (
            QMessageBox.question(self, "删除销售 SKU", f"确定删除 {sku.sku_code}？\n生产实体和 PDF 不会被删除。")
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.context.catalog().delete_sku(sku.id)
        self._refresh_skus()
