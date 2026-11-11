"""Logging with rotation and secret redaction."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable

from .config import SettingsManager

_PATTERNS = [
    re.compile(r"(sk-[A-Za-z0-9_\-]{8,})"),
    re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]{8,}", re.IGNORECASE),
    re.compile(r"(access_token=)[^&\s]+", re.IGNORECASE),
    re.compile(r"(refresh_token=)[^&\s]+", re.IGNORECASE),
    re.compile(r"(\"api_key\"\s*:\s*\")[^\"]+", re.IGNORECASE),
]


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        for pattern in _PATTERNS:
            message = pattern.sub(lambda m: m.group(1) + "***" + (m.group(2) if m.lastindex and m.lastindex > 1 else ""), message)
        record.msg = message
        record.args = ()
        return True


def setup_logging(
    log_dir: Path,
    *,
    level: str = "INFO",
    max_mb: int = 10,
    backups: int = 10,
    console: bool = True,
) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("wlj")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        log_dir / "wenlianjia.log",
        maxBytes=max(1, max_mb) * 1024 * 1024,
        backupCount=max(1, backups),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(RedactFilter())
    root.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        stream.addFilter(RedactFilter())
        root.addHandler(stream)
    return root


def configure_from_settings(paths, settings: SettingsManager, *, console: bool = True) -> logging.Logger:
    return setup_logging(
        paths.log_dir,
        level=str(settings.get("logging.level", "INFO")),
        max_mb=int(settings.get("logging.max_mb", 10)),
        backups=int(settings.get("logging.backups", 10)),
        console=console,
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"wlj.{name}")


def install_qt_message_handler(records: Iterable[str] | None = None) -> None:
    """Route Qt warnings through the application logger without importing Qt."""

    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception:  # pragma: no cover - GUI not installed in some tools
        return
    logger = get_logger("qt")

    def handler(msg_type, context, message):  # noqa: ANN001
        level = {
            QtMsgType.QtDebugMsg: logging.DEBUG,
            QtMsgType.QtInfoMsg: logging.INFO,
            QtMsgType.QtWarningMsg: logging.WARNING,
            QtMsgType.QtCriticalMsg: logging.ERROR,
            QtMsgType.QtFatalMsg: logging.CRITICAL,
        }.get(msg_type, logging.INFO)
        logger.log(level, "%s", message)

    qInstallMessageHandler(handler)
