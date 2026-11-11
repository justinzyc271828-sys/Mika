"""Shared visual language for the desktop application."""

from __future__ import annotations

from PySide6.QtWidgets import QApplication

NAVY = "#102A43"
NAVY_SOFT = "#1E3A5F"
GOLD = "#C9A227"
CREAM = "#F6F2E8"
PAPER = "#FFFDF8"
INK = "#1E2A38"
MUTED = "#667085"
LINE = "#E4DDCD"
RED = "#B42318"
GREEN = "#067647"
AMBER = "#B54708"

STYLESHEET = f"""
QWidget {{
    color: {INK};
    font-family: "Microsoft YaHei UI", "Microsoft YaHei";
    font-size: 13px;
}}
QMainWindow, QDialog {{
    background: {CREAM};
}}
QFrame#Sidebar {{
    background: {NAVY};
}}
QLabel#Brand {{
    color: {PAPER};
    font-size: 25px;
    font-weight: 700;
    letter-spacing: 2px;
}}
QLabel#BrandSub {{
    color: #C9D3DF;
    font-size: 11px;
    letter-spacing: 1px;
}}
QPushButton#NavButton {{
    color: #DCE5EE;
    background: transparent;
    border: none;
    border-radius: 9px;
    text-align: left;
    padding: 12px 16px;
    font-size: 14px;
}}
QPushButton#NavButton:hover {{
    background: {NAVY_SOFT};
}}
QPushButton#NavButton:checked {{
    background: {PAPER};
    color: {NAVY};
    font-weight: 700;
}}
QLabel#PageTitle {{
    font-size: 25px;
    font-weight: 700;
    color: {NAVY};
}}
QLabel#PageSubtitle {{
    color: {MUTED};
    font-size: 12px;
}}
QFrame#Card {{
    background: {PAPER};
    border: 1px solid {LINE};
    border-radius: 12px;
}}
QLabel#CardTitle {{
    color: {NAVY};
    font-size: 15px;
    font-weight: 700;
}}
QLabel#StatValue {{
    color: {NAVY};
    font-size: 29px;
    font-weight: 700;
}}
QLabel#StatLabel {{
    color: {MUTED};
    font-size: 12px;
}}
QLabel#Hint {{
    color: {MUTED};
    font-size: 12px;
}}
QLabel#Danger {{
    color: {RED};
}}
QLabel#Success {{
    color: {GREEN};
}}
QPushButton {{
    background: {PAPER};
    border: 1px solid #CFC6B5;
    border-radius: 8px;
    padding: 8px 16px;
}}
QPushButton:hover {{
    border-color: {GOLD};
}}
QPushButton:disabled {{
    color: #98A2B3;
    background: #F2F0EA;
    border-color: #E3DED2;
}}
QPushButton[variant="primary"] {{
    background: {NAVY};
    color: white;
    border: 1px solid {NAVY};
    font-weight: 700;
}}
QPushButton[variant="primary"]:hover {{
    background: {NAVY_SOFT};
}}
QPushButton[variant="gold"] {{
    background: {GOLD};
    color: #1B1B1B;
    border: 1px solid {GOLD};
    font-weight: 700;
}}
QPushButton[variant="danger"] {{
    color: {RED};
    border-color: #F1B7B2;
}}
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: white;
    border: 1px solid #D8D0C0;
    border-radius: 7px;
    padding: 7px 9px;
    selection-background-color: {NAVY};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus,
QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 1px solid {GOLD};
}}
QTableWidget, QTableView {{
    background: {PAPER};
    alternate-background-color: #FBF8F0;
    border: 1px solid {LINE};
    border-radius: 8px;
    gridline-color: #EFE9DB;
    selection-background-color: #E8EEF5;
    selection-color: {INK};
}}
QHeaderView::section {{
    background: #F0EADB;
    color: {NAVY};
    padding: 8px;
    border: none;
    border-bottom: 1px solid {LINE};
    font-weight: 700;
}}
QTabWidget::pane {{
    border: 1px solid {LINE};
    border-radius: 10px;
    background: {PAPER};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    padding: 9px 18px;
    color: {MUTED};
}}
QTabBar::tab:selected {{
    color: {NAVY};
    font-weight: 700;
    border-bottom: 2px solid {GOLD};
}}
QScrollArea {{
    border: none;
    background: transparent;
}}
QProgressBar {{
    border: none;
    border-radius: 5px;
    background: #E8E2D4;
    height: 10px;
    text-align: center;
}}
QProgressBar::chunk {{
    background: {GOLD};
    border-radius: 5px;
}}
QGroupBox {{
    border: 1px solid {LINE};
    border-radius: 10px;
    margin-top: 12px;
    padding: 16px 12px 12px 12px;
    background: {PAPER};
    font-weight: 700;
    color: {NAVY};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
}}
QCheckBox {{
    spacing: 7px;
}}
QStatusBar {{
    background: #EEE8DA;
    color: {MUTED};
}}
QSplitter::handle {{
    background: transparent;
    width: 8px;
}}
QToolTip {{
    background: {NAVY};
    color: white;
    border: none;
    padding: 6px;
}}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
