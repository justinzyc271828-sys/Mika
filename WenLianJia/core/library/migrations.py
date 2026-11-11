"""Engine creation and the versioned library migration ledger."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session

from core.library.orm import Base, MigrationRow
from models.schemas import utc_now

SCHEMA_VERSION = 1
_MIGRATIONS = {
    1: "product library: families, categories, tags, assets, versions, batches, audit",
}

_LEGACY_COLUMNS = {
    "books": {
        "family_id": "TEXT DEFAULT ''",
        "category_id": "TEXT DEFAULT ''",
        "template_overrides_json": "TEXT DEFAULT '{}'",
        "status": "TEXT DEFAULT 'ACTIVE'",
    },
    "skus": {
        "family_id": "TEXT DEFAULT ''",
        "category_id": "TEXT DEFAULT ''",
        "version_id": "TEXT DEFAULT ''",
        "lifecycle": "TEXT DEFAULT 'DRAFT'",
        "delivery_status": "TEXT DEFAULT 'NOT_DELIVERED'",
        "primary_product_id": "TEXT DEFAULT ''",
        "production_status": "TEXT DEFAULT ''",
        "published_at": "TEXT DEFAULT ''",
        "paused_at": "TEXT DEFAULT ''",
        "archived_at": "TEXT DEFAULT ''",
    },
}


def create_library_engine(db_path: Path | str) -> Engine:
    """Create a portable SQLAlchemy engine for the shared SQLite file.

    ``sqlite:///`` is the only SQLite-specific part.  A future PostgreSQL
    deployment passes its own URL to :func:`create_engine`; the ORM, the
    paginated queries and the migration ledger do not change.
    """

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path.as_posix()}",
        future=True,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection, _record):  # pragma: no cover - driver hook
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def ensure_schema(engine: Engine) -> None:
    """Create library tables, add legacy columns, record the schema version."""

    _ensure_legacy_columns(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        applied = session.get(MigrationRow, SCHEMA_VERSION)
        if applied is None:
            session.add(
                MigrationRow(
                    version=SCHEMA_VERSION,
                    name=_MIGRATIONS[SCHEMA_VERSION],
                    applied_at=utc_now(),
                )
            )
            session.commit()


def _ensure_legacy_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    for table, additions in _LEGACY_COLUMNS.items():
        if table not in tables:
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        for name, definition in additions.items():
            if name in existing:
                continue
            with engine.begin() as connection:
                connection.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                )


def migration_report(engine: Engine) -> list[dict[str, Any]]:
    with Session(engine) as session:
        rows = session.query(MigrationRow).order_by(MigrationRow.version).all()
        return [
            {
                "version": row.version,
                "name": row.name,
                "applied_at": row.applied_at,
            }
            for row in rows
        ]


def schema_version(engine: Engine) -> int:
    with Session(engine) as session:
        row = session.query(MigrationRow).order_by(MigrationRow.version.desc()).first()
        return int(row.version) if row else 0
