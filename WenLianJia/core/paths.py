"""Filesystem layout for WenLianJia.

All mutable data lives under one root so the application can be moved or
released without touching the source tree.  The eight product subdirectories
are created lazily and are part of the delivery contract.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class AppPaths:
    root: Path

    @property
    def incoming(self) -> Path:
        return self.root / "00_待处理"

    @property
    def processing(self) -> Path:
        return self.root / "01_生产中"

    @property
    def completed(self) -> Path:
        return self.root / "02_已完成"

    @property
    def config(self) -> Path:
        return self.root / "03_系统配置"

    @property
    def records(self) -> Path:
        return self.root / "04_日志与记录"

    @property
    def cache(self) -> Path:
        return self.root / "05_临时缓存"

    @property
    def database(self) -> Path:
        return self.config / "wenlianjia.db"

    @property
    def settings(self) -> Path:
        return self.config / "settings.json"

    @property
    def model_profiles(self) -> Path:
        return self.config / "model_profiles.json"

    @property
    def secrets(self) -> Path:
        return self.config / "secrets.bin"

    @property
    def log_dir(self) -> Path:
        return self.records / "logs"

    @property
    def reports(self) -> Path:
        return self.records / "reports"

    @property
    def books_root(self) -> Path:
        return self.completed / "00_书目"

    @property
    def skus_root(self) -> Path:
        return self.completed / "01_销售商品"

    @property
    def assets_root(self) -> Path:
        return self.completed / "02_资产库"

    @property
    def backups(self) -> Path:
        return self.records / "备份"

    @property
    def batch_reports(self) -> Path:
        return self.reports / "批量操作"

    def book_dir(self, code: str) -> Path:
        path = self.books_root / _safe_component(code or "BOOK")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def sku_dir(self, sku_code: str) -> Path:
        path = self.skus_root / _safe_component(sku_code or "SKU")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def asset_dir(self, sha256: str) -> Path:
        prefix = (sha256 or "00")[:2]
        path = self.assets_root / prefix
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def sku_subdirs(sku_dir: Path) -> dict[str, Path]:
        mapping = {
            "images": sku_dir / "01_商品图片",
            "info": sku_dir / "02_商品信息",
            "delivery": sku_dir / "03_交付记录",
        }
        for path in mapping.values():
            path.mkdir(parents=True, exist_ok=True)
        return mapping

    def ensure(self) -> None:
        for path in (
            self.incoming,
            self.processing,
            self.completed,
            self.config,
            self.records,
            self.cache,
            self.log_dir,
            self.reports,
            self.books_root,
            self.skus_root,
            self.assets_root,
            self.backups,
            self.batch_reports,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def product_dir(self, sku: str, title: str, *, completed: bool = False) -> Path:
        base = self.completed if completed else self.processing
        safe_sku = _safe_component(sku or "NO-SKU")
        safe_title = _safe_component(title or "未命名商品")
        return base / f"{safe_sku}_{safe_title}"

    @staticmethod
    def product_subdirs(product_dir: Path) -> dict[str, Path]:
        mapping = {
            "source": product_dir / "00_原始教材",
            "parsed": product_dir / "01_教材解析",
            "exercises": product_dir / "02_习题数据库",
            "answers": product_dir / "03_AI答案与详解",
            "pdf": product_dir / "04_最终PDF",
            "images": product_dir / "05_商品图片",
            "info": product_dir / "06_商品信息",
            "delivery": product_dir / "07_交付记录",
        }
        for path in mapping.values():
            path.mkdir(parents=True, exist_ok=True)
        return mapping


def _safe_component(value: str) -> str:
    cleaned = "".join(ch for ch in value if ch not in '\\/:*?"<>|\r\n\t').strip()
    cleaned = cleaned.replace(" ", "_")
    return cleaned[:60] or "未命名"


def default_root() -> Path:
    override = os.environ.get("WLJ_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return PROJECT_ROOT / "WenLianJia_Data"


def get_paths(root: Path | None = None) -> AppPaths:
    paths = AppPaths((root or default_root()).resolve())
    paths.ensure()
    return paths
