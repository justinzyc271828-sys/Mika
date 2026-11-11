"""Small reusable widgets used by every page."""

from __future__ import annotations

from typing import Iterable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


def set_variant(button: QPushButton, variant: str) -> QPushButton:
    button.setProperty("variant", variant)
    button.style().unpolish(button)
    button.style().polish(button)
    return button


class PageHeader(QWidget):
    def __init__(self, title: str, subtitle: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(3)
        title_label = QLabel(title)
        title_label.setObjectName("PageTitle")
        layout.addWidget(title_label)
        if subtitle:
            subtitle_label = QLabel(subtitle)
            subtitle_label.setObjectName("PageSubtitle")
            subtitle_label.setWordWrap(True)
            layout.addWidget(subtitle_label)


class Card(QFrame):
    def __init__(self, title: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(18, 16, 18, 16)
        self.body.setSpacing(10)
        if title:
            heading = QLabel(title)
            heading.setObjectName("CardTitle")
            heading.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            self.body.addWidget(heading)


class StatCard(QFrame):
    def __init__(self, label: str, value: str = "0", hint: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(4)
        self.value_label = QLabel(value)
        self.value_label.setObjectName("StatValue")
        self.label_label = QLabel(label)
        self.label_label.setObjectName("StatLabel")
        layout.addWidget(self.value_label)
        layout.addWidget(self.label_label)
        self.hint_label = QLabel(hint)
        self.hint_label.setObjectName("Hint")
        self.hint_label.setVisible(bool(hint))
        layout.addWidget(self.hint_label)

    def set_value(self, value: str, hint: str = "") -> None:
        self.value_label.setText(str(value))
        if hint:
            self.hint_label.setText(hint)
            self.hint_label.setVisible(True)


class ButtonRow(QWidget):
    def __init__(self, buttons: Iterable[QPushButton], parent: QWidget | None = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        for button in buttons:
            layout.addWidget(button)
        layout.addStretch(1)


def label(text: str, object_name: str = "", wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    if object_name:
        widget.setObjectName(object_name)
    widget.setWordWrap(wrap)
    widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


def divider() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet("color: #E4DDCD; background: #E4DDCD; max-height: 1px;")
    return line
