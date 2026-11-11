"""Offscreen smoke test for the product-center page."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core.prompts import PromptLibrary  # noqa: E402
from gui.context import AppContext  # noqa: E402
from gui.main_window import build_window  # noqa: E402


def test_product_center_builds_and_loads_seed(app):
    from core.library.seed import seed_library

    qt_app = QApplication.instance() or QApplication([])
    context = AppContext(
        paths=app.paths,
        settings=app.settings,
        db=app.db,
        secrets=app.secrets,
        prompts=PromptLibrary(),
    )
    window = build_window(context)
    try:
        page = window.product_center
        assert page.tabs.count() == 11
        report = seed_library(context.library(), books=10, skus=30)
        assert report.books == 10
        page.refresh()
        assert page.catalog_table.rowCount() > 0
        assert page.skus_table.rowCount() > 0
        assert page.delivery_sku_table.rowCount() == 30
        page.catalog_table.selectRow(0)
        assert page._selected_book() is not None
        page.skus_table.selectRow(0)
        assert page._selected_sku() is not None
        window.go(5)
        assert window.stack.currentIndex() == 5
    finally:
        window.close()
        context.library().engine.dispose()
        qt_app.processEvents()
