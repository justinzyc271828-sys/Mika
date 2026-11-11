"""SQLAlchemy 2.0 mapping for the chapter-26 product library.

The legacy production pipeline keeps using :mod:`core.db` (stdlib ``sqlite3``)
because it is heavily tested and every paid call is checkpointed there.  The
library layer adds its own tables in the same SQLite file through SQLAlchemy so
the catalogue can grow to 10,000+ SKUs, be paginated in SQL, and later move to
PostgreSQL by changing only the engine URL.

Only the library owns the tables declared on :class:`Base`.  The read-only
``books``/``skus`` tables are declared on a separate ``MetaData`` so
``create_all`` never tries to redefine the legacy schema.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class FamilyRow(Base):
    __tablename__ = "product_families"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    is_default: Mapped[int] = mapped_column(Integer, default=0)
    brand_name: Mapped[str] = mapped_column(String(255), default="")
    visual_style_json: Mapped[str] = mapped_column(Text, default="{}")
    image_template_json: Mapped[str] = mapped_column(Text, default="{}")
    detail_template_json: Mapped[str] = mapped_column(Text, default="{}")
    intro_template: Mapped[str] = mapped_column(Text, default="")
    pdf_template: Mapped[str] = mapped_column(String(255), default="")
    watermark_json: Mapped[str] = mapped_column(Text, default="{}")
    pricing_json: Mapped[str] = mapped_column(Text, default="{}")
    default_category_id: Mapped[str] = mapped_column(String(64), default="")
    delivery_rules_json: Mapped[str] = mapped_column(Text, default="{}")
    netdisk_rules_json: Mapped[str] = mapped_column(Text, default="{}")
    version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[str] = mapped_column(String(32), default="")
    updated_at: Mapped[str] = mapped_column(String(32), default="")


class CategoryRow(Base):
    __tablename__ = "categories"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    parent_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(32), default="")
    updated_at: Mapped[str] = mapped_column(String(32), default="")


class TagRow(Base):
    __tablename__ = "tags"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    color: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[str] = mapped_column(String(32), default="")


class EntityTagRow(Base):
    __tablename__ = "entity_tags"
    __table_args__ = (
        UniqueConstraint("entity_type", "entity_id", "tag_id", name="uq_entity_tag"),
        Index("idx_entity_tags_owner", "entity_type", "entity_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(32), index=True)
    entity_id: Mapped[str] = mapped_column(String(64), index=True)
    tag_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[str] = mapped_column(String(32), default="")


class AssetRow(Base):
    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    file_name: Mapped[str] = mapped_column(String(512), default="")
    file_type: Mapped[str] = mapped_column(String(64), default="")
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    local_path: Mapped[str] = mapped_column(Text, default="")
    cloud_path: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[str] = mapped_column(String(32), default="1")
    license_source: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String(32), default="")


class AssetLinkRow(Base):
    __tablename__ = "asset_links"
    __table_args__ = (
        UniqueConstraint(
            "asset_id", "owner_type", "owner_id", "role", name="uq_asset_link"
        ),
        Index("idx_asset_links_owner", "owner_type", "owner_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    asset_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("assets.id", ondelete="RESTRICT"), index=True
    )
    owner_type: Mapped[str] = mapped_column(String(32), default="")
    owner_id: Mapped[str] = mapped_column(String(64), default="")
    role: Mapped[str] = mapped_column(String(32), default="attachment")
    created_at: Mapped[str] = mapped_column(String(32), default="")


class BookVersionRow(Base):
    __tablename__ = "book_versions"
    __table_args__ = (
        UniqueConstraint("book_id", "version_label", name="uq_book_version"),
        Index("idx_book_versions_book", "book_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    book_id: Mapped[str] = mapped_column(String(64), index=True)
    version_label: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default="DRAFT")
    notes: Mapped[str] = mapped_column(Text, default="")
    product_id: Mapped[str] = mapped_column(String(64), default="")
    created_by: Mapped[str] = mapped_column(String(64), default="owner")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String(32), default="")
    updated_at: Mapped[str] = mapped_column(String(32), default="")


class BatchJobRow(Base):
    __tablename__ = "batch_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    succeeded: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    actor: Mapped[str] = mapped_column(String(64), default="owner")
    options_json: Mapped[str] = mapped_column(Text, default="{}")
    preview_json: Mapped[str] = mapped_column(Text, default="[]")
    report_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String(32), default="")
    started_at: Mapped[str] = mapped_column(String(32), default="")
    finished_at: Mapped[str] = mapped_column(String(32), default="")


class BatchItemRow(Base):
    __tablename__ = "batch_job_items"
    __table_args__ = (Index("idx_batch_items_job", "job_id", "status"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("batch_jobs.id", ondelete="CASCADE"), index=True
    )
    entity_type: Mapped[str] = mapped_column(String(32), default="sku")
    entity_id: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    message: Mapped[str] = mapped_column(Text, default="")
    before_json: Mapped[str] = mapped_column(Text, default="{}")
    after_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String(32), default="")


class AuditRow(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("idx_audit_created", "created_at"),
        Index("idx_audit_entity", "entity_type", "entity_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor: Mapped[str] = mapped_column(String(64), default="owner")
    action: Mapped[str] = mapped_column(String(64), default="")
    entity_type: Mapped[str] = mapped_column(String(32), default="")
    entity_id: Mapped[str] = mapped_column(String(64), default="")
    before_json: Mapped[str] = mapped_column(Text, default="{}")
    after_json: Mapped[str] = mapped_column(Text, default="{}")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String(32), default="")


class MigrationRow(Base):
    __tablename__ = "library_migrations"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    applied_at: Mapped[str] = mapped_column(String(32), default="")


# --------------------------------------------------------- read-only legacy views
# A separate MetaData keeps these out of ``Base.metadata.create_all``.
legacy_meta = MetaData()

books_t = Table(
    "books",
    legacy_meta,
    Column("id", String(64), primary_key=True),
    Column("code", String(64)),
    Column("title", String(255)),
    Column("original_title", String(255)),
    Column("authors_json", Text),
    Column("translators_json", Text),
    Column("publisher", String(255)),
    Column("edition", String(255)),
    Column("publication_year", String(32)),
    Column("isbn_10", String(32)),
    Column("isbn_13", String(32)),
    Column("volumes", Integer),
    Column("language", String(32)),
    Column("subject", String(32)),
    Column("cover_image", Text),
    Column("cover_source_url", Text),
    Column("source_license", Text),
    Column("source_verified", Integer),
    Column("verification_status", String(32)),
    Column("family_id", String(64)),
    Column("category_id", String(64)),
    Column("template_overrides_json", Text),
    Column("status", String(32)),
    Column("metadata_json", Text),
    Column("created_at", String(32)),
    Column("updated_at", String(32)),
)

skus_t = Table(
    "skus",
    legacy_meta,
    Column("id", String(64), primary_key=True),
    Column("sku_code", String(64)),
    Column("book_id", String(64)),
    Column("name", String(255)),
    Column("kind", String(32)),
    Column("items_json", Text),
    Column("price_cents", Integer),
    Column("original_price_cents", Integer),
    Column("enabled", Integer),
    Column("description", Text),
    Column("image_dir", Text),
    Column("status", String(32)),
    Column("allow_above_parts", Integer),
    Column("family_id", String(64)),
    Column("category_id", String(64)),
    Column("version_id", String(64)),
    Column("lifecycle", String(32)),
    Column("delivery_status", String(32)),
    Column("primary_product_id", String(64)),
    Column("production_status", String(32)),
    Column("published_at", String(32)),
    Column("paused_at", String(32)),
    Column("archived_at", String(32)),
    Column("created_at", String(32)),
    Column("updated_at", String(32)),
)

entity_tags_t = EntityTagRow.__table__
