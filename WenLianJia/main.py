"""WenLianJia desktop entry point."""

from __future__ import annotations

import logging
import sys
import traceback

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMessageBox

from core.config import SettingsManager
from core.logging_setup import configure_from_settings, get_logger, install_qt_message_handler
from core.paths import get_paths
from gui.context import AppContext
from gui.main_window import MainWindow
from gui.theme import apply_theme


def _install_exception_hook(logger: logging.Logger) -> None:
    def hook(exc_type, exc_value, exc_tb):  # noqa: ANN001
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        logger.error("unhandled exception\n%s", text)
        try:
            QMessageBox.critical(
                None,
                "程序遇到未处理的错误",
                f"{exc_type.__name__}: {exc_value}\n\n详细信息已写入日志。",
            )
        except Exception:  # pragma: no cover - GUI teardown
            pass

    sys.excepthook = hook


def main() -> int:
    paths = get_paths()
    settings = SettingsManager(paths)
    configure_from_settings(paths, settings, console=False)
    logger = get_logger("main")
    install_qt_message_handler()
    _install_exception_hook(logger)
    logger.info("WenLianJia starting; data root=%s", paths.root)

    app = QApplication(sys.argv)
    app.setApplicationName("WenLianJia")
    app.setApplicationDisplayName("文链家 WenLianJia")
    app.setOrganizationName("爱做题的大肥鱼")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    apply_theme(app)

    context = AppContext.create(paths, settings)
    try:
        manifest = context.backup().create_auto_backup()
        if manifest:
            logger.info("automatic backup created: %s", manifest.get("database"))
    except Exception:  # noqa: BLE001 - backup must not block startup
        logger.warning("automatic backup failed", exc_info=True)
    window = MainWindow(context)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
