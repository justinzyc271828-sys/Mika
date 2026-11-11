"""SQLite persistence.

The database is the single source of truth for resumable production.  Pipeline
stages write durable checkpoints here, so a crash never loses a paid model call
and never silently reuses a stale answer.  Connections are thread-local because
the GUI reads while a worker thread produces.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator

from models.schemas import (
    ApiUsage,
    Book,
    Chapter,
    DeliveryStatus,
    Exercise,
    ExerciseStatus,
    ExerciseType,
    GeneratedFile,
    Job,
    JobStatus,
    PdfPage,
    Product,
    SalesSKU,
    ShareRecord,
    SkuItem,
    SkuLifecycle,
    Solution,
    SolutionContent,
    StageStatus,
    Subject,
    UploadRecord,
    Verification,
    VerificationVerdict,
    stable_hash,
    utc_now,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id TEXT PRIMARY KEY,
    book_id TEXT DEFAULT '',
    volume_label TEXT DEFAULT '',
    volume_number INTEGER DEFAULT 0,
    volume_code TEXT DEFAULT '',
    sku TEXT DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT 'other',
    source_title TEXT DEFAULT '',
    source_author TEXT DEFAULT '',
    source_edition TEXT DEFAULT '',
    source_path TEXT DEFAULT '',
    source_sha256 TEXT DEFAULT '',
    total_pages INTEGER DEFAULT 0,
    chapter_count INTEGER DEFAULT 0,
    exercise_count INTEGER DEFAULT 0,
    solved_count INTEGER DEFAULT 0,
    verified_count INTEGER DEFAULT 0,
    review_required INTEGER DEFAULT 0,
    status TEXT DEFAULT 'draft',
    version TEXT DEFAULT '1.0',
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_products_sha ON products(source_sha256);
CREATE INDEX IF NOT EXISTS idx_products_status ON products(status);
CREATE INDEX IF NOT EXISTS idx_products_book ON products(book_id, volume_number);

CREATE TABLE IF NOT EXISTS books (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    original_title TEXT DEFAULT '',
    authors_json TEXT DEFAULT '[]',
    translators_json TEXT DEFAULT '[]',
    publisher TEXT DEFAULT '',
    edition TEXT DEFAULT '',
    publication_year TEXT DEFAULT '',
    isbn_10 TEXT DEFAULT '',
    isbn_13 TEXT DEFAULT '',
    volumes INTEGER DEFAULT 1,
    language TEXT DEFAULT 'zh-CN',
    subject TEXT DEFAULT 'other',
    cover_image TEXT DEFAULT '',
    cover_source_url TEXT DEFAULT '',
    source_license TEXT DEFAULT '',
    source_verified INTEGER DEFAULT 0,
    verification_status TEXT DEFAULT 'NEEDS_REVIEW',
    family_id TEXT DEFAULT '',
    category_id TEXT DEFAULT '',
    template_overrides_json TEXT DEFAULT '{}',
    status TEXT DEFAULT 'ACTIVE',
    metadata_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_books_title ON books(title);
CREATE INDEX IF NOT EXISTS idx_books_isbn ON books(isbn_13, isbn_10);
CREATE INDEX IF NOT EXISTS idx_books_family ON books(family_id);
CREATE INDEX IF NOT EXISTS idx_books_category ON books(category_id);

CREATE TABLE IF NOT EXISTS skus (
    id TEXT PRIMARY KEY,
    sku_code TEXT NOT NULL UNIQUE,
    book_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'single',
    items_json TEXT DEFAULT '[]',
    price_cents INTEGER DEFAULT 0,
    original_price_cents INTEGER DEFAULT 0,
    enabled INTEGER DEFAULT 1,
    description TEXT DEFAULT '',
    image_dir TEXT DEFAULT '',
    status TEXT DEFAULT 'draft',
    allow_above_parts INTEGER DEFAULT 0,
    family_id TEXT DEFAULT '',
    category_id TEXT DEFAULT '',
    version_id TEXT DEFAULT '',
    lifecycle TEXT DEFAULT 'DRAFT',
    delivery_status TEXT DEFAULT 'NOT_DELIVERED',
    primary_product_id TEXT DEFAULT '',
    production_status TEXT DEFAULT '',
    published_at TEXT DEFAULT '',
    paused_at TEXT DEFAULT '',
    archived_at TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_skus_book ON skus(book_id, enabled);
CREATE INDEX IF NOT EXISTS idx_skus_lifecycle ON skus(lifecycle);
CREATE INDEX IF NOT EXISTS idx_skus_production ON skus(production_status);
CREATE INDEX IF NOT EXISTS idx_skus_delivery ON skus(delivery_status);
CREATE INDEX IF NOT EXISTS idx_skus_price ON skus(price_cents);

CREATE TABLE IF NOT EXISTS book_cache (
    cache_key TEXT PRIMARY KEY,
    provider TEXT DEFAULT '',
    payload_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    expires_at TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    current_stage TEXT DEFAULT 'queued',
    stage_status_json TEXT DEFAULT '{}',
    progress REAL DEFAULT 0,
    message TEXT DEFAULT '',
    error TEXT DEFAULT '',
    options_json TEXT DEFAULT '{}',
    version INTEGER DEFAULT 1,
    resume_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT DEFAULT '',
    finished_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_jobs_product ON jobs(product_id, created_at);

CREATE TABLE IF NOT EXISTS pdf_pages (
    product_id TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    width REAL DEFAULT 0,
    height REAL DEFAULT 0,
    text TEXT DEFAULT '',
    blocks_json TEXT DEFAULT '[]',
    is_scanned INTEGER DEFAULT 0,
    ocr_confidence REAL DEFAULT 0,
    image_path TEXT DEFAULT '',
    has_images INTEGER DEFAULT 0,
    has_formula INTEGER DEFAULT 0,
    PRIMARY KEY (product_id, page_number)
);

CREATE TABLE IF NOT EXISTS chapters (
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    number INTEGER DEFAULT 0,
    title TEXT DEFAULT '',
    start_page INTEGER DEFAULT 1,
    end_page INTEGER DEFAULT 1,
    level INTEGER DEFAULT 1,
    parent_id TEXT DEFAULT '',
    exercise_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chapters_product ON chapters(product_id, number);

CREATE TABLE IF NOT EXISTS exercises (
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    chapter_id TEXT NOT NULL,
    number TEXT DEFAULT '',
    global_number INTEGER DEFAULT 0,
    label TEXT DEFAULT '',
    page_start INTEGER DEFAULT 1,
    page_end INTEGER DEFAULT 1,
    text TEXT DEFAULT '',
    latex TEXT DEFAULT '',
    images_json TEXT DEFAULT '[]',
    exercise_type TEXT DEFAULT 'unknown',
    confidence REAL DEFAULT 0.5,
    status TEXT DEFAULT 'pending',
    source_hash TEXT DEFAULT '',
    raw_json TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_exercises_product ON exercises(product_id, global_number);
CREATE INDEX IF NOT EXISTS idx_exercises_status ON exercises(product_id, status);

CREATE TABLE IF NOT EXISTS solutions (
    id TEXT PRIMARY KEY,
    exercise_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    model TEXT DEFAULT '',
    prompt_version TEXT DEFAULT '1.0',
    content_json TEXT DEFAULT '{}',
    status TEXT DEFAULT 'ai_draft',
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    cost REAL DEFAULT 0,
    cost_known INTEGER DEFAULT 0,
    latency_ms INTEGER DEFAULT 0,
    content_hash TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_solutions_exercise ON solutions(exercise_id, created_at);

CREATE TABLE IF NOT EXISTS verifications (
    id TEXT PRIMARY KEY,
    exercise_id TEXT NOT NULL,
    solution_id TEXT NOT NULL,
    method TEXT DEFAULT 'independent_model',
    reviewer_model TEXT DEFAULT '',
    verdict TEXT DEFAULT 'inconclusive',
    details_json TEXT DEFAULT '{}',
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verifications_exercise ON verifications(exercise_id, created_at);

CREATE TABLE IF NOT EXISTS generated_files (
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT DEFAULT '',
    size INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    metadata_json TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_files_product ON generated_files(product_id, kind);

CREATE TABLE IF NOT EXISTS uploads (
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    local_path TEXT NOT NULL,
    remote_path TEXT NOT NULL,
    size INTEGER DEFAULT 0,
    sha256 TEXT DEFAULT '',
    remote_md5 TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    attempts INTEGER DEFAULT 0,
    error TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uploads_product ON uploads(product_id, status);

CREATE TABLE IF NOT EXISTS shares (
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    url TEXT DEFAULT '',
    password TEXT DEFAULT '',
    period TEXT DEFAULT 'permanent',
    status TEXT DEFAULT 'created',
    verified INTEGER DEFAULT 0,
    verified_at TEXT DEFAULT '',
    details_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shares_product ON shares(product_id, created_at);

CREATE TABLE IF NOT EXISTS api_usage (
    id TEXT PRIMARY KEY,
    product_id TEXT DEFAULT '',
    exercise_id TEXT DEFAULT '',
    kind TEXT DEFAULT 'solve',
    model TEXT DEFAULT '',
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cached_tokens INTEGER DEFAULT 0,
    estimated_cost REAL DEFAULT 0,
    cost_known INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_product ON api_usage(product_id, created_at);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT DEFAULT 'INFO',
    scope TEXT DEFAULT '',
    message TEXT DEFAULT '',
    details_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_created ON logs(created_at);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value_json TEXT DEFAULT 'null',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS perf_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT DEFAULT '',
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
"""

