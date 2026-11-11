"""Catalogue service for the chapter-26 product library.

The service owns product families, categories, tags, textbook masters, immutable
versions and sale SKUs.  It deliberately keeps the legacy production tables as
the single source of truth for books/SKUs: every write goes through
:class:`core.db.Database`, while reads for search and pagination use SQLAlchemy
so 10,000+ rows never require a full-table Python scan.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from core.catalog import CatalogError, cents_to_yuan, yuan_to_cents
from core.db import Database
from core.library.assets import AssetLibrary
from core.library.common import LibraryError, Page, clamp_page, dumps, loads
from core.library.lifecycle import (
    LIFECYCLE_LABELS,
    require_transition,
    timestamps_for,
)
from core.library.migrations import create_library_engine, ensure_schema
from core.library.orm import (
    AssetLinkRow,
    AuditRow,
    BatchItemRow,
    BatchJobRow,
    BookVersionRow,
    CategoryRow,
    EntityTagRow,
    FamilyRow,
    TagRow,
    books_t,
    skus_t,
)
from core.library.templates import (
    DEFAULT_FAMILY,
    DEFAULT_FAMILY_CODE,
    ResolvedTemplate,
    family_payload,
    resolve_template,
    validate_overrides,
)
from models.schemas import (
    Asset,
    AssetLink,
    AuditEntry,
    BatchJob,
    BatchJobItem,
    BatchStatus,
    Book,
    BookVersion,
    BookVersionStatus,
    Category,
    DeliveryStatus,
    ProductFamily,
    SalesSKU,
    SkuItem,
    SkuLifecycle,
    Tag,
    new_id,
    utc_now,
)

log = logging.getLogger(__name__)

READY_PRODUCTION = {"packaged", "completed", "ready", "READY", "PUBLISHED"}
BOOK_STATUSES = {"ACTIVE", "ARCHIVED"}
SKU_SORTS = {
    "updated": skus_t.c.updated_at,
    "price": skus_t.c.price_cents,
    "code": skus_t.c.sku_code,
    "name": skus_t.c.name,
}


# --------------------------------------------------------------------------- mappers


def _book_from_row(row: Any) -> Book:
    data = dict(row._mapping)
    data["authors"] = loads(data.pop("authors_json", "[]"), [])
    data["translators"] = loads(data.pop("translators_json", "[]"), [])
    data["template_overrides"] = loads(data.pop("template_overrides_json", "{}"), {})
    data["metadata"] = loads(data.pop("metadata_json", "{}"), {})
    data["source_verified"] = bool(data.get("source_verified"))
    data["subject"] = data.get("subject") or "other"
    return Book(**data)


def _sku_from_row(row: Any) -> SalesSKU:
    data = dict(row._mapping)
    items = loads(data.pop("items_json", "[]"), [])
    data["items"] = [SkuItem(**item) for item in items]
    data["enabled"] = bool(data.get("enabled"))
    data["allow_above_parts"] = bool(data.get("allow_above_parts"))
    try:
        data["lifecycle"] = SkuLifecycle(data.get("lifecycle") or "DRAFT")
    except ValueError:
        data["lifecycle"] = SkuLifecycle.DRAFT
    try:
        data["delivery_status"] = DeliveryStatus(
            data.get("delivery_status") or "NOT_DELIVERED"
        )
    except ValueError:
        data["delivery_status"] = DeliveryStatus.NOT_DELIVERED
    return SalesSKU(**data)


def _family_from_row(row: FamilyRow) -> ProductFamily:
    return ProductFamily(
        id=row.id,
        code=row.code,
        name=row.name,
        description=row.description,
        is_default=bool(row.is_default),
        brand_name=row.brand_name,
        visual_style=loads(row.visual_style_json, {}),
        image_template=loads(row.image_template_json, {}),
        detail_template=loads(row.detail_template_json, {}),
        intro_template=row.intro_template,
        pdf_template=row.pdf_template,
        watermark=loads(row.watermark_json, {}),
        pricing=loads(row.pricing_json, {}),
        default_category_id=row.default_category_id,
        delivery_rules=loads(row.delivery_rules_json, {}),
        netdisk_rules=loads(row.netdisk_rules_json, {}),
        version=int(row.version or 1),
        active=bool(row.active),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _category_from_row(row: CategoryRow) -> Category:
    return Category(
        id=row.id,
        code=row.code,
        name=row.name,
        parent_id=row.parent_id,
        sort_order=row.sort_order,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _tag_from_row(row: TagRow) -> Tag:
    return Tag(id=row.id, name=row.name, color=row.color, created_at=row.created_at)


def _version_from_row(row: BookVersionRow) -> BookVersion:
    try:
        status = BookVersionStatus(row.status)
    except ValueError:
        status = BookVersionStatus.DRAFT
    return BookVersion(
        id=row.id,
        book_id=row.book_id,
        version_label=row.version_label,
        status=status,
        notes=row.notes,
        product_id=row.product_id,
        created_by=row.created_by,
        metadata=loads(row.metadata_json, {}),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _asset_from_row(row) -> Asset:
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


def _batch_from_row(row: BatchJobRow) -> BatchJob:
    try:
        status = BatchStatus(row.status)
    except ValueError:
        status = BatchStatus.PREVIEW
    return BatchJob(
        id=row.id,
        kind=row.kind,
        status=status,
        total=row.total,
        processed=row.processed,
        succeeded=row.succeeded,
        failed=row.failed,
        actor=row.actor,
        options=loads(row.options_json, {}),
        preview=loads(row.preview_json, []),
        report=loads(row.report_json, {}),
        error=row.error,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


# BatchStatus is imported late in some paths; keep one canonical reference.


class LibraryService:
    """High-level operations used by the product center and the batch engine."""

    def __init__(self, paths, settings, db: Database):
        self.paths = paths
        self.settings = settings
        self.db = db
        self.engine = create_library_engine(paths.database)
        ensure_schema(self.engine)
        self.assets = AssetLibrary(self)

    # ----------------------------------------------------------------- plumbing

    @contextmanager
    def session(self):
        with Session(self.engine) as session:
            yield session

    def audit(
        self,
        action: str,
        entity_type: str,
        entity_id: str,
        *,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        detail: str = "",
        actor: str = "owner",
    ) -> None:
        try:
            with self.session() as session:
                session.add(
                    AuditRow(
                        id=new_id("aud"),
                        actor=actor,
                        action=action,
                        entity_type=entity_type,
                        entity_id=entity_id,
                        before_json=dumps(before or {}),
                        after_json=dumps(after or {}),
                        detail=detail,
                        created_at=utc_now(),
                    )
                )
                session.commit()
        except Exception:  # an audit failure must not corrupt the business write
            log.exception("audit write failed for %s %s", entity_type, entity_id)

    def list_audit(
        self, *, page: int = 1, page_size: int = 50, entity_id: str = ""
    ) -> Page[AuditEntry]:
        page, page_size = clamp_page(page, page_size)
        with self.session() as session:
            query = select(AuditRow)
            count_query = select(func.count()).select_from(AuditRow)
            if entity_id:
                query = query.where(AuditRow.entity_id == entity_id)
                count_query = count_query.where(AuditRow.entity_id == entity_id)
            total = int(session.scalar(count_query) or 0)
            rows = list(
                session.scalars(
                    query.order_by(AuditRow.created_at.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            items = [
                AuditEntry(
                    id=row.id,
                    actor=row.actor,
                    action=row.action,
                    entity_type=row.entity_type,
                    entity_id=row.entity_id,
                    before=loads(row.before_json, {}),
                    after=loads(row.after_json, {}),
                    detail=row.detail,
                    created_at=row.created_at,
                )
                for row in rows
            ]
            return Page(items=items, total=total, page=page, page_size=page_size)

    # ------------------------------------------------------------------ families

    def ensure_default_family(self) -> ProductFamily:
        with self.session() as session:
            row = session.scalar(select(FamilyRow).where(FamilyRow.code == DEFAULT_FAMILY_CODE))
            if row is None:
                family = ProductFamily(**DEFAULT_FAMILY)
                row = FamilyRow(
                    id=family.id,
                    code=family.code,
                    name=family.name,
                    description=family.description,
                    is_default=1,
                    brand_name=family.brand_name,
                    visual_style_json=dumps(family.visual_style),
                    image_template_json=dumps(family.image_template),
                    detail_template_json=dumps(family.detail_template),
                    intro_template=family.intro_template,
                    pdf_template=family.pdf_template,
                    watermark_json=dumps(family.watermark),
                    pricing_json=dumps(family.pricing),
                    default_category_id=family.default_category_id,
                    delivery_rules_json=dumps(family.delivery_rules),
                    netdisk_rules_json=dumps(family.netdisk_rules),
                    version=family.version,
                    active=1,
                    created_at=family.created_at,
                    updated_at=family.updated_at,
                )
                session.add(row)
                session.commit()
                self.audit("family.create", "family", row.id, detail="创建默认商品系列模板")
                return _family_from_row(row)
            if not row.is_default:
                session.execute(
                    FamilyRow.__table__.update().values(is_default=0)
                )
                row.is_default = 1
                row.updated_at = utc_now()
                session.commit()
            return _family_from_row(row)

    def list_families(self, *, include_inactive: bool = False) -> list[ProductFamily]:
        self.ensure_default_family()
        with self.session() as session:
            query = select(FamilyRow).order_by(FamilyRow.is_default.desc(), FamilyRow.created_at)
            if not include_inactive:
                query = query.where(FamilyRow.active == 1)
            return [_family_from_row(row) for row in session.scalars(query)]

    def get_family(self, family_id: str) -> ProductFamily | None:
        with self.session() as session:
            row = session.get(FamilyRow, family_id)
            return _family_from_row(row) if row else None

    def default_family(self) -> ProductFamily:
        with self.session() as session:
            row = session.scalar(
                select(FamilyRow).where(FamilyRow.is_default == 1, FamilyRow.active == 1)
            )
            if row is not None:
                return _family_from_row(row)
        return self.ensure_default_family()

    def create_family(
        self,
        *,
        code: str,
        name: str,
        description: str = "",
        brand_name: str = "",
        visual_style: dict | None = None,
        image_template: dict | None = None,
        detail_template: dict | None = None,
        intro_template: str = "",
        pdf_template: str = "",
        watermark: dict | None = None,
        pricing: dict | None = None,
        default_category_id: str = "",
        delivery_rules: dict | None = None,
        netdisk_rules: dict | None = None,
    ) -> ProductFamily:
        code = code.strip()
        name = name.strip()
        if not code or not name:
            raise LibraryError("商品系列编号和名称不能为空")
        with self.session() as session:
            if session.scalar(select(FamilyRow).where(FamilyRow.code == code)):
                raise LibraryError(f"商品系列编号已存在：{code}")
            family = ProductFamily(
                code=code,
                name=name,
                description=description,
                is_default=False,
                brand_name=brand_name,
                visual_style=visual_style or {},
                image_template=image_template or {},
                detail_template=detail_template or {},
                intro_template=intro_template,
                pdf_template=pdf_template,
                watermark=watermark or {},
                pricing=pricing or {},
                default_category_id=default_category_id,
                delivery_rules=delivery_rules or {},
                netdisk_rules=netdisk_rules or {},
            )
            session.add(
                FamilyRow(
                    id=family.id,
                    code=family.code,
                    name=family.name,
                    description=family.description,
                    is_default=0,
                    brand_name=family.brand_name,
                    visual_style_json=dumps(family.visual_style),
                    image_template_json=dumps(family.image_template),
                    detail_template_json=dumps(family.detail_template),
                    intro_template=family.intro_template,
                    pdf_template=family.pdf_template,
                    watermark_json=dumps(family.watermark),
                    pricing_json=dumps(family.pricing),
                    default_category_id=family.default_category_id,
                    delivery_rules_json=dumps(family.delivery_rules),
                    netdisk_rules_json=dumps(family.netdisk_rules),
                    version=1,
                    active=1,
                    created_at=family.created_at,
                    updated_at=family.updated_at,
                )
            )
            session.commit()
        self.audit("family.create", "family", family.id, detail=f"创建商品系列 {name}")
        return family

    def update_family(self, family_id: str, **fields: Any) -> ProductFamily:
        json_fields = {
            "visual_style": "visual_style_json",
            "image_template": "image_template_json",
            "detail_template": "detail_template_json",
            "watermark": "watermark_json",
            "pricing": "pricing_json",
            "delivery_rules": "delivery_rules_json",
            "netdisk_rules": "netdisk_rules_json",
        }
        allowed = {
            "name",
            "description",
            "brand_name",
            "intro_template",
            "pdf_template",
            "default_category_id",
            "active",
        } | set(json_fields)
        with self.session() as session:
            row = session.get(FamilyRow, family_id)
            if row is None:
                raise LibraryError(f"商品系列不存在：{family_id}")
            before = family_payload(_family_from_row(row))
            if fields.get("is_default"):
                session.execute(FamilyRow.__table__.update().values(is_default=0))
                row.is_default = 1
            for key, value in fields.items():
                if key == "is_default":
                    continue
                if key in json_fields:
                    setattr(row, json_fields[key], dumps(value or {}))
                elif key in allowed:
                    setattr(row, key, int(bool(value)) if key == "active" else value)
            row.version = int(row.version or 1) + 1
            row.updated_at = utc_now()
            session.commit()
            after = family_payload(_family_from_row(row))
        self.audit(
            "family.update",
            "family",
            family_id,
            before=before,
            after=after,
            detail="更新商品系列模板（不自动改写已有商品）",
        )
        return _family_from_row(row)

    def set_default_family(self, family_id: str) -> ProductFamily:
        return self.update_family(family_id, is_default=True)

    # ----------------------------------------------------------------- categories

    def create_category(
        self, *, code: str, name: str, parent_id: str = "", sort_order: int = 0
    ) -> Category:
        code = code.strip()
        name = name.strip()
        if not code or not name:
            raise LibraryError("分类编号和名称不能为空")
        with self.session() as session:
            if session.scalar(select(CategoryRow).where(CategoryRow.code == code)):
                raise LibraryError(f"分类编号已存在：{code}")
            if parent_id and session.get(CategoryRow, parent_id) is None:
                raise LibraryError(f"上级分类不存在：{parent_id}")
            category = Category(
                code=code, name=name, parent_id=parent_id, sort_order=sort_order
            )
            session.add(
                CategoryRow(
                    id=category.id,
                    code=category.code,
                    name=category.name,
                    parent_id=category.parent_id,
                    sort_order=category.sort_order,
                    created_at=category.created_at,
                    updated_at=category.updated_at,
                )
            )
            session.commit()
        self.audit("category.create", "category", category.id, detail=f"创建分类 {name}")
        return category

    def list_categories(self) -> list[Category]:
        with self.session() as session:
            rows = session.scalars(
                select(CategoryRow).order_by(CategoryRow.sort_order, CategoryRow.name)
            )
            return [_category_from_row(row) for row in rows]

    def category_tree(self) -> list[dict[str, Any]]:
        categories = self.list_categories()
        by_parent: dict[str, list[Category]] = {}
        for category in categories:
            by_parent.setdefault(category.parent_id, []).append(category)

        def build(parent_id: str) -> list[dict[str, Any]]:
            return [
                {**category.model_dump(mode="json"), "children": build(category.id)}
                for category in by_parent.get(parent_id, [])
            ]

        return build("")

    def update_category(self, category_id: str, **fields: Any) -> Category:
        allowed = {"code", "name", "parent_id", "sort_order"}
        with self.session() as session:
            row = session.get(CategoryRow, category_id)
            if row is None:
                raise LibraryError(f"分类不存在：{category_id}")
            for key, value in fields.items():
                if key in allowed:
                    setattr(row, key, value)
            row.updated_at = utc_now()
            session.commit()
            return _category_from_row(row)

    def delete_category(self, category_id: str) -> None:
        with self.session() as session:
            row = session.get(CategoryRow, category_id)
            if row is None:
                raise LibraryError(f"分类不存在：{category_id}")
            child = session.scalar(
                select(func.count()).select_from(CategoryRow).where(
                    CategoryRow.parent_id == category_id
                )
            )
            if child:
                raise LibraryError("该分类仍有下级分类，不能删除")
            used = session.scalar(
                select(func.count()).select_from(books_t).where(
                    books_t.c.category_id == category_id
                )
            )
            if used:
                raise LibraryError(f"该分类仍被 {used} 本教材使用，不能删除")
            session.delete(row)
            session.commit()
        self.audit("category.delete", "category", category_id, detail="删除分类")

    # ----------------------------------------------------------------------- tags

    def create_tag(self, name: str, color: str = "") -> Tag:
        name = name.strip()
        if not name:
            raise LibraryError("标签名称不能为空")
        with self.session() as session:
            if session.scalar(select(TagRow).where(TagRow.name == name)):
                raise LibraryError(f"标签已存在：{name}")
            tag = Tag(name=name, color=color)
            session.add(
                TagRow(id=tag.id, name=tag.name, color=tag.color, created_at=tag.created_at)
            )
            session.commit()
        self.audit("tag.create", "tag", tag.id, detail=f"创建标签 {name}")
        return tag

    def list_tags(self) -> list[Tag]:
        with self.session() as session:
            return [
                _tag_from_row(row)
                for row in session.scalars(select(TagRow).order_by(TagRow.name))
            ]

    def tag_entity(self, entity_type: str, entity_id: str, tag_id: str) -> None:
        with self.session() as session:
            if session.get(TagRow, tag_id) is None:
                raise LibraryError(f"标签不存在：{tag_id}")
            existing = session.scalar(
                select(EntityTagRow).where(
                    EntityTagRow.entity_type == entity_type,
                    EntityTagRow.entity_id == entity_id,
                    EntityTagRow.tag_id == tag_id,
                )
            )
            if existing is None:
                session.add(
                    EntityTagRow(
                        id=new_id("etg"),
                        entity_type=entity_type,
                        entity_id=entity_id,
                        tag_id=tag_id,
                        created_at=utc_now(),
                    )
                )
                session.commit()

    def untag_entity(self, entity_type: str, entity_id: str, tag_id: str) -> int:
        with self.session() as session:
            rows = list(
                session.scalars(
                    select(EntityTagRow).where(
                        EntityTagRow.entity_type == entity_type,
                        EntityTagRow.entity_id == entity_id,
                        EntityTagRow.tag_id == tag_id,
                    )
                )
            )
            for row in rows:
                session.delete(row)
            session.commit()
            return len(rows)

    def tags_for(self, entity_type: str, entity_id: str) -> list[Tag]:
        with self.session() as session:
            rows = session.scalars(
                select(TagRow)
                .join(EntityTagRow, EntityTagRow.tag_id == TagRow.id)
                .where(
                    EntityTagRow.entity_type == entity_type,
                    EntityTagRow.entity_id == entity_id,
                )
                .order_by(TagRow.name)
            )
            return [_tag_from_row(row) for row in rows]

    def delete_tag(self, tag_id: str) -> None:
        with self.session() as session:
            used = session.scalar(
                select(func.count()).select_from(EntityTagRow).where(
                    EntityTagRow.tag_id == tag_id
                )
            )
            if used:
                raise LibraryError(f"标签仍被 {used} 个对象使用，不能删除")
            row = session.get(TagRow, tag_id)
            if row is None:
                raise LibraryError(f"标签不存在：{tag_id}")
            session.delete(row)
            session.commit()

    # ---------------------------------------------------------------------- books

    def create_book(
        self,
        *,
        code: str,
        title: str,
        authors: Sequence[str] | None = None,
        original_title: str = "",
        publisher: str = "",
        edition: str = "",
        publication_year: str = "",
        isbn_10: str = "",
        isbn_13: str = "",
        volumes: int = 1,
        language: str = "zh-CN",
        subject: Any = None,
        family_id: str = "",
        category_id: str = "",
        template_overrides: dict | None = None,
        cover_image: str = "",
        metadata: dict | None = None,
    ) -> Book:
        code = code.strip()
        title = title.strip()
        if not code or not title:
            raise LibraryError("教材编号和书名不能为空")
        family = self.get_family(family_id) if family_id else self.default_family()
        if family is None:
            raise LibraryError(f"商品系列不存在：{family_id}")
        overrides = template_overrides or {}
        errors = validate_overrides(overrides)
        if errors:
            raise LibraryError("；".join(errors))
        if self.db.get_book_by_code(code):
            raise LibraryError(f"教材编号已存在：{code}")
        book = Book(
            code=code,
            title=title,
            authors=list(authors or []),
            original_title=original_title,
            publisher=publisher,
            edition=edition,
            publication_year=publication_year,
            isbn_10=isbn_10,
            isbn_13=isbn_13,
            volumes=max(1, int(volumes)),
            language=language,
            subject=subject or "other",
            family_id=family.id,
            category_id=category_id or family.default_category_id,
            template_overrides=overrides,
            status="ACTIVE",
            cover_image=cover_image,
            metadata=metadata or {},
        )
        self.db.add_book(book)
        self.audit(
            "book.create",
            "book",
            book.id,
            after={"code": book.code, "title": book.title, "family_id": book.family_id},
            detail=f"新建教材母商品 {title}",
        )
        return book

    def get_book(self, book_id: str) -> Book | None:
        return self.db.get_book(book_id)

    def update_book(self, book_id: str, **fields: Any) -> Book:
        before = self.db.get_book(book_id)
        if before is None:
            raise LibraryError(f"教材不存在：{book_id}")
        if "template_overrides" in fields:
            errors = validate_overrides(fields["template_overrides"] or {})
            if errors:
                raise LibraryError("；".join(errors))
        if "family_id" in fields and fields["family_id"]:
            if self.get_family(fields["family_id"]) is None:
                raise LibraryError(f"商品系列不存在：{fields['family_id']}")
        if "code" in fields:
            other = self.db.get_book_by_code(str(fields["code"]).strip())
            if other and other.id != book_id:
                raise LibraryError(f"教材编号已存在：{fields['code']}")
        self.db.update_book(book_id, **fields)
        after = self.db.get_book(book_id)
        self.audit(
            "book.update",
            "book",
            book_id,
            before=before.model_dump(mode="json"),
            after=after.model_dump(mode="json") if after else {},
            detail="更新教材母商品",
        )
        return after  # type: ignore[return-value]

    def delete_book(self, book_id: str, *, confirm: bool = False) -> None:
        if not confirm:
            raise LibraryError("删除教材需要确认")
        book = self.db.get_book(book_id)
        if book is None:
            raise LibraryError(f"教材不存在：{book_id}")
        with self.session() as session:
            versions = session.scalar(
                select(func.count()).select_from(BookVersionRow).where(
                    BookVersionRow.book_id == book_id
                )
            )
            skus = session.scalar(
                select(func.count()).select_from(skus_t).where(skus_t.c.book_id == book_id)
            )
        if versions or skus:
            raise LibraryError(
                f"教材仍有 {versions or 0} 个版本、{skus or 0} 个 SKU，不能直接删除；请先归档"
            )
        self.assets.unlink_owner("book", book_id)
        self.db.execute("DELETE FROM books WHERE id=?", (book_id,))
        self.audit("book.delete", "book", book_id, before=book.model_dump(mode="json"), detail="删除教材")

    def list_books(
        self,
        *,
        q: str = "",
        author: str = "",
        isbn: str = "",
        publisher: str = "",
        subject: str = "",
        family_id: str = "",
        category_id: str = "",
        status: str = "",
        page: int = 1,
        page_size: int = 50,
        sort: str = "updated",
    ) -> Page[Book]:
        page, page_size = clamp_page(page, page_size)
        conditions = []
        if q.strip():
            like = f"%{q.strip()}%"
            conditions.append(
                or_(
                    books_t.c.title.like(like),
                    books_t.c.code.like(like),
                    books_t.c.original_title.like(like),
                    books_t.c.authors_json.like(like),
                    books_t.c.publisher.like(like),
                    books_t.c.isbn_10.like(like),
                    books_t.c.isbn_13.like(like),
                )
            )
        if author.strip():
            conditions.append(books_t.c.authors_json.like(f"%{author.strip()}%"))
        if isbn.strip():
            like = f"%{isbn.strip()}%"
            conditions.append(or_(books_t.c.isbn_10.like(like), books_t.c.isbn_13.like(like)))
        if publisher.strip():
            conditions.append(books_t.c.publisher.like(f"%{publisher.strip()}%"))
        if subject.strip():
            conditions.append(books_t.c.subject == subject.strip())
        if family_id:
            conditions.append(books_t.c.family_id == family_id)
        if category_id:
            conditions.append(books_t.c.category_id == category_id)
        if status:
            conditions.append(books_t.c.status == status)

        with self.session() as session:
            query = select(books_t)
            count_query = select(func.count()).select_from(books_t)
            for condition in conditions:
                query = query.where(condition)
                count_query = count_query.where(condition)
            total = int(session.scalar(count_query) or 0)
            order = books_t.c.updated_at.desc() if sort == "updated" else books_t.c.title
            rows = session.execute(
                query.order_by(order).offset((page - 1) * page_size).limit(page_size)
            )
            return Page(
                items=[_book_from_row(row) for row in rows],
                total=total,
                page=page,
                page_size=page_size,
            )

    def search_all(self, q: str, *, limit: int = 20) -> dict[str, Any]:
        books = self.list_books(q=q, page=1, page_size=limit)
        skus = self.list_skus(q=q, page=1, page_size=limit)
        return {
            "query": q,
            "books": books.as_dict(),
            "skus": skus.as_dict(),
            "total": books.total + skus.total,
        }

    # ------------------------------------------------------------------- versions

    def create_version(
        self,
        *,
        book_id: str,
        version_label: str,
        notes: str = "",
        product_id: str = "",
        status: BookVersionStatus = BookVersionStatus.DRAFT,
        metadata: dict | None = None,
        created_by: str = "owner",
    ) -> BookVersion:
        version_label = version_label.strip()
        if not version_label:
            raise LibraryError("版本号不能为空")
        if self.db.get_book(book_id) is None:
            raise LibraryError(f"教材不存在：{book_id}")
        if product_id:
            product = self.db.get_product(product_id)
            if product is None or product.book_id != book_id:
                raise LibraryError("版本关联的生产记录不属于该教材")
        with self.session() as session:
            existing = session.scalar(
                select(BookVersionRow).where(
                    BookVersionRow.book_id == book_id,
                    BookVersionRow.version_label == version_label,
                )
            )
            if existing:
                raise LibraryError(f"版本已存在：{version_label}")
            version = BookVersion(
                book_id=book_id,
                version_label=version_label,
                status=status,
                notes=notes,
                product_id=product_id,
                metadata=metadata or {},
                created_by=created_by,
            )
            session.add(
                BookVersionRow(
                    id=version.id,
                    book_id=version.book_id,
                    version_label=version.version_label,
                    status=version.status.value,
                    notes=version.notes,
                    product_id=version.product_id,
                    created_by=version.created_by,
                    metadata_json=dumps(version.metadata),
                    created_at=version.created_at,
                    updated_at=version.updated_at,
                )
            )
            session.commit()
        self.audit(
            "version.create",
            "version",
            version.id,
            after={"book_id": book_id, "version_label": version_label},
            detail=f"新建教材版本 {version_label}",
        )
        return version

    def list_versions(self, book_id: str) -> list[BookVersion]:
        with self.session() as session:
            rows = session.scalars(
                select(BookVersionRow)
                .where(BookVersionRow.book_id == book_id)
                .order_by(BookVersionRow.created_at.desc())
            )
            return [_version_from_row(row) for row in rows]

    def get_version(self, version_id: str) -> BookVersion | None:
        with self.session() as session:
            row = session.get(BookVersionRow, version_id)
            return _version_from_row(row) if row else None

    def update_version(self, version_id: str, **fields: Any) -> BookVersion:
        allowed = {"version_label", "status", "notes", "product_id", "metadata"}
        with self.session() as session:
            row = session.get(BookVersionRow, version_id)
            if row is None:
                raise LibraryError(f"版本不存在：{version_id}")
            for key, value in fields.items():
                if key not in allowed:
                    continue
                if key == "status":
                    row.status = value.value if isinstance(value, BookVersionStatus) else str(value)
                elif key == "metadata":
                    row.metadata_json = dumps(value or {})
                else:
                    setattr(row, key, value)
            row.updated_at = utc_now()
            session.commit()
            return _version_from_row(row)

    def activate_version(self, version_id: str) -> BookVersion:
        with self.session() as session:
            row = session.get(BookVersionRow, version_id)
            if row is None:
                raise LibraryError(f"版本不存在：{version_id}")
            session.execute(
                BookVersionRow.__table__.update()
                .where(
                    BookVersionRow.book_id == row.book_id,
                    BookVersionRow.status == BookVersionStatus.ACTIVE.value,
                )
                .values(status=BookVersionStatus.ARCHIVED.value)
            )
            row.status = BookVersionStatus.ACTIVE.value
            row.updated_at = utc_now()
            session.commit()
            version = _version_from_row(row)
        self.audit(
            "version.activate",
            "version",
            version_id,
            after={"book_id": version.book_id, "version_label": version.version_label},
            detail="切换教材当前版本",
        )
        return version

    def archive_version(self, version_id: str) -> BookVersion:
        return self.update_version(version_id, status=BookVersionStatus.ARCHIVED)

    def delete_version(self, version_id: str) -> None:
        with self.session() as session:
            row = session.get(BookVersionRow, version_id)
            if row is None:
                raise LibraryError(f"版本不存在：{version_id}")
            used = session.scalar(
                select(func.count()).select_from(skus_t).where(skus_t.c.version_id == version_id)
            )
            if used:
                raise LibraryError(f"该版本仍被 {used} 个 SKU 使用，不能删除")
            session.delete(row)
            session.commit()
        self.audit("version.delete", "version", version_id, detail="删除教材版本")

    # ----------------------------------------------------------------------- SKUs

    def _require_book(self, book_id: str) -> Book:
        book = self.db.get_book(book_id)
        if book is None:
            raise LibraryError(f"教材不存在：{book_id}")
        return book

    def _unique_sku_code(self, book: Book, suffix: str = "") -> str:
        base = (book.code or "SKU").strip().upper()
        candidate = f"{base}-{suffix}" if suffix else base
        with self.session() as session:
            existing = {
                row[0]
                for row in session.execute(
                    select(skus_t.c.sku_code).where(skus_t.c.sku_code.like(f"{base}%"))
                )
            }
        if candidate not in existing:
            return candidate
        index = 2
        while f"{candidate}-{index}" in existing:
            index += 1
        return f"{candidate}-{index}"

    def create_single_sku(
        self,
        *,
        book_id: str,
        product_id: str,
        name: str,
        price: Any,
        original_price: Any = None,
        edition: str = "detailed",
        sku_code: str = "",
        enabled: bool = True,
        description: str = "",
        lifecycle: SkuLifecycle = SkuLifecycle.DRAFT,
        version_id: str = "",
        category_id: str = "",
    ) -> SalesSKU:
        book = self._require_book(book_id)
        product = self.db.get_product(product_id)
        if product is None or product.book_id != book_id:
            raise LibraryError("生产记录不存在或不属于该教材")
        if edition not in {"detailed", "quick", "both"}:
            raise LibraryError("交付版本必须是 detailed、quick 或 both")
        price_cents = yuan_to_cents(price)
        if price_cents <= 0:
            raise LibraryError("售价必须大于 0")
        original_cents = (
            yuan_to_cents(original_price) if original_price is not None else price_cents
        )
        if original_cents < price_cents:
            raise LibraryError("原价不能低于售价")
        code = sku_code.strip() or self._unique_sku_code(
            book, (product.volume_code or product.volume_label or "S").strip()
        )
        if self.db.get_sku_by_code(code):
            raise LibraryError(f"SKU 编号已存在：{code}")
        if version_id:
            version = self.get_version(version_id)
            if version is None or version.book_id != book_id:
                raise LibraryError("版本不属于该教材")
        sku = SalesSKU(
            sku_code=code,
            book_id=book_id,
            name=name.strip() or product.title,
            kind="single",
            items=[
                SkuItem(
                    product_id=product_id,
                    volume_label=product.volume_label,
                    edition=edition,
                )
            ],
            price_cents=price_cents,
            original_price_cents=original_cents,
            enabled=enabled,
            description=description,
            status="draft",
            family_id=book.family_id,
            category_id=category_id or book.category_id,
            version_id=version_id,
            lifecycle=lifecycle,
            primary_product_id=product_id,
            production_status=product.status,
        )
        self.db.add_sku(sku)
        self.audit(
            "sku.create",
            "sku",
            sku.id,
            after={
                "sku_code": sku.sku_code,
                "book_id": book_id,
                "price_cents": price_cents,
                "kind": "single",
            },
            detail=f"新建单册 SKU {sku.sku_code}",
        )
        return sku

    def create_bundle(
        self,
        *,
        book_id: str,
        items: Iterable[dict[str, Any] | SkuItem],
        name: str,
        price: Any,
        original_price: Any = None,
        sku_code: str = "",
        enabled: bool = True,
        description: str = "",
        allow_above_parts: bool = False,
        lifecycle: SkuLifecycle = SkuLifecycle.DRAFT,
        version_id: str = "",
        category_id: str = "",
    ) -> SalesSKU:
        book = self._require_book(book_id)
        normalized: list[SkuItem] = []
        for index, raw in enumerate(items):
            item = raw if isinstance(raw, SkuItem) else SkuItem(**raw)
            product = self.db.get_product(item.product_id)
            if product is None or product.book_id != book_id:
                raise LibraryError(f"套装子商品不存在或不属于该教材：{item.product_id}")
            if product.status not in READY_PRODUCTION:
                raise LibraryError(
                    f"子商品《{product.title}》尚未生产完成（{product.status}），不能组成套装"
                )
            if item.edition not in {"detailed", "quick", "both"}:
                raise LibraryError("交付版本必须是 detailed、quick 或 both")
            normalized.append(
                SkuItem(
                    product_id=item.product_id,
                    volume_label=item.volume_label or product.volume_label,
                    edition=item.edition,
                    sort_order=item.sort_order or index,
                )
            )
        if len({item.product_id for item in normalized}) < 2:
            raise LibraryError("套装至少需要两个不同的子商品")
        price_cents = yuan_to_cents(price)
        if price_cents <= 0:
            raise LibraryError("售价必须大于 0")
        original_cents = (
            yuan_to_cents(original_price) if original_price is not None else price_cents
        )
        if original_cents < price_cents:
            raise LibraryError("原价不能低于售价")
        baseline = self._bundle_baseline(book_id, normalized)
        if not allow_above_parts and price_cents > baseline:
            raise LibraryError(
                f"套装售价 {cents_to_yuan(price_cents)} 元高于单册参考价合计 "
                f"{cents_to_yuan(baseline)} 元；如确需上浮请显式允许"
            )
        code = sku_code.strip() or self._unique_sku_code(book, "B")
        if self.db.get_sku_by_code(code):
            raise LibraryError(f"SKU 编号已存在：{code}")
        if version_id:
            version = self.get_version(version_id)
            if version is None or version.book_id != book_id:
                raise LibraryError("版本不属于该教材")
        sku = SalesSKU(
            sku_code=code,
            book_id=book_id,
            name=name.strip(),
            kind="bundle",
            items=normalized,
            price_cents=price_cents,
            original_price_cents=original_cents,
            enabled=enabled,
            description=description,
            status="draft",
            allow_above_parts=allow_above_parts,
            family_id=book.family_id,
            category_id=category_id or book.category_id,
            version_id=version_id,
            lifecycle=lifecycle,
            primary_product_id=normalized[0].product_id,
            production_status="bundle",
        )
        self.db.add_sku(sku)
        self.audit(
            "sku.create",
            "sku",
            sku.id,
            after={
                "sku_code": sku.sku_code,
                "book_id": book_id,
                "price_cents": price_cents,
                "kind": "bundle",
                "items": [item.model_dump() for item in normalized],
            },
            detail=f"新建套装 SKU {sku.sku_code}",
        )
        return sku

    def _bundle_baseline(self, book_id: str, items: list[SkuItem]) -> int:
        total = 0
        with self.session() as session:
            for item in items:
                rows = session.execute(
                    select(skus_t.c.price_cents, skus_t.c.original_price_cents).where(
                        skus_t.c.book_id == book_id,
                        skus_t.c.kind == "single",
                        skus_t.c.items_json.like(f"%{item.product_id}%"),
                    )
                ).all()
                if not rows:
                    raise LibraryError(
                        "套装缺少单册参考价；请先为该子商品建立单册 SKU 或显式允许高于分册"
                    )
                # Prefer the original price, matching the legacy catalogue rule.
                values = [int(row[1] or row[0] or 0) for row in rows]
                total += min(values) if values else 0
        return total

    def get_sku(self, sku_id: str) -> SalesSKU | None:
        return self.db.get_sku(sku_id)

    def update_sku(self, sku_id: str, **fields: Any) -> SalesSKU:
        before = self.db.get_sku(sku_id)
        if before is None:
            raise LibraryError(f"SKU 不存在：{sku_id}")
        if "lifecycle" in fields:
            target = fields["lifecycle"]
            target = target if isinstance(target, SkuLifecycle) else SkuLifecycle(target)
            require_transition(before.lifecycle, target)
            fields["lifecycle"] = target
            fields.update(timestamps_for(target, utc_now()))
        if "delivery_status" in fields and not isinstance(
            fields["delivery_status"], DeliveryStatus
        ):
            fields["delivery_status"] = DeliveryStatus(fields["delivery_status"])
        if "price_cents" in fields or "price" in fields:
            price_cents = (
                int(fields.pop("price_cents"))
                if "price_cents" in fields
                else yuan_to_cents(fields.pop("price"))
            )
            if price_cents <= 0:
                raise LibraryError("售价必须大于 0")
            fields["price_cents"] = price_cents
        if "version_id" in fields and fields["version_id"]:
            version = self.get_version(fields["version_id"])
            if version is None or version.book_id != before.book_id:
                raise LibraryError("版本不属于该教材")
        self.db.update_sku(sku_id, **fields)
        after = self.db.get_sku(sku_id)
        self.audit(
            "sku.update",
            "sku",
            sku_id,
            before=before.model_dump(mode="json"),
            after=after.model_dump(mode="json") if after else {},
            detail="更新 SKU",
        )
        return after  # type: ignore[return-value]

    def transition_lifecycle(self, sku_id: str, target: SkuLifecycle | str) -> SalesSKU:
        sku = self.get_sku(sku_id)
        if sku is None:
            raise LibraryError(f"SKU 不存在：{sku_id}")
        target = target if isinstance(target, SkuLifecycle) else SkuLifecycle(target)
        return self.update_sku(sku_id, lifecycle=target)

    def set_delivery_status(
        self, sku_id: str, status: DeliveryStatus | str, *, note: str = ""
    ) -> SalesSKU:
        return self.update_sku(sku_id, delivery_status=status)

    def set_price(self, sku_id: str, price: Any, original_price: Any = None) -> SalesSKU:
        fields: dict[str, Any] = {"price_cents": yuan_to_cents(price)}
        if original_price is not None:
            fields["original_price_cents"] = yuan_to_cents(original_price)
        return self.update_sku(sku_id, **fields)

    def batch_set_prices(self, prices: dict[str, Any]) -> dict[str, Any]:
        report = {"updated": 0, "failed": 0, "errors": []}
        for sku_id, value in prices.items():
            try:
                self.set_price(sku_id, value)
                report["updated"] += 1
            except (LibraryError, CatalogError, ValueError) as exc:
                report["failed"] += 1
                report["errors"].append({"sku_id": sku_id, "error": str(exc)})
        return report

    def delete_sku(self, sku_id: str, *, confirm: bool = False) -> None:
        if not confirm:
            raise LibraryError("删除 SKU 需要确认")
        sku = self.get_sku(sku_id)
        if sku is None:
            raise LibraryError(f"SKU 不存在：{sku_id}")
        self.assets.unlink_owner("sku", sku_id)
        self.db.delete_sku(sku_id)
        self.audit(
            "sku.delete",
            "sku",
            sku_id,
            before=sku.model_dump(mode="json"),
            detail="删除 SKU（不删除共享资产文件）",
        )

    def list_skus(
        self,
        *,
        q: str = "",
        book_id: str = "",
        lifecycle: str | SkuLifecycle = "",
        delivery_status: str | DeliveryStatus = "",
        production_status: str = "",
        family_id: str = "",
        category_id: str = "",
        version_id: str = "",
        tag_id: str = "",
        enabled: bool | None = None,
        min_price: Any = None,
        max_price: Any = None,
        page: int = 1,
        page_size: int = 50,
        sort: str = "updated",
    ) -> Page[SalesSKU]:
        page, page_size = clamp_page(page, page_size)
        conditions = []
        if q.strip():
            like = f"%{q.strip()}%"
            conditions.append(or_(skus_t.c.sku_code.like(like), skus_t.c.name.like(like)))
        if book_id:
            conditions.append(skus_t.c.book_id == book_id)
        if lifecycle:
            conditions.append(
                skus_t.c.lifecycle == (lifecycle.value if isinstance(lifecycle, SkuLifecycle) else lifecycle)
            )
        if delivery_status:
            conditions.append(
                skus_t.c.delivery_status
                == (
                    delivery_status.value
                    if isinstance(delivery_status, DeliveryStatus)
                    else delivery_status
                )
            )
        if production_status:
            conditions.append(skus_t.c.production_status == production_status)
        if family_id:
            conditions.append(skus_t.c.family_id == family_id)
        if category_id:
            conditions.append(skus_t.c.category_id == category_id)
        if version_id:
            conditions.append(skus_t.c.version_id == version_id)
        if enabled is not None:
            conditions.append(skus_t.c.enabled == int(bool(enabled)))
        if min_price is not None:
            conditions.append(skus_t.c.price_cents >= yuan_to_cents(min_price))
        if max_price is not None:
            conditions.append(skus_t.c.price_cents <= yuan_to_cents(max_price))
        if tag_id:
            conditions.append(
                skus_t.c.id.in_(
                    select(EntityTagRow.entity_id).where(
                        EntityTagRow.entity_type == "sku",
                        EntityTagRow.tag_id == tag_id,
                    )
                )
            )

        with self.session() as session:
            query = select(skus_t)
            count_query = select(func.count()).select_from(skus_t)
            for condition in conditions:
                query = query.where(condition)
                count_query = count_query.where(condition)
            total = int(session.scalar(count_query) or 0)
            order = SKU_SORTS.get(sort, skus_t.c.updated_at)
            if sort in {"updated", "price"}:
                order = order.desc()
            rows = session.execute(
                query.order_by(order).offset((page - 1) * page_size).limit(page_size)
            )
            return Page(
                items=[_sku_from_row(row) for row in rows],
                total=total,
                page=page,
                page_size=page_size,
            )

    def set_sku_version(self, sku_id: str, version_id: str) -> SalesSKU:
        return self.update_sku(sku_id, version_id=version_id)

    # -------------------------------------------------------------------- details

    def resolve_book_template(self, book_id: str) -> ResolvedTemplate:
        book = self._require_book(book_id)
        family = self.get_family(book.family_id) if book.family_id else None
        family = family or self.default_family()
        return resolve_template(family, book.template_overrides)

    def book_detail(self, book_id: str) -> dict[str, Any]:
        book = self._require_book(book_id)
        versions = self.list_versions(book_id)
        skus = self.list_skus(book_id=book_id, page=1, page_size=500)
        template = self.resolve_book_template(book_id)
        return {
            "book": book.model_dump(mode="json"),
            "versions": [version.model_dump(mode="json") for version in versions],
            "skus": [sku.model_dump(mode="json") for sku in skus.items],
            "sku_total": skus.total,
            "template": template.data,
            "template_sources": template.sources,
            "tags": [tag.model_dump(mode="json") for tag in self.tags_for("book", book_id)],
            "assets": [
                asset.model_dump(mode="json") for asset in self.assets.assets_for("book", book_id)
            ],
        }

    def sku_detail(self, sku_id: str) -> dict[str, Any]:
        sku = self.get_sku(sku_id)
        if sku is None:
            raise LibraryError(f"SKU 不存在：{sku_id}")
        book = self.db.get_book(sku.book_id)
        version = self.get_version(sku.version_id) if sku.version_id else None
        products = []
        for item in sku.items:
            product = self.db.get_product(item.product_id)
            if product:
                products.append(product.model_dump(mode="json"))
        return {
            "sku": sku.model_dump(mode="json"),
            "book": book.model_dump(mode="json") if book else None,
            "version": version.model_dump(mode="json") if version else None,
            "products": products,
            "tags": [tag.model_dump(mode="json") for tag in self.tags_for("sku", sku_id)],
            "assets": [
                asset.model_dump(mode="json") for asset in self.assets.assets_for("sku", sku_id)
            ],
            "links": self.assets.links_for_owner("sku", sku_id),
        }

    def family_summary(self, family_id: str) -> dict[str, Any]:
        family = self.get_family(family_id)
        if family is None:
            raise LibraryError(f"商品系列不存在：{family_id}")
        with self.session() as session:
            books = int(
                session.scalar(
                    select(func.count()).select_from(books_t).where(
                        books_t.c.family_id == family_id
                    )
                )
                or 0
            )
            skus = int(
                session.scalar(
                    select(func.count()).select_from(skus_t).where(
                        skus_t.c.family_id == family_id
                    )
                )
                or 0
            )
            book_rows = session.execute(
                select(books_t.c.id, books_t.c.code, books_t.c.title)
                .where(books_t.c.family_id == family_id)
                .order_by(books_t.c.title)
                .limit(50)
            ).all()
        return {
            "family": family.model_dump(mode="json"),
            "book_count": books,
            "sku_count": skus,
            "books": [
                {"id": row[0], "code": row[1], "title": row[2]} for row in book_rows
            ],
        }

    def family_tree(self) -> list[dict[str, Any]]:
        return [self.family_summary(family.id) for family in self.list_families()]

    def apply_family_template(
        self,
        family_id: str,
        *,
        book_ids: Sequence[str] | None = None,
        reset_overrides: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        family = self.get_family(family_id)
        if family is None:
            raise LibraryError(f"商品系列不存在：{family_id}")
        if book_ids is None:
            with self.session() as session:
                book_ids = [
                    row[0]
                    for row in session.execute(
                        select(books_t.c.id).where(books_t.c.family_id == family_id)
                    )
                ]
        preview = []
        for book_id in book_ids:
            book = self.db.get_book(book_id)
            if book is None:
                preview.append({"book_id": book_id, "status": "missing"})
                continue
            preview.append(
                {
                    "book_id": book_id,
                    "code": book.code,
                    "title": book.title,
                    "status": "would_update",
                    "reset_overrides": reset_overrides,
                }
            )
        if dry_run:
            return {"dry_run": True, "family_id": family_id, "items": preview}
        updated = 0
        for item in preview:
            if item["status"] != "would_update":
                continue
            fields: dict[str, Any] = {"family_id": family_id}
            if reset_overrides:
                fields["template_overrides"] = {}
            self.db.update_book(item["book_id"], **fields)
            updated += 1
        self.audit(
            "family.apply",
            "family",
            family_id,
            after={"updated": updated, "reset_overrides": reset_overrides},
            detail="主动将模板应用到已有教材",
        )
        return {"dry_run": False, "family_id": family_id, "updated": updated, "items": preview}

    # ---------------------------------------------------------------------- stats

    def overview(self) -> dict[str, Any]:
        with self.session() as session:
            book_total = int(session.scalar(select(func.count()).select_from(books_t)) or 0)
            sku_total = int(session.scalar(select(func.count()).select_from(skus_t)) or 0)
            family_total = int(session.scalar(select(func.count()).select_from(FamilyRow)) or 0)
            category_total = int(session.scalar(select(func.count()).select_from(CategoryRow)) or 0)
            tag_total = int(session.scalar(select(func.count()).select_from(TagRow)) or 0)
            lifecycle_rows = session.execute(
                select(skus_t.c.lifecycle, func.count()).group_by(skus_t.c.lifecycle)
            ).all()
            delivery_rows = session.execute(
                select(skus_t.c.delivery_status, func.count()).group_by(
                    skus_t.c.delivery_status
                )
            ).all()
            price_total = int(session.scalar(select(func.sum(skus_t.c.price_cents))) or 0)
            books_without_sku = int(
                session.scalar(
                    select(func.count()).select_from(books_t).where(
                        books_t.c.id.not_in(select(skus_t.c.book_id))
                    )
                )
                or 0
            )
        lifecycle = {label.value: 0 for label in SkuLifecycle}
        lifecycle.update({str(key or "DRAFT"): int(value) for key, value in lifecycle_rows})
        delivery = {label.value: 0 for label in DeliveryStatus}
        delivery.update({str(key or "NOT_DELIVERED"): int(value) for key, value in delivery_rows})
        return {
            "books": book_total,
            "skus": sku_total,
            "families": family_total,
            "categories": category_total,
            "tags": tag_total,
            "books_without_sku": books_without_sku,
            "price_total_cents": price_total,
            "price_total_yuan": cents_to_yuan(price_total),
            "lifecycle": lifecycle,
            "delivery": delivery,
            "assets": self.assets.usage_summary(),
        }

    def category_stats(self) -> list[dict[str, Any]]:
        with self.session() as session:
            rows = session.execute(
                select(
                    books_t.c.category_id,
                    func.count(books_t.c.id),
                ).group_by(books_t.c.category_id)
            ).all()
        categories = {category.id: category.name for category in self.list_categories()}
        return [
            {
                "category_id": row[0] or "",
                "name": categories.get(row[0], "未分类"),
                "books": int(row[1]),
            }
            for row in rows
        ]
