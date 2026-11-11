"""Database backup, metadata export, restore and integrity checks."""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from core.library.assets import sha256_file
from core.library.common import LibraryError, dumps
from core.library.orm import (
    AssetLinkRow,
    AssetRow,
    BookVersionRow,
    CategoryRow,
    FamilyRow,
    TagRow,
    books_t,
    skus_t,
)
from core.library.service import LibraryService, _asset_from_row, _book_from_row, _sku_from_row
from models.schemas import utc_now

BACKUP_PREFIX = "wenlianjia"


class BackupError(LibraryError):
    pass


class BackupManager:
    def __init__(self, service: LibraryService):
        self.service = service
        self.paths = service.paths
        self.db = service.db
        self.engine = service.engine

    # ------------------------------------------------------------------ backup

    def create_backup(self, *, label: str = "") -> dict[str, Any]:
        self.paths.backups.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = f"-{label}" if label else ""
        target = self.paths.backups / f"{BACKUP_PREFIX}-{stamp}{suffix}.db"
        self._checkpoint_and_close()
        source = sqlite3.connect(self.db.path)
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
            destination.execute("PRAGMA integrity_check")
        finally:
            destination.close()
            source.close()
        manifest = {
            "database": str(target),
            "created_at": utc_now(),
            "sha256": sha256_file(target),
            "size": target.stat().st_size,
            "schema_version": self._schema_version(),
            "counts": self._counts(),
        }
        manifest_path = target.with_suffix(".manifest.json")
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        metadata_path = self.export_metadata(
            self.paths.backups / f"{BACKUP_PREFIX}-{stamp}{suffix}-metadata.json"
        )
        manifest["metadata_export"] = str(metadata_path)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.service.audit(
            "backup.create",
            "backup",
            target.name,
            after={"path": str(target), "sha256": manifest["sha256"]},
            detail="创建数据库与元数据备份",
        )
        return manifest

    def _checkpoint_and_close(self) -> None:
        try:
            self.db.close_all()
        finally:
            self.engine.dispose()
        connection = sqlite3.connect(self.db.path)
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()

    def list_backups(self) -> list[dict[str, Any]]:
        if not self.paths.backups.exists():
            return []
        result = []
        for path in sorted(self.paths.backups.glob(f"{BACKUP_PREFIX}-*.db"), reverse=True):
            manifest_path = path.with_suffix(".manifest.json")
            manifest = {}
            if manifest_path.is_file():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    manifest = {}
            result.append(
                {
                    "path": str(path),
                    "name": path.name,
                    "size": path.stat().st_size,
                    "created_at": manifest.get("created_at", ""),
                    "sha256": manifest.get("sha256", ""),
                    "counts": manifest.get("counts", {}),
                }
            )
        return result

    def create_auto_backup(self, *, interval_hours: float = 24.0) -> dict[str, Any] | None:
        """Create at most one automatic backup per interval.

        Called at startup.  A recent backup (manual or automatic) suppresses a
        new one, so repeated launches do not fill the backup directory.
        """
        if interval_hours <= 0:
            raise BackupError("自动备份间隔必须大于 0 小时")
        if not self.paths.backups.exists():
            return self.create_backup(label="auto")
        latest = 0.0
        for path in self.paths.backups.glob(f"{BACKUP_PREFIX}-*.db"):
            try:
                latest = max(latest, path.stat().st_mtime)
            except OSError:
                continue
        if latest and time.time() - latest < interval_hours * 3600:
            return None
        return self.create_backup(label="auto")

    # ----------------------------------------------------------------- restore

    def restore_backup(self, path: Path | str, *, confirm: bool = False) -> dict[str, Any]:
        if not confirm:
            raise BackupError("恢复数据库需要确认")
        source_path = Path(path)
        if not source_path.is_file():
            raise BackupError(f"备份文件不存在：{source_path}")
        self._assert_sqlite(source_path)
        self.paths.backups.mkdir(parents=True, exist_ok=True)
        safety = self.paths.backups / f"{BACKUP_PREFIX}-pre-restore-{datetime.now():%Y%m%d-%H%M%S}.db"
        self._checkpoint_and_close()
        shutil.copy2(self.db.path, safety)
        source = sqlite3.connect(source_path)
        destination = sqlite3.connect(self.db.path)
        try:
            source.backup(destination)
            destination.execute("PRAGMA integrity_check")
        finally:
            destination.close()
            source.close()
        # Re-open through the normal layers so migrations and row mappers match.
        self.db.init_schema()
        from core.library.migrations import ensure_schema

        ensure_schema(self.engine)
        counts = self._counts()
        self.service.audit(
            "backup.restore",
            "backup",
            source_path.name,
            after={"counts": counts, "safety": str(safety)},
            detail="从备份恢复数据库",
        )
        return {
            "restored_from": str(source_path),
            "safety_copy": str(safety),
            "counts": counts,
        }

    def verify_backup(self, path: Path | str) -> dict[str, Any]:
        source_path = Path(path)
        if not source_path.is_file():
            raise BackupError(f"备份文件不存在：{source_path}")
        self._assert_sqlite(source_path)
        connection = sqlite3.connect(source_path)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            tables = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
            counts = {}
            for table in ("books", "skus", "products", "product_families", "assets"):
                if table in tables:
                    counts[table] = connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
        finally:
            connection.close()
        return {
            "path": str(source_path),
            "integrity": integrity,
            "ok": integrity == "ok",
            "tables": tables,
            "counts": counts,
            "sha256": sha256_file(source_path),
        }

    def _assert_sqlite(self, path: Path) -> None:
        try:
            connection = sqlite3.connect(path)
            try:
                connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
            finally:
                connection.close()
        except sqlite3.DatabaseError as exc:
            raise BackupError(f"不是有效的 SQLite 数据库：{exc}") from exc

    # --------------------------------------------------------------- metadata

    def export_metadata(self, path: Path | str | None = None) -> Path:
        destination = Path(path) if path else (
            self.paths.backups / f"{BACKUP_PREFIX}-metadata-{datetime.now():%Y%m%d-%H%M%S}.json"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "exported_at": utc_now(),
            "schema_version": self._schema_version(),
            "families": [family.model_dump(mode="json") for family in self.service.list_families(include_inactive=True)],
            "categories": [category.model_dump(mode="json") for category in self.service.list_categories()],
            "tags": [tag.model_dump(mode="json") for tag in self.service.list_tags()],
            "books": [],
            "versions": [],
            "skus": [],
            "assets": [],
            "asset_links": [],
        }
        page = 1
        while True:
            result = self.service.list_books(page=page, page_size=500)
            payload["books"].extend(book.model_dump(mode="json") for book in result.items)
            if page >= result.pages:
                break
            page += 1
        page = 1
        while True:
            result = self.service.list_skus(page=page, page_size=500)
            payload["skus"].extend(sku.model_dump(mode="json") for sku in result.items)
            if page >= result.pages:
                break
            page += 1
        with self.service.session() as session:
            payload["versions"] = [
                {
                    "id": row.id,
                    "book_id": row.book_id,
                    "version_label": row.version_label,
                    "status": row.status,
                    "product_id": row.product_id,
                    "created_at": row.created_at,
                }
                for row in session.scalars(select(BookVersionRow))
            ]
            payload["assets"] = [
                _asset_from_row(row).model_dump(mode="json")
                for row in session.scalars(select(AssetRow))
            ]
            payload["asset_links"] = [
                {
                    "asset_id": row.asset_id,
                    "owner_type": row.owner_type,
                    "owner_id": row.owner_id,
                    "role": row.role,
                }
                for row in session.scalars(select(AssetLinkRow))
            ]
        destination.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return destination

    # ---------------------------------------------------------------- integrity

    def integrity_check(self) -> dict[str, Any]:
        connection = sqlite3.connect(self.db.path)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            connection.close()
        assets = self.service.assets.verify()
        asset_bad = [entry for entry in assets if entry["status"] != "ok"]
        with self.service.session() as session:
            orphan_skus = [
                row[0]
                for row in session.execute(
                    select(skus_t.c.sku_code).where(
                        skus_t.c.book_id.not_in(select(books_t.c.id))
                    )
                )
            ]
            books_without_sku = int(
                session.scalar(
                    select(func.count()).select_from(books_t).where(
                        books_t.c.id.not_in(select(skus_t.c.book_id))
                    )
                )
                or 0
            )
            duplicate_sha = list(
                session.execute(
                    select(AssetRow.sha256, func.count())
                    .group_by(AssetRow.sha256)
                    .having(func.count() > 1)
                )
            )
        return {
            "checked_at": utc_now(),
            "database_integrity": integrity,
            "database_ok": integrity == "ok",
            "assets_checked": len(assets),
            "assets_bad": asset_bad,
            "orphan_skus": orphan_skus,
            "books_without_sku": books_without_sku,
            "duplicate_sha256": [row[0] for row in duplicate_sha],
            "ok": integrity == "ok" and not asset_bad and not orphan_skus and not duplicate_sha,
        }

    # ------------------------------------------------------------------ helpers

    def _schema_version(self) -> int:
        connection = sqlite3.connect(self.db.path)
        try:
            row = connection.execute(
                "SELECT MAX(version) FROM library_migrations"
            ).fetchone()
            return int(row[0] or 0)
        except sqlite3.DatabaseError:
            return 0
        finally:
            connection.close()

    def _counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        connection = sqlite3.connect(self.db.path)
        try:
            tables = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ]
            for table in (
                "books",
                "skus",
                "products",
                "product_families",
                "categories",
                "tags",
                "book_versions",
                "assets",
                "asset_links",
                "batch_jobs",
                "audit_log",
            ):
                if table in tables:
                    counts[table] = connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
        finally:
            connection.close()
        return counts
