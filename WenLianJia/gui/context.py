"""Application services shared by the GUI pages."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtWidgets import QApplication

from core.ai_client import AiClient
from core.ai_solver import SolverService
from core.answer_verifier import AnswerVerifier
from core.baidu_netdisk import BaiduNetdisk
from core.book_lookup import BookLookupService
from core.catalog import CatalogManager
from core.config import SettingsManager
from core.db import Database
from core.library.backup import BackupManager
from core.library.batch import BatchEngine
from core.library.service import LibraryService
from core.paths import AppPaths
from core.prompts import PromptLibrary
from core.secrets import SecretStore
from models.schemas import Product


@dataclass
class AppContext:
    paths: AppPaths
    settings: SettingsManager
    db: Database
    secrets: SecretStore
    prompts: PromptLibrary
    _library: LibraryService | None = field(default=None, init=False, repr=False)

    @classmethod
    def create(cls, paths: AppPaths, settings: SettingsManager) -> "AppContext":
        return cls(
            paths=paths,
            settings=settings,
            db=Database(paths.database),
            secrets=SecretStore(paths.secrets),
            prompts=PromptLibrary(),
        )

    # Services are created on demand.  They are cheap wrappers; the OpenAI
    # client is only instantiated on the thread that will use it.
    def ai_client(self) -> AiClient:
        return AiClient(self.settings, self.secrets, self.db)

    def solver(self) -> SolverService:
        return SolverService(self.settings, self.db, self.ai_client(), self.prompts)

    def verifier(self) -> AnswerVerifier:
        return AnswerVerifier(self.settings, self.db, self.ai_client(), self.prompts)

    def netdisk(self) -> BaiduNetdisk:
        return BaiduNetdisk(self.settings, self.secrets, self.db, self.paths)

    def catalog(self) -> CatalogManager:
        return CatalogManager(self.settings, self.db, self.paths)

    def book_lookup(self) -> BookLookupService:
        return BookLookupService(self.settings, self.db, self.secrets, self.paths)

    def library(self) -> LibraryService:
        if self._library is None:
            self._library = LibraryService(self.paths, self.settings, self.db)
        return self._library

    def batch(self) -> BatchEngine:
        return BatchEngine(self.library())

    def backup(self) -> BackupManager:
        return BackupManager(self.library())

    def ai_configured(self) -> tuple[bool, str]:
        return self.ai_client().configured()

    def netdisk_configured(self) -> tuple[bool, str]:
        client = self.netdisk()
        ok = client.configured()
        return ok, "" if ok else "百度网盘 AppKey、SecretKey 或 App 名称不完整"

    def product_dir(self, product: Product) -> Path:
        return self.paths.product_dir(product.sku, product.title)

    def product_subdirs(self, product: Product) -> dict[str, Path]:
        return self.paths.product_subdirs(self.product_dir(product))


def app_context(paths: AppPaths, settings: SettingsManager) -> AppContext:
    return AppContext.create(paths, settings)
