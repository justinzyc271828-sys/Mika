"""Content-addressed asset store with reference-counted deletion protection."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.library.common import LibraryError, Page, clamp_page, dumps, loads
from core.library.orm import AssetLinkRow, AssetRow
from models.schemas import Asset, utc_now

CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _row_asset(row: AssetRow) -> Asset:
    return Asset(
        id=row.id,
        file_name=row.file_name,
        file_type=row.file_type,
        file_size=int(row.file_size or 0),
        sha256=row.sha256,
        local_path=row.local_path,
        cloud_path=row.cloud_path,
        version=row.version,
        license_source=row.license_source,
        metadata=loads(row.metadata_json, {}),
        created_at=row.created_at,
    )


class AssetLibrary:
    def __init__(self, library):
        self.library = library
        self.paths = library.paths

    # --------------------------------------------------------------- registration

    def add_file(
        self,
        path: Path | str,
        *,
        license_source: str = "",
        version: str = "1",
        copy: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[Asset, bool]:
        """Register a file; returns ``(asset, created)``.

        Identical bytes always resolve to one asset.  The bytes are copied into
        the asset store by default so later renames or SKU deletion cannot break
        another product's reference.
        """

        source = Path(path)
        if not source.is_file():
            raise LibraryError(f"文件不存在：{source}")
        digest = sha256_file(source)
        existing = self.by_sha(digest)
        if existing:
            return existing, False

        suffix = source.suffix.lower()
        destination = self.paths.asset_dir(digest) / f"{digest}{suffix}"
        if copy:
            if not destination.exists():
                shutil.copy2(source, destination)
            local_path = str(destination)
        else:
            local_path = str(source)

        asset = Asset(
            file_name=source.name,
            file_type=suffix.lstrip("."),
            file_size=source.stat().st_size,
            sha256=digest,
            local_path=local_path,
            version=str(version),
            license_source=license_source,
            metadata=metadata or {},
        )
        with self.library.session() as session:
            session.add(
                AssetRow(
                    id=asset.id,
                    file_name=asset.file_name,
                    file_type=asset.file_type,
                    file_size=asset.file_size,
                    sha256=asset.sha256,
                    local_path=asset.local_path,
                    cloud_path="",
                    version=asset.version,
                    license_source=asset.license_source,
                    metadata_json=dumps(asset.metadata),
                    created_at=asset.created_at,
                )
            )
            session.commit()
        self.library.audit(
            "asset.register",
            "asset",
            asset.id,
            after={"file_name": asset.file_name, "sha256": asset.sha256},
            detail=f"登记资产 {asset.file_name}",
        )
        return asset, True

    def by_sha(self, sha256: str) -> Asset | None:
        with self.library.session() as session:
            row = session.scalar(select(AssetRow).where(AssetRow.sha256 == sha256))
            return _row_asset(row) if row else None

    def get(self, asset_id: str) -> Asset | None:
        with self.library.session() as session:
            row = session.get(AssetRow, asset_id)
            return _row_asset(row) if row else None

    # --------------------------------------------------------------------- links

    def link(
        self,
        asset_id: str,
        owner_type: str,
        owner_id: str,
        *,
        role: str = "attachment",
    ) -> None:
        if not self.get(asset_id):
            raise LibraryError(f"资产不存在：{asset_id}")
        with self.library.session() as session:
            existing = session.scalar(
                select(AssetLinkRow).where(
                    AssetLinkRow.asset_id == asset_id,
                    AssetLinkRow.owner_type == owner_type,
                    AssetLinkRow.owner_id == owner_id,
                    AssetLinkRow.role == role,
                )
            )
            if existing is None:
                session.add(
                    AssetLinkRow(
                        id=f"lnk_{utc_now()}_{asset_id[:8]}_{owner_id[:8]}",
                        asset_id=asset_id,
                        owner_type=owner_type,
                        owner_id=owner_id,
                        role=role,
                        created_at=utc_now(),
                    )
                )
                session.commit()

    def unlink(
        self,
        asset_id: str,
        owner_type: str,
        owner_id: str,
        *,
        role: str | None = None,
    ) -> int:
        with self.library.session() as session:
            query = select(AssetLinkRow).where(
                AssetLinkRow.asset_id == asset_id,
                AssetLinkRow.owner_type == owner_type,
                AssetLinkRow.owner_id == owner_id,
            )
            if role is not None:
                query = query.where(AssetLinkRow.role == role)
            rows = list(session.scalars(query))
            for row in rows:
                session.delete(row)
            session.commit()
            return len(rows)

    def unlink_owner(self, owner_type: str, owner_id: str) -> int:
        with self.library.session() as session:
            rows = list(
                session.scalars(
                    select(AssetLinkRow).where(
                        AssetLinkRow.owner_type == owner_type,
                        AssetLinkRow.owner_id == owner_id,
                    )
                )
            )
            for row in rows:
                session.delete(row)
            session.commit()
            return len(rows)

    def links_for(self, asset_id: str) -> list[dict[str, Any]]:
        with self.library.session() as session:
            rows = list(
                session.scalars(
                    select(AssetLinkRow)
                    .where(AssetLinkRow.asset_id == asset_id)
                    .order_by(AssetLinkRow.created_at)
                )
            )
            return [
                {
                    "id": row.id,
                    "owner_type": row.owner_type,
                    "owner_id": row.owner_id,
                    "role": row.role,
                    "created_at": row.created_at,
                }
                for row in rows
            ]

    def links_for_owner(self, owner_type: str, owner_id: str) -> list[dict[str, Any]]:
        with self.library.session() as session:
            rows = list(
                session.scalars(
                    select(AssetLinkRow)
                    .where(
                        AssetLinkRow.owner_type == owner_type,
                        AssetLinkRow.owner_id == owner_id,
                    )
                    .order_by(AssetLinkRow.created_at)
                )
            )
            return [
                {
                    "id": row.id,
                    "asset_id": row.asset_id,
                    "owner_type": row.owner_type,
                    "owner_id": row.owner_id,
                    "role": row.role,
                    "created_at": row.created_at,
                }
                for row in rows
            ]

    def assets_for(self, owner_type: str, owner_id: str) -> list[Asset]:
        with self.library.session() as session:
            rows = list(
                session.scalars(
                    select(AssetRow)
                    .join(AssetLinkRow, AssetLinkRow.asset_id == AssetRow.id)
                    .where(
                        AssetLinkRow.owner_type == owner_type,
                        AssetLinkRow.owner_id == owner_id,
                    )
                    .order_by(AssetRow.created_at)
                )
            )
            return [_row_asset(row) for row in rows]

    def ref_count(self, asset_id: str) -> int:
        with self.library.session() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(AssetLinkRow)
                    .where(AssetLinkRow.asset_id == asset_id)
                )
                or 0
            )

    # -------------------------------------------------------------------- listing

    def list_assets(
        self,
        *,
        q: str = "",
        page: int = 1,
        page_size: int = 50,
        only_orphans: bool = False,
    ) -> Page[dict[str, Any]]:
        page, page_size = clamp_page(page, page_size)
        with self.library.session() as session:
            query = select(AssetRow)
            count_query = select(func.count()).select_from(AssetRow)
            if q.strip():
                like = f"%{q.strip()}%"
                query = query.where(
                    AssetRow.file_name.like(like) | AssetRow.sha256.like(like)
                )
                count_query = count_query.where(
                    AssetRow.file_name.like(like) | AssetRow.sha256.like(like)
                )
            if only_orphans:
                subquery = select(AssetLinkRow.asset_id)
                query = query.where(AssetRow.id.not_in(subquery))
                count_query = count_query.where(AssetRow.id.not_in(subquery))
            total = int(session.scalar(count_query) or 0)
            rows = list(
                session.scalars(
                    query.order_by(AssetRow.created_at.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            items = []
            for row in rows:
                asset = _row_asset(row)
                items.append(
                    {
                        **asset.model_dump(mode="json"),
                        "refs": self.ref_count(asset.id),
                    }
                )
            return Page(items=items, total=total, page=page, page_size=page_size)

    def orphans(self) -> list[Asset]:
        with self.library.session() as session:
            subquery = select(AssetLinkRow.asset_id)
            rows = list(
                session.scalars(
                    select(AssetRow)
                    .where(AssetRow.id.not_in(subquery))
                    .order_by(AssetRow.created_at)
                )
            )
            return [_row_asset(row) for row in rows]

    # ------------------------------------------------------------------ deletion

    def delete_asset(
        self,
        asset_id: str,
        *,
        confirm: bool = False,
        delete_file: bool = False,
    ) -> dict[str, Any]:
        if not confirm:
            raise LibraryError("删除资产需要确认")
        refs = self.ref_count(asset_id)
        if refs:
            raise LibraryError(
                f"资产仍被 {refs} 个商品或版本引用，不能删除；请先解除引用"
            )
        asset = self.get(asset_id)
        if asset is None:
            raise LibraryError(f"资产不存在：{asset_id}")
        with self.library.session() as session:
            row = session.get(AssetRow, asset_id)
            if row is not None:
                session.delete(row)
                session.commit()
        removed_file = False
        if delete_file and asset.local_path:
            path = Path(asset.local_path)
            if path.is_file():
                path.unlink()
                removed_file = True
        self.library.audit(
            "asset.delete",
            "asset",
            asset_id,
            before={"file_name": asset.file_name, "sha256": asset.sha256},
            detail="删除未被引用的资产",
        )
        return {"asset_id": asset_id, "removed_file": removed_file}

    # ----------------------------------------------------------------- integrity

    def verify(self, asset_id: str | None = None) -> list[dict[str, Any]]:
        with self.library.session() as session:
            if asset_id:
                row = session.get(AssetRow, asset_id)
                rows = [row] if row else []
            else:
                rows = list(session.scalars(select(AssetRow).order_by(AssetRow.created_at)))
            report = []
            for row in rows:
                path = Path(row.local_path) if row.local_path else None
                if path is None or not path.is_file():
                    status = "missing"
                else:
                    actual = sha256_file(path)
                    status = "ok" if actual == row.sha256 else "hash_mismatch"
                report.append(
                    {
                        "asset_id": row.id,
                        "file_name": row.file_name,
                        "path": row.local_path,
                        "expected_sha256": row.sha256,
                        "status": status,
                    }
                )
            return report

    def usage_summary(self) -> dict[str, Any]:
        with self.library.session() as session:
            total = int(session.scalar(select(func.count()).select_from(AssetRow)) or 0)
            bytes_total = int(session.scalar(select(func.sum(AssetRow.file_size))) or 0)
            linked = int(
                session.scalar(
                    select(func.count(func.distinct(AssetLinkRow.asset_id)))
                )
                or 0
            )
        return {"assets": total, "bytes": bytes_total, "linked": linked, "orphans": total - linked}