_PRODUCT_COLUMNS = {
    "book_id",
    "volume_label",
    "volume_number",
    "volume_code",
    "sku",
    "title",
    "subject",
    "source_title",
    "source_author",
    "source_edition",
    "source_path",
    "source_sha256",
    "total_pages",
    "chapter_count",
    "exercise_count",
    "solved_count",
    "verified_count",
    "review_required",
    "status",
    "version",
    "notes",
}
_JOB_COLUMNS = {
    "status",
    "current_stage",
    "stage_status",
    "progress",
    "message",
    "error",
    "options",
    "version",
    "resume_count",
    "started_at",
    "finished_at",
}
_EXERCISE_COLUMNS = {
    "number",
    "global_number",
    "label",
    "page_start",
    "page_end",
    "text",
    "latex",
    "images",
    "exercise_type",
    "confidence",
    "status",
    "source_hash",
    "raw",
}


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _enum_or(enum_cls: type[Enum], value: Any, default: Any) -> Any:
    try:
        return enum_cls(value)
    except (TypeError, ValueError):
        return default


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._connections: set[sqlite3.Connection] = set()
        self.init_schema()

    # ---------------------------------------------------------------- plumbing

    def _connect(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(
                self.path, timeout=30, check_same_thread=False, isolation_level=None
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("PRAGMA foreign_keys=ON")
            self._local.connection = connection
            with self._write_lock:
                self._connections.add(connection)
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        with self._write_lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._write_lock:
            return self._connect().execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self._connect().execute(sql, tuple(params)).fetchall())

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self._connect().execute(sql, tuple(params)).fetchone()

    def init_schema(self) -> None:
        with self._write_lock:
            connection = self._connect()
            connection.executescript(SCHEMA)
            self._migrate(connection)

    def _migrate(self, connection: sqlite3.Connection) -> None:
        """Add columns introduced after the first shipped database.

        SQLite's ``CREATE TABLE IF NOT EXISTS`` cannot add columns to an
        existing database.  Every migration is additive and idempotent so an
        owner can upgrade without losing paid work.
        """

        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(products)")
        }
        additions = {
            "book_id": "TEXT DEFAULT ''",
            "volume_label": "TEXT DEFAULT ''",
            "volume_number": "INTEGER DEFAULT 0",
            "volume_code": "TEXT DEFAULT ''",
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE products ADD COLUMN {name} {definition}"
                )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_products_book ON products(book_id, volume_number)"
        )

        # Chapter 26 library columns.  They are added in place so an existing
        # 1.0 database keeps every paid production record.
        self._add_columns(
            connection,
            "books",
            {
                "family_id": "TEXT DEFAULT ''",
                "category_id": "TEXT DEFAULT ''",
                "template_overrides_json": "TEXT DEFAULT '{}'",
                "status": "TEXT DEFAULT 'ACTIVE'",
            },
        )
        self._add_columns(
            connection,
            "skus",
            {
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
        )
        for statement in (
            "CREATE INDEX IF NOT EXISTS idx_books_family ON books(family_id)",
            "CREATE INDEX IF NOT EXISTS idx_books_category ON books(category_id)",
            "CREATE INDEX IF NOT EXISTS idx_skus_lifecycle ON skus(lifecycle)",
            "CREATE INDEX IF NOT EXISTS idx_skus_production ON skus(production_status)",
            "CREATE INDEX IF NOT EXISTS idx_skus_delivery ON skus(delivery_status)",
            "CREATE INDEX IF NOT EXISTS idx_skus_price ON skus(price_cents)",
        ):
            connection.execute(statement)

        # One-time backfill of the new lifecycle columns from the legacy
        # delivery status.  The marker row makes this run exactly once, so a
        # later deliberate DRAFT state is never rewritten on startup.
        marker = connection.execute(
            "SELECT value_json FROM settings WHERE key='library_lifecycle_backfill'"
        ).fetchone()
        if marker is None:
            connection.execute(
                """UPDATE skus SET lifecycle = CASE status
                     WHEN 'needs_review' THEN 'REVIEW_REQUIRED'
                     WHEN 'ready' THEN 'READY'
                     WHEN 'packaged' THEN 'READY'
                     WHEN 'uploaded' THEN 'PUBLISHED'
                     WHEN 'delivered' THEN 'PUBLISHED'
                     ELSE 'DRAFT' END
                   WHERE lifecycle IS NULL OR lifecycle = '' OR lifecycle = 'DRAFT'"""
            )
            connection.execute(
                """UPDATE skus SET delivery_status = CASE status
                     WHEN 'uploaded' THEN 'UPLOADED'
                     WHEN 'delivered' THEN 'VERIFIED'
                     WHEN 'needs_review' THEN 'NEEDS_MANUAL'
                     ELSE 'NOT_DELIVERED' END
                   WHERE delivery_status IS NULL OR delivery_status = ''"""
            )
            connection.execute(
                "INSERT OR REPLACE INTO settings (key, value_json, updated_at) VALUES (?,?,?)",
                ("library_lifecycle_backfill", "2", utc_now()),
            )

    @staticmethod
    def _add_columns(
        connection: sqlite3.Connection, table: str, additions: dict[str, str]
    ) -> None:
        columns = {
            row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                )

    def close_all(self) -> None:
        """Close every thread-local connection (used before a database restore)."""

        with self._write_lock:
            for connection in list(self._connections):
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
            self._connections.clear()
        self._local.connection = None

    def close(self) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            self._local.connection = None
            with self._write_lock:
                self._connections.discard(connection)

    # ----------------------------------------------------------------- products

    def add_product(self, product: Product) -> Product:
        self.execute(
            """INSERT OR REPLACE INTO products
            (id, book_id, volume_label, volume_number, volume_code, sku, title,
             subject, source_title, source_author, source_edition,
             source_path, source_sha256, total_pages, chapter_count, exercise_count,
             solved_count, verified_count, review_required, status, version, notes,
             created_at, updated_at, metadata_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                product.id,
                product.book_id,
                product.volume_label,
                product.volume_number,
                product.volume_code,
                product.sku,
                product.title,
                product.subject.value,
                product.source_title,
                product.source_author,
                product.source_edition,
                product.source_path,
                product.source_sha256,
                product.total_pages,
                product.chapter_count,
                product.exercise_count,
                product.solved_count,
                product.verified_count,
                product.review_required,
                product.status,
                product.version,
                product.notes,
                product.created_at,
                product.updated_at,
                _dumps(product.metadata),
            ),
        )
        return product

    def update_product(self, product_id: str, **fields: Any) -> None:
        updates = {key: value for key, value in fields.items() if key in _PRODUCT_COLUMNS}
        if not updates:
            return
        if "subject" in updates and isinstance(updates["subject"], Subject):
            updates["subject"] = updates["subject"].value
        updates["updated_at"] = utc_now()
        assignments = ", ".join(f"{key}=?" for key in updates)
        self.execute(
            f"UPDATE products SET {assignments} WHERE id=?",
            (*updates.values(), product_id),
        )

    def get_product(self, product_id: str) -> Product | None:
        row = self.query_one("SELECT * FROM products WHERE id=?", (product_id,))
        return self._row_product(row) if row else None

    def get_product_by_sha(self, sha: str) -> Product | None:
        row = self.query_one(
            "SELECT * FROM products WHERE source_sha256=? ORDER BY created_at DESC LIMIT 1", (sha,)
        )
        return self._row_product(row) if row else None

    def list_products(self, status: str | None = None, search: str = "") -> list[Product]:
        clauses = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if search:
            clauses.append("(title LIKE ? OR sku LIKE ? OR source_title LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like, like])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.query(
            f"SELECT * FROM products {where} ORDER BY created_at DESC", params
        )
        return [self._row_product(row) for row in rows]

    def delete_product_data(self, product_id: str) -> None:
        """Remove derived rows. Used only by explicit reset actions."""

        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM verifications WHERE exercise_id IN "
                "(SELECT id FROM exercises WHERE product_id=?)",
                (product_id,),
            )
            for table in (
                "solutions",
                "exercises",
                "chapters",
                "pdf_pages",
                "generated_files",
                "uploads",
                "shares",
                "api_usage",
            ):
                connection.execute(f"DELETE FROM {table} WHERE product_id=?", (product_id,))
            connection.execute("DELETE FROM jobs WHERE product_id=?", (product_id,))

    def refresh_product_counts(self, product_id: str) -> Product | None:
        product = self.get_product(product_id)
        if not product:
            return None
        chapters = self.query_one(
            "SELECT COUNT(*) AS n FROM chapters WHERE product_id=?", (product_id,)
        )
        counts = self.exercise_status_counts(product_id)
        total = sum(counts.values())
        verified = counts.get(ExerciseStatus.VERIFIED.value, 0) + counts.get(
            ExerciseStatus.APPROVED.value, 0
        )
        review = counts.get(ExerciseStatus.NEEDS_REVIEW.value, 0)
        solved = (
            verified
            + review
            + counts.get(ExerciseStatus.AI_DRAFT.value, 0)
            + counts.get(ExerciseStatus.SOLVING.value, 0)
        )
        self.update_product(
            product_id,
            chapter_count=int(chapters["n"] if chapters else 0),
            exercise_count=total,
            all_count=total,
            solved_count=solved,
            verified_count=verified,
            review_required=review,
        )
        return self.get_product(product_id)

    @staticmethod
    def _row_product(row: sqlite3.Row) -> Product:
        try:
            subject = Subject(row["subject"])
        except ValueError:
            subject = Subject.OTHER
        return Product(
            id=row["id"],
            book_id=row["book_id"] or "",
            volume_label=row["volume_label"] or "",
            volume_number=int(row["volume_number"] or 0),
            volume_code=row["volume_code"] or "",
            sku=row["sku"],
            title=row["title"],
            subject=subject,
            source_title=row["source_title"],
            source_author=row["source_author"],
            source_edition=row["source_edition"],
            source_path=row["source_path"],
            source_sha256=row["source_sha256"],
            total_pages=row["total_pages"],
            chapter_count=row["chapter_count"],
            exercise_count=row["exercise_count"],
            solved_count=row["solved_count"],
            verified_count=row["verified_count"],
            review_required=row["review_required"],
            status=row["status"],
            version=row["version"],
            notes=row["notes"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata=_loads(row["metadata_json"], {}),
        )

    def list_productions(self, book_id: str) -> list[Product]:
        rows = self.query(
            "SELECT * FROM products WHERE book_id=? ORDER BY volume_number, created_at",
            (book_id,),
        )
        return [self._row_product(row) for row in rows]

    # ------------------------------------------------------------------- books

    def add_book(self, book: Book) -> Book:
        self.execute(
            """INSERT OR REPLACE INTO books
            (id, code, title, original_title, authors_json, translators_json,
             publisher, edition, publication_year, isbn_10, isbn_13, volumes,
             language, subject, cover_image, cover_source_url, source_license,
             source_verified, verification_status, family_id, category_id,
             template_overrides_json, status, metadata_json, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                book.id,
                book.code,
                book.title,
                book.original_title,
                _dumps(book.authors),
                _dumps(book.translators),
                book.publisher,
                book.edition,
                book.publication_year,
                book.isbn_10,
                book.isbn_13,
                book.volumes,
                book.language,
                book.subject.value,
                book.cover_image,
                book.cover_source_url,
                book.source_license,
                int(book.source_verified),
                book.verification_status,
                book.family_id,
                book.category_id,
                _dumps(book.template_overrides),
                book.status,
                _dumps(book.metadata),
                book.created_at,
                book.updated_at,
            ),
        )
        return book

    def update_book(self, book_id: str, **fields: Any) -> None:
        columns = {
            "code",
            "title",
            "original_title",
            "publisher",
            "edition",
            "publication_year",
            "isbn_10",
            "isbn_13",
            "volumes",
            "language",
            "cover_image",
            "cover_source_url",
            "source_license",
            "source_verified",
            "verification_status",
            "family_id",
            "category_id",
            "status",
            "metadata",
        }
        updates: dict[str, Any] = {}
        for key, value in fields.items():
            if key in ("authors", "translators"):
                updates[f"{key}_json"] = _dumps(value)
            elif key == "subject" and isinstance(value, Subject):
                updates["subject"] = value.value
            elif key == "metadata":
                updates["metadata_json"] = _dumps(value)
            elif key == "template_overrides":
                updates["template_overrides_json"] = _dumps(value)
            elif key == "source_verified":
                updates["source_verified"] = int(bool(value))
            elif key in columns:
                updates[key] = value
        if not updates:
            return
        updates["updated_at"] = utc_now()
        assignments = ", ".join(f"{key}=?" for key in updates)
        self.execute(
            f"UPDATE books SET {assignments} WHERE id=?",
            (*updates.values(), book_id),
        )

    def get_book(self, book_id: str) -> Book | None:
        row = self.query_one("SELECT * FROM books WHERE id=?", (book_id,))
        return self._row_book(row) if row else None

    def get_book_by_code(self, code: str) -> Book | None:
        row = self.query_one("SELECT * FROM books WHERE code=?", (code,))
        return self._row_book(row) if row else None

    def get_book_by_isbn(self, isbn: str) -> Book | None:
        cleaned = "".join(ch for ch in isbn if ch.isdigit() or ch in "Xx")
        row = self.query_one(
            "SELECT * FROM books WHERE isbn_13=? OR isbn_10=? ORDER BY created_at DESC LIMIT 1",
            (cleaned, cleaned),
        )
        return self._row_book(row) if row else None

    def list_books(self) -> list[Book]:
        rows = self.query("SELECT * FROM books ORDER BY created_at DESC")
        return [self._row_book(row) for row in rows]

    @staticmethod
    def _row_book(row: sqlite3.Row) -> Book:
        try:
            subject = Subject(row["subject"])
        except ValueError:
            subject = Subject.OTHER
        return Book(
            id=row["id"],
            code=row["code"],
            title=row["title"],
            original_title=row["original_title"],
            authors=_loads(row["authors_json"], []),
            translators=_loads(row["translators_json"], []),
            publisher=row["publisher"],
            edition=row["edition"],
            publication_year=row["publication_year"],
            isbn_10=row["isbn_10"],
            isbn_13=row["isbn_13"],
            volumes=int(row["volumes"] or 1),
            language=row["language"],
            subject=subject,
            cover_image=row["cover_image"],
            cover_source_url=row["cover_source_url"],
            source_license=row["source_license"],
            source_verified=bool(row["source_verified"]),
            verification_status=row["verification_status"],
            family_id=row["family_id"] or "",
            category_id=row["category_id"] or "",
            template_overrides=_loads(row["template_overrides_json"], {}),
            status=row["status"] or "ACTIVE",
            metadata=_loads(row["metadata_json"], {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # -------------------------------------------------------------------- skus

    def add_sku(self, sku: SalesSKU) -> SalesSKU:
        self.execute(
            """INSERT OR REPLACE INTO skus
            (id, sku_code, book_id, name, kind, items_json, price_cents,
             original_price_cents, enabled, description, image_dir, status,
             allow_above_parts, family_id, category_id, version_id, lifecycle,
             delivery_status, primary_product_id, production_status,
             published_at, paused_at, archived_at, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                sku.id,
                sku.sku_code,
                sku.book_id,
                sku.name,
                sku.kind,
                _dumps([item.model_dump(mode="json") for item in sku.items]),
                int(sku.price_cents),
                int(sku.original_price_cents),
                int(sku.enabled),
                sku.description,
                sku.image_dir,
                sku.status,
                int(sku.allow_above_parts),
                sku.family_id,
                sku.category_id,
                sku.version_id,
                sku.lifecycle.value if isinstance(sku.lifecycle, SkuLifecycle) else sku.lifecycle,
                sku.delivery_status.value
                if isinstance(sku.delivery_status, DeliveryStatus)
                else sku.delivery_status,
                sku.primary_product_id,
                sku.production_status,
                sku.published_at,
                sku.paused_at,
                sku.archived_at,
                sku.created_at,
                sku.updated_at,
            ),
        )
        return sku

    def update_sku(self, sku_id: str, **fields: Any) -> None:
        columns = {
            "sku_code",
            "book_id",
            "name",
            "kind",
            "price_cents",
            "original_price_cents",
            "enabled",
            "description",
            "image_dir",
            "status",
            "allow_above_parts",
            "family_id",
            "category_id",
            "version_id",
            "lifecycle",
            "delivery_status",
            "primary_product_id",
            "production_status",
            "published_at",
            "paused_at",
            "archived_at",
        }
        updates: dict[str, Any] = {}
        for key, value in fields.items():
            if key == "items":
                updates["items_json"] = _dumps(
                    [
                        item.model_dump(mode="json") if isinstance(item, SkuItem) else item
                        for item in value
                    ]
                )
            elif key == "enabled":
                updates["enabled"] = int(bool(value))
            elif key == "allow_above_parts":
                updates["allow_above_parts"] = int(bool(value))
            elif key in ("lifecycle", "delivery_status") and isinstance(value, Enum):
                updates[key] = value.value
            elif key in columns:
                updates[key] = value
        if not updates:
            return
        updates["updated_at"] = utc_now()
        assignments = ", ".join(f"{key}=?" for key in updates)
        self.execute(
            f"UPDATE skus SET {assignments} WHERE id=?",
            (*updates.values(), sku_id),
        )

    def get_sku(self, sku_id: str) -> SalesSKU | None:
        row = self.query_one("SELECT * FROM skus WHERE id=?", (sku_id,))
        return self._row_sku(row) if row else None

    def get_sku_by_code(self, code: str) -> SalesSKU | None:
        row = self.query_one("SELECT * FROM skus WHERE sku_code=?", (code,))
        return self._row_sku(row) if row else None

    def list_skus(
        self, book_id: str | None = None, enabled: bool | None = None
    ) -> list[SalesSKU]:
        clauses: list[str] = []
        params: list[Any] = []
        if book_id:
            clauses.append("book_id=?")
            params.append(book_id)
        if enabled is not None:
            clauses.append("enabled=?")
            params.append(int(enabled))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.query(
            f"SELECT * FROM skus {where} ORDER BY created_at DESC", params
        )
        return [self._row_sku(row) for row in rows]

    def delete_sku(self, sku_id: str) -> None:
        self.execute("DELETE FROM skus WHERE id=?", (sku_id,))

    @staticmethod
    def _row_sku(row: sqlite3.Row) -> SalesSKU:
        raw_items = _loads(row["items_json"], [])
        items = []
        for item in raw_items if isinstance(raw_items, list) else []:
            try:
                items.append(SkuItem.model_validate(item))
            except ValueError:
                continue
        return SalesSKU(
            id=row["id"],
            sku_code=row["sku_code"],
            book_id=row["book_id"],
            name=row["name"],
            kind=row["kind"],
            items=items,
            price_cents=int(row["price_cents"] or 0),
            original_price_cents=int(row["original_price_cents"] or 0),
            enabled=bool(row["enabled"]),
            description=row["description"],
            image_dir=row["image_dir"],
            status=row["status"],
            allow_above_parts=bool(row["allow_above_parts"]),
            family_id=row["family_id"] or "",
            category_id=row["category_id"] or "",
            version_id=row["version_id"] or "",
            lifecycle=_enum_or(SkuLifecycle, row["lifecycle"], SkuLifecycle.DRAFT),
            delivery_status=_enum_or(
                DeliveryStatus, row["delivery_status"], DeliveryStatus.NOT_DELIVERED
            ),
            primary_product_id=row["primary_product_id"] or "",
            production_status=row["production_status"] or "",
            published_at=row["published_at"] or "",
            paused_at=row["paused_at"] or "",
            archived_at=row["archived_at"] or "",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # -------------------------------------------------------------- book cache

    def cache_get(self, key: str) -> Any | None:
        row = self.query_one("SELECT * FROM book_cache WHERE cache_key=?", (key,))
        if not row:
            return None
        expires = row["expires_at"] or ""
        if expires and expires < utc_now():
            return None
        return _loads(row["payload_json"], None)

    def cache_set(
        self, key: str, provider: str, payload: Any, *, days: int = 30
    ) -> None:
        from datetime import datetime, timedelta, timezone

        expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(
            timespec="seconds"
        )
        self.execute(
            """INSERT OR REPLACE INTO book_cache
            (cache_key, provider, payload_json, created_at, expires_at)
            VALUES (?,?,?,?,?)""",
            (key, provider, _dumps(payload), utc_now(), expires),
        )

    # --------------------------------------------------------------------- jobs

    def create_job(self, job: Job) -> Job:
        self.execute(
            """INSERT INTO jobs (id, product_id, status, current_stage, stage_status_json,
            progress, message, error, options_json, version, resume_count, created_at,
            started_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job.id,
                job.product_id,
                job.status.value,
                job.current_stage,
                _dumps(job.stage_status),
                job.progress,
                job.message,
                job.error,
                _dumps(job.options),
                job.version,
                job.resume_count,
                job.created_at,
                job.started_at,
                job.finished_at,
            ),
        )
        return job

    def update_job(self, job_id: str, **fields: Any) -> None:
        updates = {key: value for key, value in fields.items() if key in _JOB_COLUMNS}
        if not updates:
            return
        if "status" in updates and isinstance(updates["status"], JobStatus):
            updates["status"] = updates["status"].value
        for key in ("stage_status", "options"):
            if key in updates:
                updates[f"{key}_json"] = _dumps(updates.pop(key))
        assignments = ", ".join(f"{key}=?" for key in updates)
        self.execute(f"UPDATE jobs SET {assignments} WHERE id=?", (*updates.values(), job_id))

    def get_job(self, job_id: str) -> Job | None:
        row = self.query_one("SELECT * FROM jobs WHERE id=?", (job_id,))
        return self._row_job(row) if row else None

    def latest_job(self, product_id: str) -> Job | None:
        row = self.query_one(
            "SELECT * FROM jobs WHERE product_id=? ORDER BY created_at DESC LIMIT 1",
            (product_id,),
        )
        return self._row_job(row) if row else None

    def latest_jobs(self, limit: int = 20) -> list[Job]:
        rows = self.query("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self._row_job(row) for row in rows]

    def has_active_job(self) -> bool:
        row = self.query_one("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('pending','running')")
        return bool(row and row["n"])

    @staticmethod
    def _row_job(row: sqlite3.Row) -> Job:
        try:
            status = JobStatus(row["status"])
        except ValueError:
            status = JobStatus.PENDING
        stage_status = _loads(row["stage_status_json"], {})
        if not isinstance(stage_status, dict):
            stage_status = {}
        return Job(
            id=row["id"],
            product_id=row["product_id"],
            status=status,
            current_stage=row["current_stage"],
            stage_status=stage_status,
            progress=row["progress"],
            message=row["message"],
            error=row["error"],
            options=_loads(row["options_json"], {}),
            version=row["version"],
            resume_count=row["resume_count"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    # -------------------------------------------------------------------- pages

    def replace_pages(self, product_id: str, pages: list[PdfPage]) -> None:
        with self.transaction() as connection:
            connection.execute("DELETE FROM pdf_pages WHERE product_id=?", (product_id,))
            connection.executemany(
                """INSERT INTO pdf_pages (product_id, page_number, width, height, text,
                blocks_json, is_scanned, ocr_confidence, image_path, has_images, has_formula)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        page.product_id,
                        page.page_number,
                        page.width,
                        page.height,
                        page.text,
                        _dumps(page.blocks),
                        int(page.is_scanned),
                        page.ocr_confidence,
                        page.image_path,
                        int(page.has_images),
                        int(page.has_formula),
                    )
                    for page in pages
                ],
            )

    def list_pages(self, product_id: str) -> list[PdfPage]:
        rows = self.query(
            "SELECT * FROM pdf_pages WHERE product_id=? ORDER BY page_number", (product_id,)
        )
        return [self._row_page(row) for row in rows]

    def get_page(self, product_id: str, page_number: int) -> PdfPage | None:
        row = self.query_one(
            "SELECT * FROM pdf_pages WHERE product_id=? AND page_number=?",
            (product_id, page_number),
        )
        return self._row_page(row) if row else None

    @staticmethod
    def _row_page(row: sqlite3.Row) -> PdfPage:
        return PdfPage(
            product_id=row["product_id"],
            page_number=row["page_number"],
            width=row["width"],
            height=row["height"],
            text=row["text"],
            blocks=_loads(row["blocks_json"], []),
            is_scanned=bool(row["is_scanned"]),
            ocr_confidence=row["ocr_confidence"],
            image_path=row["image_path"],
            has_images=bool(row["has_images"]),
            has_formula=bool(row["has_formula"]),
        )

    # ----------------------------------------------------------------- chapters

    def replace_chapters(self, product_id: str, chapters: list[Chapter]) -> None:
        with self.transaction() as connection:
            connection.execute("DELETE FROM chapters WHERE product_id=?", (product_id,))
            connection.executemany(
                """INSERT INTO chapters (id, product_id, number, title, start_page, end_page,
                level, parent_id, exercise_count) VALUES (?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        chapter.id,
                        chapter.product_id,
                        chapter.number,
                        chapter.title,
                        chapter.start_page,
                        chapter.end_page,
                        chapter.level,
                        chapter.parent_id,
                        chapter.exercise_count,
                    )
                    for chapter in chapters
                ],
            )

    def list_chapters(self, product_id: str) -> list[Chapter]:
        rows = self.query(
            "SELECT * FROM chapters WHERE product_id=? ORDER BY number, start_page", (product_id,)
        )
        return [
            Chapter(
                id=row["id"],
                product_id=row["product_id"],
                number=row["number"],
                title=row["title"],
                start_page=row["start_page"],
                end_page=row["end_page"],
                level=row["level"],
                parent_id=row["parent_id"],
                exercise_count=row["exercise_count"],
            )
            for row in rows
        ]

    def update_chapter_count(self, chapter_id: str, count: int) -> None:
        self.execute("UPDATE chapters SET exercise_count=? WHERE id=?", (count, chapter_id))

    # ---------------------------------------------------------------- exercises

    def replace_exercises(self, product_id: str, exercises: list[Exercise]) -> None:
        with self.transaction() as connection:
            connection.execute("DELETE FROM exercises WHERE product_id=?", (product_id,))
            connection.executemany(
                """INSERT INTO exercises (id, product_id, chapter_id, number, global_number,
                label, page_start, page_end, text, latex, images_json, exercise_type,
                confidence, status, source_hash, raw_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        exercise.id,
                        exercise.product_id,
                        exercise.chapter_id,
                        exercise.number,
                        exercise.global_number,
                        exercise.label,
                        exercise.page_start,
                        exercise.page_end,
                        exercise.text,
                        exercise.latex,
                        _dumps(exercise.images),
                        exercise.exercise_type.value,
                        exercise.confidence,
                        exercise.status.value,
                        exercise.source_hash,
                        _dumps(exercise.raw),
                    )
                    for exercise in exercises
                ],
            )

    def list_exercises(
        self, product_id: str, chapter_id: str | None = None
    ) -> list[Exercise]:
        if chapter_id:
            rows = self.query(
                "SELECT * FROM exercises WHERE product_id=? AND chapter_id=? ORDER BY global_number",
                (product_id, chapter_id),
            )
        else:
            rows = self.query(
                "SELECT * FROM exercises WHERE product_id=? ORDER BY global_number", (product_id,)
            )
        return [self._row_exercise(row) for row in rows]

    def get_exercise(self, exercise_id: str) -> Exercise | None:
        row = self.query_one("SELECT * FROM exercises WHERE id=?", (exercise_id,))
        return self._row_exercise(row) if row else None

    def update_exercise(self, exercise_id: str, **fields: Any) -> None:
        updates = {key: value for key, value in fields.items() if key in _EXERCISE_COLUMNS}
        if not updates:
            return
        for key in ("images", "raw"):
            if key in updates:
                updates[f"{key}_json"] = _dumps(updates.pop(key))
        if "exercise_type" in updates and isinstance(updates["exercise_type"], ExerciseType):
            updates["exercise_type"] = updates["exercise_type"].value
        if "status" in updates and isinstance(updates["status"], ExerciseStatus):
            updates["status"] = updates["status"].value
        assignments = ", ".join(f"{key}=?" for key in updates)
        self.execute(f"UPDATE exercises SET {assignments} WHERE id=?", (*updates.values(), exercise_id))

    def exercise_status_counts(self, product_id: str) -> dict[str, int]:
        rows = self.query(
            "SELECT status, COUNT(*) AS n FROM exercises WHERE product_id=? GROUP BY status",
            (product_id,),
        )
        return {row["status"]: row["n"] for row in rows}

    @staticmethod
    def _row_exercise(row: sqlite3.Row) -> Exercise:
        try:
            exercise_type = ExerciseType(row["exercise_type"])
        except ValueError:
            exercise_type = ExerciseType.UNKNOWN
        try:
            status = ExerciseStatus(row["status"])
        except ValueError:
            status = ExerciseStatus.PENDING
        return Exercise(
            id=row["id"],
            product_id=row["product_id"],
            chapter_id=row["chapter_id"],
            number=row["number"],
            global_number=row["global_number"],
            label=row["label"],
            page_start=row["page_start"],
            page_end=row["page_end"],
            text=row["text"],
            latex=row["latex"],
            images=_loads(row["images_json"], []),
            exercise_type=exercise_type,
            confidence=row["confidence"],
            status=status,
            source_hash=row["source_hash"],
            raw=_loads(row["raw_json"], {}),
        )

    # ---------------------------------------------------------------- solutions

    def add_solution(self, solution: Solution) -> Solution:
        self.execute(
            """INSERT INTO solutions (id, exercise_id, product_id, model, prompt_version,
            content_json, status, tokens_in, tokens_out, cost, cost_known, latency_ms,
            content_hash, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                solution.id,
                solution.exercise_id,
                solution.product_id,
                solution.model,
                solution.prompt_version,
                _dumps(solution.content.model_dump()),
                solution.status.value,
                solution.tokens_in,
                solution.tokens_out,
                solution.cost,
                int(solution.cost_known),
                solution.latency_ms,
                solution.content_hash,
                solution.created_at,
                solution.updated_at,
            ),
        )
        return solution

    def latest_solution(self, exercise_id: str) -> Solution | None:
        row = self.query_one(
            "SELECT * FROM solutions WHERE exercise_id=? ORDER BY created_at DESC LIMIT 1",
            (exercise_id,),
        )
        return self._row_solution(row) if row else None

    def get_solution(self, solution_id: str) -> Solution | None:
        row = self.query_one("SELECT * FROM solutions WHERE id=?", (solution_id,))
        return self._row_solution(row) if row else None

    def update_solution(
        self,
        solution_id: str,
        *,
        content: SolutionContent | None = None,
        status: ExerciseStatus | None = None,
    ) -> None:
        fields: list[str] = ["updated_at=?"]
        values: list[Any] = [utc_now()]
        if content is not None:
            fields.append("content_json=?")
            values.append(_dumps(content.model_dump()))
        if status is not None:
            fields.append("status=?")
            values.append(status.value)
        values.append(solution_id)
        self.execute(f"UPDATE solutions SET {', '.join(fields)} WHERE id=?", values)


    @staticmethod
    def _row_solution(row: sqlite3.Row) -> Solution:
        content_data = _loads(row["content_json"], {})
        content = SolutionContent.model_validate(content_data) if isinstance(content_data, dict) else SolutionContent()
        try:
            status = ExerciseStatus(row["status"])
        except ValueError:
            status = ExerciseStatus.AI_DRAFT
        return Solution(
            id=row["id"],
            exercise_id=row["exercise_id"],
            product_id=row["product_id"],
            model=row["model"],
            prompt_version=row["prompt_version"],
            content=content,
            status=status,
            tokens_in=row["tokens_in"],
            tokens_out=row["tokens_out"],
            cost=row["cost"],
            cost_known=bool(row["cost_known"]),
            latency_ms=row["latency_ms"],
            content_hash=row["content_hash"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ------------------------------------------------------------ verifications

    def add_verification(self, verification: Verification) -> Verification:
        self.execute(
            """INSERT INTO verifications (id, exercise_id, solution_id, method,
            reviewer_model, verdict, details_json, tokens_in, tokens_out, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                verification.id,
                verification.exercise_id,
                verification.solution_id,
                verification.method,
                verification.reviewer_model,
                verification.verdict.value,
                _dumps(verification.details),
                verification.tokens_in,
                verification.tokens_out,
                verification.created_at,
            ),
        )
        return verification

    def latest_verification(self, exercise_id: str) -> Verification | None:
        row = self.query_one(
            "SELECT * FROM verifications WHERE exercise_id=? ORDER BY created_at DESC LIMIT 1",
            (exercise_id,),
        )
        return self._row_verification(row) if row else None

    @staticmethod
    def _row_verification(row: sqlite3.Row) -> Verification:
        try:
            verdict = VerificationVerdict(row["verdict"])
        except ValueError:
            verdict = VerificationVerdict.INCONCLUSIVE
        return Verification(
            id=row["id"],
            exercise_id=row["exercise_id"],
            solution_id=row["solution_id"],
            method=row["method"],
            reviewer_model=row["reviewer_model"],
            verdict=verdict,
            details=_loads(row["details_json"], {}),
            tokens_in=row["tokens_in"],
            tokens_out=row["tokens_out"],
            created_at=row["created_at"],
        )

    # ------------------------------------------------------------------- files

    def add_file(self, record: GeneratedFile) -> GeneratedFile:
        self.execute(
            """INSERT OR REPLACE INTO generated_files (id, product_id, kind, path, sha256,
            size, created_at, metadata_json) VALUES (?,?,?,?,?,?,?,?)""",
            (
                record.id,
                record.product_id,
                record.kind,
                record.path,
                record.sha256,
                record.size,
                record.created_at,
                _dumps(record.metadata),
            ),
        )
        return record

    def list_files(self, product_id: str, kind: str | None = None) -> list[GeneratedFile]:
        if kind:
            rows = self.query(
                "SELECT * FROM generated_files WHERE product_id=? AND kind=? ORDER BY created_at",
                (product_id, kind),
            )
        else:
            rows = self.query(
                "SELECT * FROM generated_files WHERE product_id=? ORDER BY created_at", (product_id,)
            )
        return [
            GeneratedFile(
                id=row["id"],
                product_id=row["product_id"],
                kind=row["kind"],
                path=row["path"],
                sha256=row["sha256"],
                size=row["size"],
                created_at=row["created_at"],
                metadata=_loads(row["metadata_json"], {}),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------ uploads

    def save_upload(self, record: UploadRecord) -> UploadRecord:
        self.execute(
            """INSERT OR REPLACE INTO uploads (id, product_id, local_path, remote_path,
            size, sha256, remote_md5, status, attempts, error, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record.id,
                record.product_id,
                record.local_path,
                record.remote_path,
                record.size,
                record.sha256,
                record.remote_md5,
                record.status,
                record.attempts,
                record.error,
                record.created_at,
                record.updated_at,
            ),
        )
        return record

    def list_uploads(self, product_id: str) -> list[UploadRecord]:
        rows = self.query("SELECT * FROM uploads WHERE product_id=? ORDER BY created_at", (product_id,))
        return [
            UploadRecord(
                id=row["id"],
                product_id=row["product_id"],
                local_path=row["local_path"],
                remote_path=row["remote_path"],
                size=row["size"],
                sha256=row["sha256"],
                remote_md5=row["remote_md5"],
                status=row["status"],
                attempts=row["attempts"],
                error=row["error"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def update_upload(self, upload_id: str, **fields: Any) -> None:
        allowed = {"status", "attempts", "error", "remote_md5", "size", "sha256"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        updates["updated_at"] = utc_now()
        assignments = ", ".join(f"{key}=?" for key in updates)
        self.execute(f"UPDATE uploads SET {assignments} WHERE id=?", (*updates.values(), upload_id))

    # ------------------------------------------------------------------- shares

    def add_share(self, share: ShareRecord) -> ShareRecord:
        self.execute(
            """INSERT INTO shares (id, product_id, url, password, period, status, verified,
            verified_at, details_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                share.id,
                share.product_id,
                share.url,
                share.password,
                share.period,
                share.status,
                int(share.verified),
                share.verified_at,
                _dumps(share.details),
                share.created_at,
            ),
        )
        return share

    def latest_share(self, product_id: str) -> ShareRecord | None:
        row = self.query_one(
            "SELECT * FROM shares WHERE product_id=? ORDER BY created_at DESC LIMIT 1", (product_id,)
        )
        if not row:
            return None
        return ShareRecord(
            id=row["id"],
            product_id=row["product_id"],
            url=row["url"],
            password=row["password"],
            period=row["period"],
            status=row["status"],
            verified=bool(row["verified"]),
            verified_at=row["verified_at"],
            details=_loads(row["details_json"], {}),
            created_at=row["created_at"],
        )

    # -------------------------------------------------------------------- usage

    def add_usage(self, usage: ApiUsage) -> ApiUsage:
        self.execute(
            """INSERT INTO api_usage (id, product_id, exercise_id, kind, model, input_tokens,
            output_tokens, cached_tokens, estimated_cost, cost_known, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                usage.id,
                usage.product_id,
                usage.exercise_id,
                usage.kind,
                usage.model,
                usage.input_tokens,
                usage.output_tokens,
                usage.cached_tokens,
                usage.estimated_cost,
                int(usage.cost_known),
                usage.created_at,
            ),
        )
        return usage

    def usage_for_product(self, product_id: str) -> dict[str, Any]:
        row = self.query_one(
            """SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens),0) AS tin,
            COALESCE(SUM(output_tokens),0) AS tout, COALESCE(SUM(estimated_cost),0) AS cost,
            SUM(CASE WHEN cost_known=0 THEN 1 ELSE 0 END) AS unknown_cost
            FROM api_usage WHERE product_id=?""",
            (product_id,),
        )
        return dict(row) if row else {"calls": 0, "tin": 0, "tout": 0, "cost": 0.0, "unknown_cost": 0}

    def usage_today(self) -> dict[str, Any]:
        row = self.query_one(
            """SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens),0) AS tin,
            COALESCE(SUM(output_tokens),0) AS tout, COALESCE(SUM(estimated_cost),0) AS cost,
            SUM(CASE WHEN cost_known=0 THEN 1 ELSE 0 END) AS unknown_cost
            FROM api_usage WHERE date(created_at)=date('now')"""
        )
        return dict(row) if row else {"calls": 0, "tin": 0, "tout": 0, "cost": 0.0, "unknown_cost": 0}

    # --------------------------------------------------------------------- logs

    def log_event(self, level: str, scope: str, message: str, details: dict[str, Any] | None = None) -> None:
        self.execute(
            "INSERT INTO logs (level, scope, message, details_json, created_at) VALUES (?,?,?,?,?)",
            (level.upper(), scope, message, _dumps(details or {}), utc_now()),
        )

    def list_events(self, limit: int = 100, scope: str = "") -> list[dict[str, Any]]:
        if scope:
            rows = self.query(
                "SELECT * FROM logs WHERE scope=? ORDER BY id DESC LIMIT ?", (scope, limit)
            )
        else:
            rows = self.query("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,))
        return [
            {
                "id": row["id"],
                "level": row["level"],
                "scope": row["scope"],
                "message": row["message"],
                "details": _loads(row["details_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_kv(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value_json FROM settings WHERE key=?", (key,))
        return _loads(row["value_json"], default) if row else default

    def set_kv(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT OR REPLACE INTO settings (key, value_json, updated_at) VALUES (?,?,?)",
            (key, _dumps(value), utc_now()),
        )

    # -------------------------------------------------------------------- stats

    def product_counts(self) -> dict[str, int]:
        rows = self.query("SELECT status, COUNT(*) AS n FROM products GROUP BY status")
        return {row["status"]: row["n"] for row in rows}

    def exercise_totals(self) -> dict[str, int]:
        rows = self.query("SELECT status, COUNT(*) AS n FROM exercises GROUP BY status")
        return {row["status"]: row["n"] for row in rows}

    def dashboard_stats(self) -> dict[str, Any]:
        product_counts = self.product_counts()
        exercises = self.exercise_totals()
        usage = self.query_one(
            """SELECT COUNT(*) AS calls,
            COALESCE(SUM(input_tokens),0) AS tin, COALESCE(SUM(output_tokens),0) AS tout,
            COALESCE(SUM(estimated_cost),0) AS cost,
            SUM(CASE WHEN cost_known=0 THEN 1 ELSE 0 END) AS unknown_cost FROM api_usage"""
        )
        return {
            "products_total": sum(product_counts.values()),
            "products_completed": product_counts.get("completed", 0),
            "products_in_progress": product_counts.get("processing", 0)
            + product_counts.get("queued", 0),
            "products_failed": product_counts.get("failed", 0),
            "exercises_total": sum(exercises.values()),
            "exercises_verified": exercises.get("verified", 0) + exercises.get("approved", 0),
            "exercises_review": exercises.get("needs_review", 0),
            "tokens_in": int(usage["tin"] if usage else 0),
            "tokens_out": int(usage["tout"] if usage else 0),
            "api_calls": int(usage["calls"] if usage else 0),
            "cost": float(usage["cost"] if usage else 0.0),
            "cost_unknown_calls": int(usage["unknown_cost"] or 0) if usage else 0,
        }


def find_reusable_solution(
    db: Database, exercise: Exercise, model: str, prompt_version: str
) -> Solution | None:
    """Return a verified solution for identical source content and model.

    Reuse is deliberately narrow: same prompt version, same model, same source
    hash.  Content-hash reuse avoids paying twice after a restart without
    treating a stale draft as final.
    """

    row = db.query_one(
        """SELECT s.* FROM solutions s JOIN exercises e ON e.id=s.exercise_id
        WHERE e.source_hash=? AND s.model=? AND s.prompt_version=?
        AND s.status IN ('verified','approved') ORDER BY s.created_at DESC LIMIT 1""",
        (exercise.source_hash, model, prompt_version),
    )
    return db._row_solution(row) if row else None


def content_hash(exercise: Exercise) -> str:
    return stable_hash(
        exercise.text.strip(),
        exercise.latex.strip(),
        exercise.exercise_type.value,
        exercise.images,
    )
