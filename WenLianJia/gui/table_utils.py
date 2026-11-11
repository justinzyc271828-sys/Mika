"""Table helpers to keep page code readable."""

from __future__ import annotations

from typing import Iterable, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem


def prepare_table(table: QTableWidget, headers: Sequence[str]) -> None:
    table.setColumnCount(len(headers))
    table.setHorizontalHeaderLabels(list(headers))
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setAlternatingRowColors(True)
    table.verticalHeader().setVisible(False)
    table.setShowGrid(False)
    table.horizontalHeader().setStretchLastSection(True)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setHighlightSections(False)


def set_rows(table: QTableWidget, rows: Iterable[Sequence[object]]) -> None:
    rows = list(rows)
    table.setRowCount(len(rows))
    for row_index, row in enumerate(rows):
        for column, value in enumerate(row):
            item = QTableWidgetItem("" if value is None else str(value))
            if column == 0:
                item.setData(Qt.ItemDataRole.UserRole, value)
            table.setItem(row_index, column, item)


def selected_key(table: QTableWidget, column: int = 0) -> str:
    row = table.currentRow()
    if row < 0:
        return ""
    item = table.item(row, column)
    if item is None:
        return ""
    data = item.data(Qt.ItemDataRole.UserRole)
    return str(data if data is not None else item.text())
