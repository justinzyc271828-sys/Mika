"""商品中心：系列模板、教材、SKU、资产、批量与备份的统一管理界面。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from PySide6.QtCore import Qt
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
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from core.catalog import CatalogManager, cents_to_yuan, yuan_to_cents
from core.library.batch import IMPORT_KINDS, BatchEngine
from core.library.common import LibraryError
from core.library.lifecycle import LIFECYCLE_LABELS
from core.library.service import LibraryService
from gui.table_utils import prepare_table, selected_key, set_rows
from gui.widgets import ButtonRow, Card, PageHeader, StatCard, label, set_variant
from models.schemas import (
    Book,
    BookVersionStatus,
    DeliveryStatus,
    SkuItem,
    SkuLifecycle,
    Subject,
)

SUBJECT_LABELS = {
    "mathematics": "数学",
    "physics": "物理",
    "chemistry": "化学",
    "computer_science": "计算机",
    "other": "其他",
}
SUBJECT_VALUES = {label_: value for value, label_ in SUBJECT_LABELS.items()}

DELIVERY_LABELS = {
    DeliveryStatus.NOT_DELIVERED: "未交付",
    DeliveryStatus.PENDING: "待上传",
    DeliveryStatus.UPLOADED: "已上传",
    DeliveryStatus.SHARE_CREATED: "已分享",
    DeliveryStatus.VERIFIED: "已核验",
    DeliveryStatus.NEEDS_MANUAL: "需人工",
    DeliveryStatus.FAILED: "失败",
}

BATCH_LABELS = {
    "import_books": "导入教材",
    "import_skus": "导入 SKU",
    "update_price": "批量改价",
    "update_category": "批量改分类",
    "add_tags": "批量加标签",
    "enable_skus": "批量启用 SKU",
    "disable_skus": "批量停用 SKU",
    "update_version": "批量换版本",
    "generate_intro": "批量生成介绍",
    "generate_images": "批量生成图片",
    "upload_netdisk": "批量上传网盘",
    "check_share": "批量检查分享",
}
BATCH_VALUES = {label_: key for key, label_ in BATCH_LABELS.items()}

BOOK_STATUS_LABELS = {"ACTIVE": "启用", "ARCHIVED": "归档"}


def _money(cents: int | None) -> str:
    if cents is None:
        return ""
    return f"¥{cents_to_yuan(int(cents))}"


def _money_value(text: str) -> int | None:
    text = (text or "").strip().replace("¥", "").replace("￥", "")
    if not text:
        return None
    return yuan_to_cents(text)


def _fill_combo(combo: QComboBox, items: Iterable[Any], label_attr: str = "name", data_attr: str = "id", empty: str = "") -> None:
    combo.clear()
    if empty:
        combo.addItem(empty, "")
    for item in items:
        text = getattr(item, label_attr, "") or str(item)
        combo.addItem(str(text), getattr(item, data_attr, ""))


def _error(parent: QWidget, title: str, exc: Exception) -> None:
    QMessageBox.warning(parent, title, str(exc))


def _button(text: str, slot) -> QPushButton:
    button = QPushButton(text)
    button.clicked.connect(slot)
    return button


def _confirm(parent: QWidget, title: str, text: str) -> bool:
    return (
        QMessageBox.question(parent, title, text, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        == QMessageBox.StandardButton.Yes
    )


# ------------------------------------------------------------------------- dialogs


class BookDialog(QDialog):
    def __init__(self, service: LibraryService, book: Book | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.service = service
        self.book = book
        self.setWindowTitle("编辑教材" if book else "新建教材")
        self.resize(560, 640)
        self.form = QFormLayout(self)
        self.code = QLineEdit(book.code if book else "")
        self.title = QLineEdit(book.title if book else "")
        self.authors = QLineEdit("；".join(book.authors) if book else "")
        self.publisher = QLineEdit(book.publisher if book else "")
        self.edition = QLineEdit(book.edition if book else "")
        self.year = QLineEdit(book.publication_year if book else "")
        self.isbn13 = QLineEdit(book.isbn_13 if book else "")
        self.isbn10 = QLineEdit(book.isbn_10 if book else "")
        self.volumes = QSpinBox()
        self.volumes.setRange(1, 40)
        self.volumes.setValue(book.volumes if book else 1)
        self.subject = QComboBox()
        for value, text in SUBJECT_LABELS.items():
            self.subject.addItem(text, value)
        if book:
            index = self.subject.findData(book.subject.value if hasattr(book.subject, "value") else book.subject)
            if index >= 0:
                self.subject.setCurrentIndex(index)
        self.family = QComboBox()
        self.family.addItem("默认系列", "")
        for item in service.list_families():
            self.family.addItem(item.name, item.id)
        if book and book.family_id:
            index = self.family.findData(book.family_id)
            if index >= 0:
                self.family.setCurrentIndex(index)
        self.category = QComboBox()
        self.category.addItem("未分类", "")
        for item in service.list_categories():
            self.category.addItem(item.name, item.id)
        if book and book.category_id:
            index = self.category.findData(book.category_id)
            if index >= 0:
                self.category.setCurrentIndex(index)
        self.overrides = QPlainTextEdit(json.dumps(book.template_overrides if book else {}, ensure_ascii=False, indent=2))
        self.overrides.setMaximumHeight(110)
        self.status = QComboBox()
        self.status.addItem("启用", "ACTIVE")
        self.status.addItem("归档", "ARCHIVED")
        if book:
            self.status.setCurrentIndex(1 if book.status == "ARCHIVED" else 0)
        self.form.addRow("教材编号 *", self.code)
        self.form.addRow("书名 *", self.title)
        self.form.addRow("作者（分号分隔）", self.authors)
        self.form.addRow("出版社", self.publisher)
        self.form.addRow("版次", self.edition)
        self.form.addRow("出版年", self.year)
        self.form.addRow("ISBN-13", self.isbn13)
        self.form.addRow("ISBN-10", self.isbn10)
        self.form.addRow("册数", self.volumes)
        self.form.addRow("学科", self.subject)
        self.form.addRow("商品系列", self.family)
        self.form.addRow("分类", self.category)
        self.form.addRow("模板覆盖（JSON）", self.overrides)
        self.form.addRow("状态", self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        self.form.addRow(buttons)

    def _save(self) -> None:
        try:
            overrides = json.loads(self.overrides.toPlainText() or "{}")
            if not isinstance(overrides, dict):
                raise ValueError("模板覆盖必须是 JSON 对象")
            fields = {
                "code": self.code.text().strip(),
                "title": self.title.text().strip(),
                "authors": [part.strip() for part in self.authors.text().replace("；", ";").split(";") if part.strip()],
                "publisher": self.publisher.text().strip(),
                "edition": self.edition.text().strip(),
                "publication_year": self.year.text().strip(),
                "isbn_13": self.isbn13.text().strip(),
                "isbn_10": self.isbn10.text().strip(),
                "volumes": self.volumes.value(),
                "subject": self.subject.currentData(),
                "family_id": self.family.currentData(),
                "category_id": self.category.currentData(),
                "template_overrides": overrides,
                "status": self.status.currentData(),
            }
            if self.book is None:
                fields.pop("status", None)
                self.service.create_book(**fields)
            else:
                self.service.update_book(self.book.id, **fields)
            self.accept()
        except Exception as exc:  # noqa: BLE001
            _error(self, "保存教材失败", exc)


class SkuDialog(QDialog):
    def __init__(self, service: LibraryService, sku=None, parent: QWidget | None = None):
        super().__init__(parent)
        self.service = service
        self.sku = sku
        self.setWindowTitle("编辑 SKU" if sku else "新建 SKU")
        self.resize(580, 620)
        form = QFormLayout(self)
        self.kind = QComboBox()
        self.kind.addItem("单册", "single")
        self.kind.addItem("套装", "bundle")
        self.book = QComboBox()
        for book in service.list_books(page_size=500).items:
            self.book.addItem(f"{book.code} · {book.title}", book.id)
        if sku:
            index = self.book.findData(sku.book_id)
            if index >= 0:
                self.book.setCurrentIndex(index)
        self.book.currentIndexChanged.connect(self._load_products)
        self.products = QComboBox()
        self.bundle_list = QListWidget()
        self.bundle_list.setMaximumHeight(120)
        self.bundle_list.setVisible(False)
        self.name = QLineEdit(sku.name if sku else "")
        self.price = QDoubleSpinBox()
        self.price.setRange(0.01, 999999.0)
        self.price.setDecimals(2)
        self.price.setValue(sku.price_cents / 100 if sku else 9.9)
        self.original = QDoubleSpinBox()
        self.original.setRange(0.0, 999999.0)
        self.original.setDecimals(2)
        self.original.setValue(sku.original_price_cents / 100 if sku else 0.0)
        self.edition = QComboBox()
        self.edition.addItem("详细解析", "detailed")
        self.edition.addItem("精简版", "quick")
        self.edition.addItem("两者", "both")
        if sku:
            index = self.edition.findData(sku.items[0].edition if sku.items else "detailed")
            if index >= 0:
                self.edition.setCurrentIndex(index)
        self.lifecycle = QComboBox()
        for value, text in LIFECYCLE_LABELS.items():
            self.lifecycle.addItem(text, value)
        if sku:
            index = self.lifecycle.findData(sku.lifecycle.value)
            if index >= 0:
                self.lifecycle.setCurrentIndex(index)
        self.version = QComboBox()
        self.version.addItem("不使用版本", "")
        self.description = QPlainTextEdit(sku.description if sku else "")
        self.description.setMaximumHeight(80)
        self.code = QLineEdit(sku.sku_code if sku else "")
        self.code.setPlaceholderText("留空自动生成")
        form.addRow("类型", self.kind)
        form.addRow("教材 *", self.book)
        form.addRow("单册分册", self.products)
        form.addRow("套装分册（勾选）", self.bundle_list)
        form.addRow("SKU 编号", self.code)
        form.addRow("名称", self.name)
        form.addRow("售价（元）*", self.price)
        form.addRow("原价（元）", self.original)
        form.addRow("交付版本", self.edition)
        form.addRow("生命周期", self.lifecycle)
        form.addRow("生产版本", self.version)
        form.addRow("描述", self.description)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self.book.currentIndexChanged.connect(self._load_versions)
        self._load_products()
        self._load_versions()
        if sku:
            self._kind_changed(1 if sku.kind == "bundle" else 0)
            self._check_bundle_items(sku)
            index = self.version.findData(sku.version_id or "")
            if index >= 0:
                self.version.setCurrentIndex(index)

    def _kind_changed(self, index: int) -> None:
        bundle = self.kind.currentData() == "bundle"
        self.products.setVisible(not bundle)
        self.bundle_list.setVisible(bundle)

    def _load_products(self) -> None:
        self.products.clear()
        self.bundle_list.clear()
        book_id = self.book.currentData()
        if not book_id:
            return
        for product in self.service.db.list_productions(book_id):
            self.products.addItem(
                f"{product.volume_label or product.volume_code} · {product.volume_code or product.id}",
                product.id,
            )
            item = QListWidgetItem(
                f"{product.volume_label or product.volume_code} · {product.volume_code or product.id}"
            )
            item.setData(Qt.ItemDataRole.UserRole, product.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.bundle_list.addItem(item)

    def _load_versions(self) -> None:
        self.version.clear()
        self.version.addItem("不使用版本", "")
        book_id = self.book.currentData()
        if not book_id:
            return
        for version in self.service.list_versions(book_id):
            self.version.addItem(version.version_label, version.id)

    def _check_bundle_items(self, sku) -> None:
        wanted = {item.product_id for item in sku.items}
        for index in range(self.bundle_list.count()):
            item = self.bundle_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) in wanted:
                item.setCheckState(Qt.CheckState.Checked)

    def _save(self) -> None:
        try:
            book_id = self.book.currentData()
            if not book_id:
                raise LibraryError("请选择教材")
            price = self.price.value()
            original = self.original.value() or None
            common = {
                "name": self.name.text().strip(),
                "price": price,
                "original_price": original,
                "lifecycle": self.lifecycle.currentData(),
                "version_id": self.version.currentData() or "",
                "description": self.description.toPlainText().strip(),
            }
            if self.sku is None:
                if self.kind.currentData() == "bundle":
                    items = []
                    for index in range(self.bundle_list.count()):
                        item = self.bundle_list.item(index)
                        if item.checkState() == Qt.CheckState.Checked:
                            product_id = item.data(Qt.ItemDataRole.UserRole)
                            product = next(
                                (p for p in self.service.db.list_productions(book_id) if p.id == product_id),
                                None,
                            )
                            items.append(
                                SkuItem(
                                    product_id=product_id,
                                    volume_label=product.volume_label if product else "",
                                    edition=self.edition.currentData(),
                                )
                            )
                    self.service.create_bundle(
                        book_id=book_id,
                        items=items,
                        sku_code=self.code.text().strip(),
                        **common,
                    )
                else:
                    product_id = self.products.currentData()
                    if not product_id:
                        raise LibraryError("请选择分册")
                    self.service.create_single_sku(
                        book_id=book_id,
                        product_id=product_id,
                        sku_code=self.code.text().strip(),
                        edition=self.edition.currentData(),
                        **common,
                    )
            else:
                self.service.update_sku(self.sku.id, **common)
            self.accept()
        except Exception as exc:  # noqa: BLE001
            _error(self, "保存 SKU 失败", exc)


class FamilyDialog(QDialog):
    def __init__(self, service: LibraryService, family=None, parent: QWidget | None = None):
        super().__init__(parent)
        self.service = service
        self.family = family
        self.setWindowTitle("编辑商品系列" if family else "新建商品系列")
        self.resize(480, 360)
        form = QFormLayout(self)
        self.code = QLineEdit(family.code if family else "")
        self.name = QLineEdit(family.name if family else "")
        self.brand = QLineEdit(family.brand_name if family else "")
        self.opacity = QDoubleSpinBox()
        self.opacity.setRange(0.0, 1.0)
        self.opacity.setSingleStep(0.01)
        self.opacity.setValue(float(family.watermark.get("opacity", 0.18)) if family else 0.18)
        self.round = QSpinBox()
        self.round.setRange(1, 10000)
        self.round.setValue(int(family.pricing.get("round_cents", 100)) if family else 100)
        self.discount = QDoubleSpinBox()
        self.discount.setRange(0.0, 1.0)
        self.discount.setSingleStep(0.01)
        self.discount.setValue(float(family.pricing.get("bundle_discount", 0.9)) if family else 0.9)
        self.default = QCheckBox("设为默认系列")
        self.default.setChecked(bool(family.is_default) if family else False)
        form.addRow("系列编号", self.code)
        form.addRow("系列名称", self.name)
        form.addRow("品牌名称", self.brand)
        form.addRow("水印透明度", self.opacity)
        form.addRow("价格取整（分）", self.round)
        form.addRow("套装折扣", self.discount)
        form.addRow("", self.default)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _save(self) -> None:
        try:
            if self.family is None:
                family = self.service.create_family(
                    code=self.code.text().strip(),
                    name=self.name.text().strip(),
                    brand_name=self.brand.text().strip(),
                    watermark={"opacity": self.opacity.value()},
                    pricing={
                        "round_cents": self.round.value(),
                        "bundle_discount": self.discount.value(),
                    },
                )
                if self.default.isChecked():
                    self.service.set_default_family(family.id)
            else:
                watermark = dict(self.family.watermark)
                watermark["opacity"] = self.opacity.value()
                pricing = dict(self.family.pricing)
                pricing["round_cents"] = self.round.value()
                pricing["bundle_discount"] = self.discount.value()
                self.service.update_family(
                    self.family.id,
                    code=self.code.text().strip(),
                    name=self.name.text().strip(),
                    brand_name=self.brand.text().strip(),
                    watermark=watermark,
                    pricing=pricing,
                )
                if self.default.isChecked():
                    self.service.set_default_family(self.family.id)
            self.accept()
        except Exception as exc:  # noqa: BLE001
            _error(self, "保存系列失败", exc)


class CategoryDialog(QDialog):
    def __init__(self, service: LibraryService, category=None, parent: QWidget | None = None):
        super().__init__(parent)
        self.service = service
        self.category = category
        self.setWindowTitle("编辑分类" if category else "新建分类")
        form = QFormLayout(self)
        self.code = QLineEdit(category.code if category else "")
        self.name = QLineEdit(category.name if category else "")
        self.parent_box = QComboBox()
        self.parent_box.addItem("顶级分类", "")
        for item in service.list_categories():
            if category and item.id == category.id:
                continue
            self.parent_box.addItem(item.name, item.id)
        if category and category.parent_id:
            index = self.parent_box.findData(category.parent_id)
            if index >= 0:
                self.parent_box.setCurrentIndex(index)
        self.sort = QSpinBox()
        self.sort.setRange(0, 9999)
        self.sort.setValue(category.sort_order if category else 0)
        form.addRow("分类编号", self.code)
        form.addRow("分类名称", self.name)
        form.addRow("上级分类", self.parent_box)
        form.addRow("排序", self.sort)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _save(self) -> None:
        try:
            if self.category is None:
                self.service.create_category(
                    code=self.code.text().strip(),
                    name=self.name.text().strip(),
                    parent_id=self.parent_box.currentData() or "",
                    sort_order=self.sort.value(),
                )
            else:
                self.service.update_category(
                    self.category.id,
                    code=self.code.text().strip(),
                    name=self.name.text().strip(),
                    parent_id=self.parent_box.currentData() or "",
                    sort_order=self.sort.value(),
                )
            self.accept()
        except Exception as exc:  # noqa: BLE001
            _error(self, "保存分类失败", exc)


class TagDialog(QDialog):
    def __init__(self, service: LibraryService, parent: QWidget | None = None):
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("新建标签")
        form = QFormLayout(self)
        self.name = QLineEdit()
        self.color = QLineEdit("#C4553B")
        form.addRow("标签名称", self.name)
        form.addRow("颜色", self.color)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _save(self) -> None:
        try:
            self.service.create_tag(self.name.text().strip(), self.color.text().strip())
            self.accept()
        except Exception as exc:  # noqa: BLE001
            _error(self, "保存标签失败", exc)


class VersionDialog(QDialog):
    def __init__(self, service: LibraryService, book_id: str, version=None, parent: QWidget | None = None):
        super().__init__(parent)
        self.service = service
        self.book_id = book_id
        self.version = version
        self.setWindowTitle("编辑版本" if version else "新建版本")
        form = QFormLayout(self)
        self.label = QLineEdit(version.version_label if version else "")
        self.notes = QPlainTextEdit(version.notes if version else "")
        self.notes.setMaximumHeight(100)
        self.status = QComboBox()
        for value, text in {
            BookVersionStatus.DRAFT: "草稿",
            BookVersionStatus.ACTIVE: "当前",
            BookVersionStatus.ARCHIVED: "归档",
            BookVersionStatus.FAILED: "失败",
        }.items():
            self.status.addItem(text, value.value)
        if version:
            index = self.status.findData(version.status.value)
            if index >= 0:
                self.status.setCurrentIndex(index)
        self.activate = QCheckBox("保存后设为当前版本")
        form.addRow("版本号", self.label)
        form.addRow("说明", self.notes)
        form.addRow("状态", self.status)
        form.addRow("", self.activate)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _save(self) -> None:
        try:
            if self.version is None:
                version = self.service.create_version(
                    book_id=self.book_id,
                    version_label=self.label.text().strip(),
                    notes=self.notes.toPlainText().strip(),
                    status=BookVersionStatus(self.status.currentData()),
                )
            else:
                version = self.service.update_version(
                    self.version.id,
                    version_label=self.label.text().strip(),
                    notes=self.notes.toPlainText().strip(),
                    status=BookVersionStatus(self.status.currentData()),
                )
            if self.activate.isChecked():
                self.service.activate_version(version.id)
            self.accept()
        except Exception as exc:  # noqa: BLE001
            _error(self, "保存版本失败", exc)


# --------------------------------------------------------------------------- page


class ProductCenterPage(QWidget):
    def __init__(self, context):
        super().__init__()
        self.context = context
        self.service = context.library()
        self.batch = context.batch()
        self.backup = context.backup()
        self.book_page = 1
        self.sku_page = 1
        self.asset_page = 1
        self.audit_page = 1
        self.page_size = 50
        self.current_batch_job = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 22, 28, 18)
        layout.setSpacing(12)
        layout.addWidget(
            PageHeader(
                "商品中心",
                "闲鱼教辅默认模板 · 教材母商品 / 分册 / SKU · 资产、版本、批量与备份",
            )
        )
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_overview(), "总览")
        self.tabs.addTab(self._build_catalog(), "商品目录")
        self.tabs.addTab(self._build_books(), "教材与子商品")
        self.tabs.addTab(self._build_skus(), "SKU 与价格")
        self.tabs.addTab(self._build_families(), "系列模板")
        self.tabs.addTab(self._build_taxonomy(), "分类与标签")
        self.tabs.addTab(self._build_assets(), "资产与文件")
        self.tabs.addTab(self._build_versions(), "版本管理")
        self.tabs.addTab(self._build_delivery(), "网盘交付")
        self.tabs.addTab(self._build_batch(), "批量操作")
        self.tabs.addTab(self._build_stats(), "统计与审计")
        self.tabs.currentChanged.connect(lambda _index: self.refresh())
        layout.addWidget(self.tabs, 1)
        self.refresh()

    # ------------------------------------------------------------------ builders
    def _build_overview(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 14, 12, 12)
        cards = QHBoxLayout()
        self.stat_books = StatCard("教材母商品", "0")
        self.stat_skus = StatCard("销售 SKU", "0")
        self.stat_assets = StatCard("资产文件", "0")
        self.stat_families = StatCard("商品系列", "0")
        for card in (self.stat_books, self.stat_skus, self.stat_assets, self.stat_families):
            cards.addWidget(card)
        layout.addLayout(cards)
        self.overview_hint = label("", "Hint", wrap=True)
        layout.addWidget(self.overview_hint)
        row = QHBoxLayout()
        left = Card("生命周期分布")
        self.lifecycle_table = QTableWidget()
        prepare_table(self.lifecycle_table, ["状态", "数量"])
        left.body.addWidget(self.lifecycle_table)
        right = Card("交付状态")
        self.delivery_table = QTableWidget()
        prepare_table(self.delivery_table, ["状态", "数量"])
        right.body.addWidget(self.delivery_table)
        row.addWidget(left)
        row.addWidget(right)
        layout.addLayout(row)
        buttons = QHBoxLayout()
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        backup = QPushButton("立即备份数据库")
        backup.clicked.connect(self._create_backup)
        seed = QPushButton("生成 100 教材 / 300 SKU 演示数据")
        set_variant(seed, "secondary")
        seed.clicked.connect(self._seed_demo)
        buttons.addWidget(refresh)
        buttons.addWidget(backup)
        buttons.addWidget(seed)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addStretch(1)
        return page

    def _build_catalog(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 14, 12, 12)
        filters = QHBoxLayout()
        self.catalog_search = QLineEdit()
        self.catalog_search.setPlaceholderText("书名 / 作者 / ISBN / 编号 / 出版社")
        self.catalog_search.returnPressed.connect(self._catalog_search)
        self.catalog_subject = QComboBox()
        self.catalog_subject.addItem("全部学科", "")
        for value, text in SUBJECT_LABELS.items():
            self.catalog_subject.addItem(text, value)
        search = QPushButton("检索")
        search.clicked.connect(self._catalog_search)
        filters.addWidget(self.catalog_search, 1)
        filters.addWidget(self.catalog_subject)
        filters.addWidget(search)
        layout.addLayout(filters)
        self.catalog_table = QTableWidget()
        prepare_table(
            self.catalog_table,
            ["编号", "书名", "作者", "出版社", "学科", "分类", "版本", "SKU", "状态"],
        )
        self.catalog_table.doubleClicked.connect(lambda _index: self._edit_selected_book())
        layout.addWidget(self.catalog_table, 1)
        pager = QHBoxLayout()
        prev = QPushButton("上一页")
        prev.clicked.connect(lambda: self._change_book_page(-1))
        next_ = QPushButton("下一页")
        next_.clicked.connect(lambda: self._change_book_page(1))
        self.catalog_page_label = label("", "Hint")
        pager.addWidget(prev)
        pager.addWidget(next_)
        pager.addWidget(self.catalog_page_label)
        pager.addStretch(1)
        layout.addLayout(pager)
        row = ButtonRow(
            [
                _button("编辑教材", self._edit_selected_book),
                _button("查看详情", self._show_book_detail),
                _button("打开书目目录", self._open_book_dir),
            ]
        )
        layout.addWidget(row)
        return page

    def _build_books(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 14, 12, 12)
        self.books_table = QTableWidget()
        prepare_table(self.books_table, ["编号", "书名", "册数", "学科", "系列", "分类", "状态"])
        self.books_table.itemSelectionChanged.connect(self._refresh_products)
        layout.addWidget(self.books_table, 2)
        layout.addWidget(label("选中教材的分册 / 生产记录", "CardTitle"))
        self.products_table = QTableWidget()
        prepare_table(self.products_table, ["分册", "编号", "状态", "页数", "习题", "已核验", "生产 ID"])
        layout.addWidget(self.products_table, 1)
        layout.addWidget(
            ButtonRow(
                [
                    _button("新建教材", self._new_book),
                    _button("编辑教材", self._edit_selected_book),
                    _button("删除教材", self._delete_selected_book),
                    _button("刷新", self.refresh),
                ]
            )
        )
        return page

    def _build_skus(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 14, 12, 12)
        filters = QHBoxLayout()
        self.sku_search = QLineEdit()
        self.sku_search.setPlaceholderText("SKU 编号 / 名称")
        self.sku_search.returnPressed.connect(self._sku_search)
        self.sku_lifecycle = QComboBox()
        self.sku_lifecycle.addItem("全部生命周期", "")
        for value, text in LIFECYCLE_LABELS.items():
            self.sku_lifecycle.addItem(text, value)
        self.sku_delivery = QComboBox()
        self.sku_delivery.addItem("全部交付状态", "")
        for value, text in DELIVERY_LABELS.items():
            self.sku_delivery.addItem(text, value.value)
        search = QPushButton("筛选")
        search.clicked.connect(self._sku_search)
        filters.addWidget(self.sku_search, 1)
        filters.addWidget(self.sku_lifecycle)
        filters.addWidget(self.sku_delivery)
        filters.addWidget(search)
        layout.addLayout(filters)
        self.skus_table = QTableWidget()
        prepare_table(
            self.skus_table,
            ["SKU 编号", "名称", "类型", "售价", "原价", "生命周期", "交付", "版本", "启用"],
        )
        self.skus_table.doubleClicked.connect(lambda _index: self._edit_selected_sku())
        layout.addWidget(self.skus_table, 1)
        pager = QHBoxLayout()
        prev = QPushButton("上一页")
        prev.clicked.connect(lambda: self._change_sku_page(-1))
        next_ = QPushButton("下一页")
        next_.clicked.connect(lambda: self._change_sku_page(1))
        self.sku_page_label = label("", "Hint")
        pager.addWidget(prev)
        pager.addWidget(next_)
        pager.addWidget(self.sku_page_label)
        pager.addStretch(1)
        layout.addLayout(pager)
        layout.addWidget(
            ButtonRow(
                [
                    _button("新建单册 SKU", lambda: self._new_sku("single")),
                    _button("新建套装 SKU", lambda: self._new_sku("bundle")),
                    _button("编辑 SKU", self._edit_selected_sku),
                    _button("推进生命周期", self._advance_lifecycle),
                    _button("改价", self._change_price),
                    _button("删除 SKU", self._delete_selected_sku),
                ]
            )
        )
        return page

    def _build_families(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 14, 12, 12)
        self.family_table = QTableWidget()
        prepare_table(self.family_table, ["系列", "编号", "品牌", "默认", "水印", "版本", "教材数"])
        layout.addWidget(self.family_table, 1)
        layout.addWidget(
            ButtonRow(
                [
                    _button("新建系列", self._new_family),
                    _button("编辑系列", self._edit_selected_family),
                    _button("设为默认", self._set_default_family),
                    _button("应用到现有教材", self._apply_family),
                    _button("刷新", self.refresh),
                ]
            )
        )
        layout.addWidget(
            label(
                "模板更新只影响未来新建商品；对现有教材批量应用必须由这里主动触发。",
                "Hint",
                wrap=True,
            )
        )
        return page

    def _build_taxonomy(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(12, 14, 12, 12)
        categories = Card("分类")
        self.category_table = QTableWidget()
        prepare_table(self.category_table, ["编号", "名称", "上级", "排序"])
        categories.body.addWidget(self.category_table, 1)
        categories.body.addWidget(
            ButtonRow(
                [
                    _button("新建分类", self._new_category),
                    _button("编辑分类", self._edit_selected_category),
                    _button("删除分类", self._delete_selected_category),
                ]
            )
        )
        tags = Card("标签")
        self.tag_table = QTableWidget()
        prepare_table(self.tag_table, ["名称", "颜色", "引用数"])
        tags.body.addWidget(self.tag_table, 1)
        tags.body.addWidget(
            ButtonRow(
                [
                    _button("新建标签", self._new_tag),
                    _button("删除标签", self._delete_selected_tag),
                ]
            )
        )
        layout.addWidget(categories, 1)
        layout.addWidget(tags, 1)
        return page

    def _build_assets(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 14, 12, 12)
        filters = QHBoxLayout()
        self.asset_search = QLineEdit()
        self.asset_search.setPlaceholderText("文件名 / SHA-256")
        self.asset_search.returnPressed.connect(self._asset_search)
        self.asset_orphans = QCheckBox("只看未引用")
        self.asset_orphans.stateChanged.connect(lambda _state: self._asset_search())
        search = QPushButton("检索")
        search.clicked.connect(self._asset_search)
        filters.addWidget(self.asset_search, 1)
        filters.addWidget(self.asset_orphans)
        filters.addWidget(search)
        layout.addLayout(filters)
        self.asset_summary = label("", "Hint")
        layout.addWidget(self.asset_summary)
        self.asset_table = QTableWidget()
        prepare_table(self.asset_table, ["文件名", "类型", "大小", "SHA-256", "引用", "本地路径"])
        layout.addWidget(self.asset_table, 1)
        layout.addWidget(
            ButtonRow(
                [
                    _button("添加文件", self._add_asset),
                    _button("完整性核验", self._verify_assets),
                    _button("删除资产", self._delete_selected_asset),
                    _button("打开资产目录", self._open_asset_dir),
                ]
            )
        )
        return page

    def _build_versions(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 14, 12, 12)
        row = QHBoxLayout()
        self.version_book = QComboBox()
        for book in self.service.list_books(page_size=500).items:
            self.version_book.addItem(f"{book.code} · {book.title}", book.id)
        self.version_book.currentIndexChanged.connect(lambda _index: self._refresh_versions())
        row.addWidget(label("教材", "Hint"))
        row.addWidget(self.version_book, 1)
        layout.addLayout(row)
        self.version_table = QTableWidget()
        prepare_table(self.version_table, ["版本", "状态", "说明", "生产 ID", "创建时间"])
        layout.addWidget(self.version_table, 1)
        layout.addWidget(
            ButtonRow(
                [
                    _button("新建版本", self._new_version),
                    _button("编辑版本", self._edit_selected_version),
                    _button("设为当前", self._activate_selected_version),
                    _button("归档", self._archive_selected_version),
                    _button("删除版本", self._delete_selected_version),
                ]
            )
        )
        return page

    def _build_delivery(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 14, 12, 12)
        self.delivery_sku_table = QTableWidget()
        prepare_table(self.delivery_sku_table, ["SKU 编号", "名称", "交付状态", "生命周期", "图片目录"])
        layout.addWidget(self.delivery_sku_table, 1)
        layout.addWidget(
            ButtonRow(
                [
                    _button("构建交付文件", self._deliver_sku),
                    _button("上传网盘", self._upload_sku),
                    _button("检查分享", self._verify_share),
                    _button("创建分享", self._create_share),
                    _button("刷新", self.refresh),
                ]
            )
        )
        layout.addWidget(
            label(
                "真实上传、分享与核验需要已配置并授权的百度网盘；未配置时只显示错误，不会伪造云端结果。",
                "Hint",
                wrap=True,
            )
        )
        return page

    def _build_batch(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 14, 12, 12)
        row = QHBoxLayout()
        self.batch_kind = QComboBox()
        for value in IMPORT_KINDS:
            self.batch_kind.addItem(BATCH_LABELS.get(value, value), value)
        self.batch_file = QLineEdit()
        self.batch_file.setPlaceholderText("选择 CSV / XLSX 文件")
        browse = QPushButton("浏览")
        browse.clicked.connect(self._browse_batch_file)
        row.addWidget(self.batch_kind)
        row.addWidget(self.batch_file, 1)
        row.addWidget(browse)
        layout.addLayout(row)
        self.batch_hint = label("先预览，再执行；失败行可单独重试。", "Hint")
        layout.addWidget(self.batch_hint)
        self.batch_table = QTableWidget()
        prepare_table(self.batch_table, ["行", "状态", "说明", "数据"])
        layout.addWidget(self.batch_table, 1)
        layout.addWidget(
            ButtonRow(
                [
                    _button("预览", self._preview_batch),
                    _button("执行", self._run_batch),
                    _button("重试失败行", self._retry_batch),
                    _button("导出教材", lambda: self._export_batch("books")),
                    _button("导出 SKU", lambda: self._export_batch("skus")),
                    _button("导出任务报告", self._export_batch_report),
                ]
            )
        )
        return page

    def _build_stats(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 14, 12, 12)
        self.stats_label = label("", "Hint", wrap=True)
        layout.addWidget(self.stats_label)
        self.category_table_stats = QTableWidget()
        prepare_table(self.category_table_stats, ["分类", "教材数", "SKU 数"])
        layout.addWidget(self.category_table_stats, 1)
        layout.addWidget(label("操作审计", "CardTitle"))
        self.audit_table = QTableWidget()
        prepare_table(self.audit_table, ["时间", "操作", "对象", "对象 ID", "说明"])
        layout.addWidget(self.audit_table, 1)
        pager = QHBoxLayout()
        prev = QPushButton("上一页")
        prev.clicked.connect(lambda: self._change_audit_page(-1))
        next_ = QPushButton("下一页")
        next_.clicked.connect(lambda: self._change_audit_page(1))
        self.audit_page_label = label("", "Hint")
        pager.addWidget(prev)
        pager.addWidget(next_)
        pager.addWidget(self.audit_page_label)
        pager.addStretch(1)
        layout.addLayout(pager)
        return page

    # ------------------------------------------------------------------ refresh
    def refresh(self) -> None:
        try:
            overview = self.service.overview()
            self.stat_books.set_value(str(overview.get("books", 0)))
            self.stat_skus.set_value(str(overview.get("skus", 0)))
            assets = overview.get("assets", {})
            self.stat_assets.set_value(str(assets.get("assets", 0)))
            self.stat_families.set_value(str(overview.get("families", 0)))
            self.overview_hint.setText(
                f"教材无 SKU：{overview.get('books_without_sku', 0)} · "
                f"资产引用：{assets.get('linked', 0)} · 未引用：{assets.get('orphans', 0)} · "
                f"SKU 价格合计：{overview.get('price_total_yuan', '0.00')} 元"
            )
            lifecycle = overview.get("lifecycle", {})
            set_rows(self.lifecycle_table, [(LIFECYCLE_LABELS.get(SkuLifecycle(key), key), value) for key, value in lifecycle.items()])
            delivery = overview.get("delivery", {})
            set_rows(
                self.delivery_table,
                [(DELIVERY_LABELS.get(DeliveryStatus(key), key), value) for key, value in delivery.items()],
            )
            self._refresh_books()
            self._refresh_skus()
            self._refresh_families()
            self._refresh_taxonomy()
            self._refresh_assets()
            self._refresh_versions()
            self._refresh_delivery()
            self._refresh_stats()
        except Exception as exc:  # noqa: BLE001
            self.overview_hint.setText(f"刷新失败：{exc}")

    def _refresh_books(self) -> None:
        page = self.service.list_books(
            q=self.catalog_search.text().strip(),
            subject=self.catalog_subject.currentData() or "",
            page=self.book_page,
            page_size=self.page_size,
        )
        rows = []
        for book in page.items:
            skus = self.service.list_skus(book_id=book.id, page_size=1).total
            versions = len(self.service.list_versions(book.id))
            category = next((c.name for c in self.service.list_categories() if c.id == book.category_id), "")
            rows.append(
                (
                    book.code,
                    book.title,
                    "；".join(book.authors),
                    book.publisher,
                    SUBJECT_LABELS.get(book.subject.value if hasattr(book.subject, "value") else book.subject, ""),
                    category,
                    versions,
                    skus,
                    BOOK_STATUS_LABELS.get(book.status, book.status),
                )
            )
        set_rows(self.catalog_table, rows)
        set_rows(
            self.books_table,
            [
                (
                    book.code,
                    book.title,
                    book.volumes,
                    SUBJECT_LABELS.get(book.subject.value if hasattr(book.subject, "value") else book.subject, ""),
                    next((f.name for f in self.service.list_families() if f.id == book.family_id), ""),
                    next((c.name for c in self.service.list_categories() if c.id == book.category_id), ""),
                    BOOK_STATUS_LABELS.get(book.status, book.status),
                )
                for book in page.items
            ],
        )
        self.catalog_page_label.setText(f"第 {page.page} / {max(page.pages, 1)} 页 · 共 {page.total} 条")
        if self.books_table.currentRow() < 0 and self.books_table.rowCount():
            self.books_table.selectRow(0)
        self._refresh_products()

    def _refresh_products(self) -> None:
        book = self._selected_book()
        if book is None:
            set_rows(self.products_table, [])
            return
        rows = []
        for product in self.service.db.list_productions(book.id):
            rows.append(
                (
                    product.volume_label,
                    product.volume_code,
                    product.status,
                    product.total_pages,
                    product.exercise_count,
                    product.verified_count,
                    product.id,
                )
            )
        set_rows(self.products_table, rows)

    def _refresh_skus(self) -> None:
        page = self.service.list_skus(
            q=self.sku_search.text().strip(),
            lifecycle=self.sku_lifecycle.currentData() or "",
            delivery_status=self.sku_delivery.currentData() or "",
            page=self.sku_page,
            page_size=self.page_size,
        )
        set_rows(
            self.skus_table,
            [
                (
                    sku.sku_code,
                    sku.name,
                    "套装" if sku.kind == "bundle" else "单册",
                    _money(sku.price_cents),
                    _money(sku.original_price_cents),
                    LIFECYCLE_LABELS.get(sku.lifecycle, sku.lifecycle.value),
                    DELIVERY_LABELS.get(sku.delivery_status, sku.delivery_status.value),
                    sku.version_id,
                    "是" if sku.enabled else "否",
                )
                for sku in page.items
            ],
        )
        self.sku_page_label.setText(f"第 {page.page} / {max(page.pages, 1)} 页 · 共 {page.total} 条")

    def _refresh_families(self) -> None:
        rows = []
        for family in self.service.list_families():
            books = self.service.list_books(family_id=family.id, page_size=1).total
            rows.append(
                (
                    family.name,
                    family.code,
                    family.brand_name,
                    "是" if family.is_default else "否",
                    family.watermark.get("opacity", ""),
                    family.version,
                    books,
                )
            )
        set_rows(self.family_table, rows)

    def _refresh_taxonomy(self) -> None:
        categories = self.service.list_categories()
        by_id = {c.id: c.name for c in categories}
        set_rows(
            self.category_table,
            [
                (c.code, c.name, by_id.get(c.parent_id, "—"), c.sort_order)
                for c in categories
            ],
        )
        tags = self.service.list_tags()
        set_rows(
            self.tag_table,
            [(tag.name, tag.color, "") for tag in tags],
        )
        self._refresh_stats()

    def _refresh_assets(self) -> None:
        page = self.service.assets.list_assets(
            q=self.asset_search.text().strip(),
            page=self.asset_page,
            page_size=self.page_size,
            only_orphans=self.asset_orphans.isChecked(),
        )
        set_rows(
            self.asset_table,
            [
                (
                    item.get("file_name", ""),
                    item.get("file_type", ""),
                    item.get("file_size", 0),
                    item.get("sha256", "")[:16],
                    item.get("refs", 0),
                    item.get("local_path", ""),
                )
                for item in page.items
            ],
        )
        summary = self.service.assets.usage_summary()
        self.asset_summary.setText(
            f"资产 {summary.get('assets', 0)} 个 · 共 {summary.get('bytes', 0)} 字节 · "
            f"已引用 {summary.get('linked', 0)} · 未引用 {summary.get('orphans', 0)} · "
            f"第 {page.page} / {max(page.pages, 1)} 页"
        )

    def _refresh_versions(self) -> None:
        book_id = self.version_book.currentData()
        if not book_id:
            set_rows(self.version_table, [])
            return
        set_rows(
            self.version_table,
            [
                (
                    version.version_label,
                    version.status.value,
                    version.notes,
                    version.product_id,
                    version.created_at,
                )
                for version in self.service.list_versions(book_id)
            ],
        )

    def _refresh_delivery(self) -> None:
        page = self.service.list_skus(page=1, page_size=500)
        set_rows(
            self.delivery_sku_table,
            [
                (
                    sku.sku_code,
                    sku.name,
                    DELIVERY_LABELS.get(sku.delivery_status, sku.delivery_status.value),
                    LIFECYCLE_LABELS.get(sku.lifecycle, sku.lifecycle.value),
                    sku.image_dir,
                )
                for sku in page.items
            ],
        )

    def _refresh_stats(self) -> None:
        try:
            overview = self.service.overview()
            self.stats_label.setText(
                f"教材 {overview.get('books', 0)} · SKU {overview.get('skus', 0)} · "
                f"系列 {overview.get('families', 0)} · 分类 {overview.get('categories', 0)} · "
                f"标签 {overview.get('tags', 0)} · 资产 {overview.get('assets', {}).get('assets', 0)}"
            )
            set_rows(
                self.category_table_stats,
                [
                    (item.get("name", ""), item.get("books", 0), item.get("skus", 0))
                    for item in self.service.category_stats()
                ],
            )
            audit = self.service.list_audit(page=self.audit_page, page_size=self.page_size)
            set_rows(
                self.audit_table,
                [
                    (entry.created_at, entry.action, entry.entity_type, entry.entity_id, entry.detail)
                    for entry in audit.items
                ],
            )
            self.audit_page_label.setText(
                f"第 {audit.page} / {max(audit.pages, 1)} 页 · 共 {audit.total} 条"
            )
        except Exception as exc:  # noqa: BLE001
            self.stats_label.setText(f"统计刷新失败：{exc}")

    # ------------------------------------------------------------------ selection
    def _selected_book(self) -> Book | None:
        key = selected_key(self.catalog_table) or selected_key(self.books_table)
        if not key:
            return None
        return self.service.db.get_book_by_code(str(key))

    def _selected_sku(self):
        key = selected_key(self.skus_table) or selected_key(
            getattr(self, "delivery_sku_table", self.skus_table)
        )
        if not key:
            return None
        return self.service.db.get_sku_by_code(str(key))

    def _selected_category(self):
        code = selected_key(self.category_table)
        return next((c for c in self.service.list_categories() if c.code == code), None)

    def _selected_tag(self):
        name = selected_key(self.tag_table)
        return next((t for t in self.service.list_tags() if t.name == name), None)

    def _selected_family(self):
        code = selected_key(self.family_table)
        return next((f for f in self.service.list_families() if f.code == code), None)

    def _selected_version(self):
        book_id = self.version_book.currentData()
        label_text = selected_key(self.version_table)
        if not book_id or not label_text:
            return None
        return next(
            (v for v in self.service.list_versions(book_id) if v.version_label == label_text),
            None,
        )

    # ------------------------------------------------------------------ actions
    def _catalog_search(self) -> None:
        self.book_page = 1
        self._refresh_books()

    def _sku_search(self) -> None:
        self.sku_page = 1
        self._refresh_skus()

    def _asset_search(self) -> None:
        self.asset_page = 1
        self._refresh_assets()

    def _change_book_page(self, delta: int) -> None:
        self.book_page = max(1, self.book_page + delta)
        self._refresh_books()

    def _change_sku_page(self, delta: int) -> None:
        self.sku_page = max(1, self.sku_page + delta)
        self._refresh_skus()

    def _change_audit_page(self, delta: int) -> None:
        self.audit_page = max(1, self.audit_page + delta)
        self._refresh_stats()

    def _new_book(self) -> None:
        if BookDialog(self.service, parent=self).exec():
            self.refresh()

    def _edit_selected_book(self) -> None:
        book = self._selected_book()
        if book is None:
            QMessageBox.information(self, "未选择", "请先在表格中选择一本教材。")
            return
        if BookDialog(self.service, book, parent=self).exec():
            self.refresh()

    def _show_book_detail(self) -> None:
        book = self._selected_book()
        if book is None:
            return
        detail = self.service.book_detail(book.id)
        QMessageBox.information(
            self,
            f"{book.code} · {book.title}",
            json.dumps(detail, ensure_ascii=False, indent=2, default=str)[:6000],
        )

    def _open_book_dir(self) -> None:
        book = self._selected_book()
        if book is None:
            return
        path = self.context.paths.book_dir(book.code)
        path.mkdir(parents=True, exist_ok=True)
        self._open_path(path)

    def _delete_selected_book(self) -> None:
        book = self._selected_book()
        if book is None or not _confirm(self, "删除教材", f"确定删除 {book.code}？有版本或 SKU 时会被阻止。"):
            return
        try:
            self.service.delete_book(book.id, confirm=True)
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            _error(self, "删除教材失败", exc)

    def _new_sku(self, kind: str) -> None:
        dialog = SkuDialog(self.service, parent=self)
        index = dialog.kind.findData(kind)
        if index >= 0:
            dialog.kind.setCurrentIndex(index)
        if dialog.exec():
            self.refresh()

    def _edit_selected_sku(self) -> None:
        sku = self._selected_sku()
        if sku is None:
            QMessageBox.information(self, "未选择", "请先在表格中选择一个 SKU。")
            return
        if SkuDialog(self.service, sku, parent=self).exec():
            self.refresh()

    def _advance_lifecycle(self) -> None:
        sku = self._selected_sku()
        if sku is None:
            return
        targets = {
            SkuLifecycle.DRAFT: SkuLifecycle.PENDING_PRODUCTION,
            SkuLifecycle.PENDING_PRODUCTION: SkuLifecycle.PRODUCING,
            SkuLifecycle.PRODUCING: SkuLifecycle.REVIEW_REQUIRED,
            SkuLifecycle.REVIEW_REQUIRED: SkuLifecycle.READY,
            SkuLifecycle.READY: SkuLifecycle.PUBLISHED,
            SkuLifecycle.PUBLISHED: SkuLifecycle.PAUSED,
            SkuLifecycle.PAUSED: SkuLifecycle.PUBLISHED,
        }
        target = targets.get(sku.lifecycle)
        if target is None:
            QMessageBox.information(self, "无法推进", f"{sku.sku_code} 当前状态为 {sku.lifecycle.value}。")
            return
        if not _confirm(self, "推进生命周期", f"{sku.sku_code}：{sku.lifecycle.value} → {target.value}？"):
            return
        try:
            self.service.transition_lifecycle(sku.id, target)
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            _error(self, "推进失败", exc)

    def _change_price(self) -> None:
        sku = self._selected_sku()
        if sku is None:
            return
        from PySide6.QtWidgets import QInputDialog

        value, ok = QInputDialog.getDouble(
            self,
            "修改售价",
            f"{sku.sku_code} 新售价（元）",
            sku.price_cents / 100,
            0.01,
            999999,
            2,
        )
        if not ok:
            return
        try:
            self.service.set_price(sku.id, value)
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            _error(self, "改价失败", exc)

    def _delete_selected_sku(self) -> None:
        sku = self._selected_sku()
        if sku is None or not _confirm(self, "删除 SKU", f"确定删除 {sku.sku_code}？共享资产不会被删除。"):
            return
        try:
            self.service.delete_sku(sku.id, confirm=True)
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            _error(self, "删除 SKU 失败", exc)

    def _new_family(self) -> None:
        if FamilyDialog(self.service, parent=self).exec():
            self.refresh()

    def _edit_selected_family(self) -> None:
        family = self._selected_family()
        if family is None:
            return
        if FamilyDialog(self.service, family, parent=self).exec():
            self.refresh()

    def _set_default_family(self) -> None:
        family = self._selected_family()
        if family is None:
            return
        try:
            self.service.set_default_family(family.id)
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            _error(self, "设置默认系列失败", exc)

    def _apply_family(self) -> None:
        family = self._selected_family()
        if family is None:
            return
        if not _confirm(
            self,
            "应用模板",
            f"将 {family.name} 应用到该系列现有教材？此操作只改变教材的系列归属，不覆盖单个教材的覆盖项。",
        ):
            return
        try:
            result = self.service.apply_family_template(family.id)
            QMessageBox.information(self, "完成", f"已更新 {result.get('updated', 0)} 本教材。")
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            _error(self, "应用模板失败", exc)

    def _new_category(self) -> None:
        if CategoryDialog(self.service, parent=self).exec():
            self.refresh()

    def _edit_selected_category(self) -> None:
        category = self._selected_category()
        if category is None:
            return
        if CategoryDialog(self.service, category, parent=self).exec():
            self.refresh()

    def _delete_selected_category(self) -> None:
        category = self._selected_category()
        if category is None or not _confirm(self, "删除分类", f"确定删除分类 {category.name}？"):
            return
        try:
            self.service.delete_category(category.id)
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            _error(self, "删除分类失败", exc)

    def _new_tag(self) -> None:
        if TagDialog(self.service, parent=self).exec():
            self.refresh()

    def _delete_selected_tag(self) -> None:
        tag = self._selected_tag()
        if tag is None or not _confirm(self, "删除标签", f"确定删除标签 {tag.name}？"):
            return
        try:
            self.service.delete_tag(tag.id)
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            _error(self, "删除标签失败", exc)

    def _add_asset(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择要入库的文件")
        if not path:
            return
        try:
            asset, created = self.service.assets.add_file(path)
            QMessageBox.information(
                self,
                "资产入库",
                f"{'新资产' if created else '已存在相同文件'}：{asset.file_name}\nSHA-256：{asset.sha256[:16]}…",
            )
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            _error(self, "资产入库失败", exc)

    def _verify_assets(self) -> None:
        try:
            report = self.service.assets.verify()
            bad = [item for item in report if item.get("status") != "ok"]
            QMessageBox.information(
                self,
                "资产核验",
                f"已检查 {len(report)} 个资产，异常 {len(bad)} 个。" + (f"\n{bad[:5]}" if bad else ""),
            )
        except Exception as exc:  # noqa: BLE001
            _error(self, "资产核验失败", exc)

    def _delete_selected_asset(self) -> None:
        name = selected_key(self.asset_table)
        if not name:
            return
        page = self.service.assets.list_assets(q=name, page_size=10)
        asset = next((item for item in page.items if item.get("file_name") == name), None)
        if asset is None:
            return
        if not _confirm(self, "删除资产", "只删除未被引用的资产记录；共享文件不会被自动删除。"):
            return
        try:
            self.service.assets.delete_asset(asset["id"], confirm=True, delete_file=False)
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            _error(self, "删除资产失败", exc)

    def _open_asset_dir(self) -> None:
        self.context.paths.assets_root.mkdir(parents=True, exist_ok=True)
        self._open_path(self.context.paths.assets_root)

    def _new_version(self) -> None:
        book_id = self.version_book.currentData()
        if not book_id:
            return
        if VersionDialog(self.service, book_id, parent=self).exec():
            self.refresh()

    def _edit_selected_version(self) -> None:
        version = self._selected_version()
        if version is None:
            return
        if VersionDialog(self.service, version.book_id, version, parent=self).exec():
            self.refresh()

    def _activate_selected_version(self) -> None:
        version = self._selected_version()
        if version is None:
            return
        try:
            self.service.activate_version(version.id)
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            _error(self, "设置当前版本失败", exc)

    def _archive_selected_version(self) -> None:
        version = self._selected_version()
        if version is None:
            return
        try:
            self.service.archive_version(version.id)
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            _error(self, "归档版本失败", exc)

    def _delete_selected_version(self) -> None:
        version = self._selected_version()
        if version is None or not _confirm(self, "删除版本", "有 SKU 使用该版本时会被阻止。"):
            return
        try:
            self.service.delete_version(version.id)
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            _error(self, "删除版本失败", exc)

    def _catalog(self) -> CatalogManager:
        return CatalogManager(self.context.settings, self.context.db, self.context.paths)

    def _deliver_sku(self) -> None:
        sku = self._selected_sku()
        if sku is None:
            return
        try:
            result = self._catalog().build_delivery(sku)
            QMessageBox.information(self, "交付构建", json.dumps(result, ensure_ascii=False, default=str)[:4000])
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            _error(self, "构建交付失败", exc)

    def _upload_sku(self) -> None:
        sku = self._selected_sku()
        if sku is None:
            return
        try:
            self._catalog().upload_sku(sku)
            self.service.set_delivery_status(sku.id, DeliveryStatus.UPLOADED)
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            _error(self, "上传网盘失败", exc)

    def _verify_share(self) -> None:
        sku = self._selected_sku()
        if sku is None:
            return
        try:
            records = self._catalog().verify_sku_uploads(sku)
            self.service.set_delivery_status(
                sku.id, DeliveryStatus.VERIFIED if records else DeliveryStatus.NEEDS_MANUAL
            )
            self.refresh()
            QMessageBox.information(self, "检查分享", f"找到 {len(records)} 条上传记录。")
        except Exception as exc:  # noqa: BLE001
            _error(self, "检查分享失败", exc)

    def _create_share(self) -> None:
        sku = self._selected_sku()
        if sku is None:
            return
        try:
            self._catalog().create_share(sku)
            self.service.set_delivery_status(sku.id, DeliveryStatus.SHARE_CREATED)
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            _error(self, "创建分享失败", exc)

    def _browse_batch_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择批量文件", "", "表格文件 (*.csv *.xlsx *.xlsm);;所有文件 (*)"
        )
        if path:
            self.batch_file.setText(path)

    def _preview_batch(self) -> None:
        path = self.batch_file.text().strip()
        if not path:
            QMessageBox.information(self, "缺少文件", "请先选择 CSV 或 XLSX 文件。")
            return
        try:
            job = self.batch.preview_file(
                self.batch_kind.currentData(),
                path,
                options={"update_existing": True},
            )
            self.current_batch_job = job.id
            self._show_batch_job(job)
            self.batch_hint.setText(
                f"预览：共 {job.total} 行，可执行 {job.succeeded} 行，问题 {job.failed} 行。"
            )
        except Exception as exc:  # noqa: BLE001
            _error(self, "预览失败", exc)

    def _show_batch_job(self, job) -> None:
        items = self.batch.job_items(job.id)
        set_rows(
            self.batch_table,
            [
                (
                    item.entity_id,
                    item.status,
                    item.message,
                    json.dumps(item.before, ensure_ascii=False)[:160],
                )
                for item in items
            ],
        )

    def _run_batch(self) -> None:
        if not self.current_batch_job:
            QMessageBox.information(self, "没有任务", "请先预览。")
            return
        try:
            job = self.batch.run(self.current_batch_job)
            self._show_batch_job(job)
            self.batch_hint.setText(
                f"执行结果：{job.status.value} · 成功 {job.succeeded} · 失败 {job.failed}"
            )
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            _error(self, "执行失败", exc)

    def _retry_batch(self) -> None:
        if not self.current_batch_job:
            return
        try:
            job = self.batch.retry(self.current_batch_job)
            self._show_batch_job(job)
            self.batch_hint.setText(f"重试结果：{job.status.value} · 成功 {job.succeeded} · 失败 {job.failed}")
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            _error(self, "重试失败", exc)

    def _export_batch(self, kind: str) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出",
            str(self.context.paths.reports / f"{kind}.xlsx"),
            "Excel (*.xlsx);;CSV (*.csv)",
        )
        if not path:
            return
        try:
            if kind == "books":
                job = self.batch.export_books(path)
            else:
                job = self.batch.export_skus(path)
            QMessageBox.information(self, "导出完成", f"{path}\n共 {job.total} 条。")
        except Exception as exc:  # noqa: BLE001
            _error(self, "导出失败", exc)

    def _export_batch_report(self) -> None:
        if not self.current_batch_job:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出任务报告", str(self.context.paths.batch_reports / "batch-report.csv"), "CSV (*.csv)"
        )
        if not path:
            return
        try:
            self.batch.export_job_report(self.current_batch_job, path)
            QMessageBox.information(self, "导出完成", path)
        except Exception as exc:  # noqa: BLE001
            _error(self, "导出报告失败", exc)

    def _create_backup(self) -> None:
        try:
            manifest = self.backup.create_backup()
            QMessageBox.information(
                self,
                "备份完成",
                f"{manifest['database']}\nSHA-256：{manifest['sha256'][:16]}…",
            )
        except Exception as exc:  # noqa: BLE001
            _error(self, "备份失败", exc)

    def _seed_demo(self) -> None:
        if not _confirm(
            self,
            "生成演示数据",
            "将创建 100 本教材、300 个 SKU 及资产引用，用于验收检索、分页和批量操作。继续？",
        ):
            return
        try:
            from core.library.seed import seed_library

            report = seed_library(self.service, books=100, skus=300)
            QMessageBox.information(self, "演示数据", json.dumps(report.as_dict(), ensure_ascii=False))
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            _error(self, "生成演示数据失败", exc)

    def _open_path(self, path: Path) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
