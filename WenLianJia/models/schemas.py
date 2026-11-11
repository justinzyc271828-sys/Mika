"""Typed records used across parsing, solving, production and delivery.

The SQLite layer stores JSON for nested structures and keeps the scalar columns
that are queried often.  Pydantic validates anything that crosses a module or
process boundary, which prevents half-written records from reaching the PDF or
the upload stage.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> str:
    """Return a sortable UTC timestamp with second precision."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str = "") -> str:
    value = uuid.uuid4().hex
    return f"{prefix}{value}" if prefix else value


def stable_hash(*parts: Any) -> str:
    """Hash arbitrary JSON-serialisable values deterministically."""

    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StageStatus(str, Enum):
    WAITING = "waiting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ExerciseStatus(str, Enum):
    PENDING = "pending"
    SOLVING = "solving"
    AI_DRAFT = "ai_draft"
    VERIFIED = "verified"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
    APPROVED = "approved"  # human accepted, equivalent to verified for release


class ExerciseType(str, Enum):
    CALCULATION = "calculation"
    PROOF = "proof"
    CHOICE = "choice"
    FILL_BLANK = "fill_blank"
    SHORT_ANSWER = "short_answer"
    PROGRAMMING = "programming"
    EXPERIMENT = "experiment"
    UNKNOWN = "unknown"


class Subject(str, Enum):
    MATHEMATICS = "mathematics"
    PHYSICS = "physics"
    CHEMISTRY = "chemistry"
    COMPUTER_SCIENCE = "computer_science"
    OTHER = "other"


class VerificationVerdict(str, Enum):
    PASS = "pass"
    NEEDS_REVISION = "needs_revision"
    REJECT = "reject"
    INCONCLUSIVE = "inconclusive"


class SkuLifecycle(str, Enum):
    """Selling lifecycle of a SKU, deliberately separate from production."""

    DRAFT = "DRAFT"
    PENDING_PRODUCTION = "PENDING_PRODUCTION"
    PRODUCING = "PRODUCING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    READY = "READY"
    PUBLISHED = "PUBLISHED"
    PAUSED = "PAUSED"
    ARCHIVED = "ARCHIVED"
    FAILED = "FAILED"


class DeliveryStatus(str, Enum):
    """Cloud delivery state of a SKU, independent of its selling lifecycle."""

    NOT_DELIVERED = "NOT_DELIVERED"
    PENDING = "PENDING"
    UPLOADED = "UPLOADED"
    SHARE_CREATED = "SHARE_CREATED"
    VERIFIED = "VERIFIED"
    NEEDS_MANUAL = "NEEDS_MANUAL"
    FAILED = "FAILED"


class BookVersionStatus(str, Enum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"
    FAILED = "FAILED"


class BatchStatus(str, Enum):
    PREVIEW = "PREVIEW"
    READY = "READY"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Product(BaseModel):
    """A production entity: one parsed volume, its answers and its PDFs.

    A textbook (``Book``) groups one or more productions.  A sale (``SalesSKU``)
    points at one production for a single-volume product or several productions
    for a bundle.  The three layers are deliberately separate so a bundle can
    reuse existing PDFs without paying for the same model calls again.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("prd_"))
    book_id: str = ""
    volume_label: str = ""
    volume_number: int = 0
    volume_code: str = ""
    sku: str = ""
    title: str = ""
    subject: Subject = Subject.OTHER
    source_title: str = ""
    source_author: str = ""
    source_edition: str = ""
    source_path: str = ""
    source_sha256: str = ""
    total_pages: int = 0
    chapter_count: int = 0
    exercise_count: int = 0
    solved_count: int = 0
    verified_count: int = 0
    review_required: int = 0
    status: str = "draft"
    version: str = "1.0"
    notes: str = ""
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Book(BaseModel):
    """Bibliographic identity of one textbook edition.

    ``code`` is the owner-visible catalogue code (for example ``LA001``), while
    ``id`` is the durable database id.  A book can own several productions,
    one per volume.  Cover fields are only filled from a traceable source.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("book_"))
    code: str = ""
    title: str = ""
    original_title: str = ""
    authors: list[str] = Field(default_factory=list)
    translators: list[str] = Field(default_factory=list)
    publisher: str = ""
    edition: str = ""
    publication_year: str = ""
    isbn_10: str = ""
    isbn_13: str = ""
    volumes: int = 1
    language: str = "zh-CN"
    subject: Subject = Subject.OTHER
    cover_image: str = ""
    cover_source_url: str = ""
    source_license: str = ""
    source_verified: bool = False
    verification_status: str = "NEEDS_REVIEW"
    family_id: str = ""
    category_id: str = ""
    template_overrides: dict[str, Any] = Field(default_factory=dict)
    status: str = "ACTIVE"
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)


class SkuItem(BaseModel):
    """One production included in a sale, with the edition the buyer receives."""

    model_config = ConfigDict(extra="ignore")

    product_id: str
    volume_label: str = ""
    edition: str = "detailed"  # detailed | quick | both
    sort_order: int = 0


class SalesSKU(BaseModel):
    """A sellable unit.  Prices are integer cents, never binary floats."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("sku_"))
    sku_code: str = ""
    book_id: str = ""
    name: str = ""
    kind: str = "single"  # single | bundle
    items: list[SkuItem] = Field(default_factory=list)
    price_cents: int = 0
    original_price_cents: int = 0
    enabled: bool = True
    description: str = ""
    image_dir: str = ""
    status: str = "draft"
    allow_above_parts: bool = False
    family_id: str = ""
    category_id: str = ""
    version_id: str = ""
    lifecycle: SkuLifecycle = SkuLifecycle.DRAFT
    delivery_status: DeliveryStatus = DeliveryStatus.NOT_DELIVERED
    primary_product_id: str = ""
    production_status: str = ""
    published_at: str = ""
    paused_at: str = ""
    archived_at: str = ""
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)


class BookCandidate(BaseModel):
    """A ranked candidate returned by a book metadata lookup."""

    model_config = ConfigDict(extra="ignore")

    candidate_id: str = Field(default_factory=lambda: new_id("cand_"))
    title: str = ""
    original_title: str = ""
    authors: list[str] = Field(default_factory=list)
    publisher: str = ""
    edition: str = ""
    publication_year: str = ""
    isbn_10: str = ""
    isbn_13: str = ""
    volume: str = ""
    language: str = ""
    cover_url: str = ""
    source_url: str = ""
    source_name: str = ""
    source_license: str = ""
    authority: int = -1
    score: float = 0.0
    reasons: list[str] = Field(default_factory=list)


class Job(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("job_"))
    product_id: str
    status: JobStatus = JobStatus.PENDING
    current_stage: str = "queued"
    stage_status: dict[str, str] = Field(default_factory=dict)
    progress: float = 0.0
    message: str = ""
    error: str = ""
    options: dict[str, Any] = Field(default_factory=dict)
    version: int = 1
    resume_count: int = 0
    created_at: str = Field(default_factory=utc_now)
    started_at: str = ""
    finished_at: str = ""


class PdfPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    product_id: str
    page_number: int
    width: float = 0.0
    height: float = 0.0
    text: str = ""
    blocks: list[dict[str, Any]] = Field(default_factory=list)
    is_scanned: bool = False
    ocr_confidence: float = 0.0
    image_path: str = ""
    has_images: bool = False
    has_formula: bool = False


class Chapter(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("ch_"))
    product_id: str
    number: int = 0
    title: str = ""
    start_page: int = 1
    end_page: int = 1
    level: int = 1
    parent_id: str = ""
    exercise_count: int = 0


class Exercise(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    product_id: str
    chapter_id: str
    number: str = ""
    global_number: int = 0
    label: str = ""
    page_start: int = 1
    page_end: int = 1
    text: str = ""
    latex: str = ""
    images: list[str] = Field(default_factory=list)
    exercise_type: ExerciseType = ExerciseType.UNKNOWN
    confidence: float = 0.5
    status: ExerciseStatus = ExerciseStatus.PENDING
    source_hash: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _non_empty_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("exercise id is required")
        return value


class SolutionContent(BaseModel):
    """The exact JSON contract requested from the solving model."""

    model_config = ConfigDict(extra="ignore")

    question_restatement: str = ""
    analysis: str = ""
    knowledge_points: list[str] = Field(default_factory=list)
    approach: str = ""
    detailed_steps: list[str] = Field(default_factory=list)
    final_answer: str = ""
    method_summary: str = ""
    pitfalls: list[str] = Field(default_factory=list)
    answer_latex: str = ""
    confidence: float = 0.0
    needs_figure: bool = False
    figure_note: str = ""


class Solution(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("sol_"))
    exercise_id: str
    product_id: str
    model: str = ""
    prompt_version: str = "1.0"
    content: SolutionContent = Field(default_factory=SolutionContent)
    status: ExerciseStatus = ExerciseStatus.AI_DRAFT
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    cost_known: bool = False
    latency_ms: int = 0
    content_hash: str = ""
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)


class Verification(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("ver_"))
    exercise_id: str
    solution_id: str
    method: str = "independent_model"
    reviewer_model: str = ""
    verdict: VerificationVerdict = VerificationVerdict.INCONCLUSIVE
    details: dict[str, Any] = Field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0
    created_at: str = Field(default_factory=utc_now)


class GeneratedFile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("file_"))
    product_id: str
    kind: str
    path: str
    sha256: str = ""
    size: int = 0
    created_at: str = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class UploadRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("upl_"))
    product_id: str
    local_path: str
    remote_path: str
    size: int = 0
    sha256: str = ""
    remote_md5: str = ""
    status: str = "pending"
    attempts: int = 0
    error: str = ""
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)


class ShareRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("shr_"))
    product_id: str
    url: str = ""
    password: str = ""
    period: str = "permanent"
    status: str = "created"
    verified: bool = False
    verified_at: str = ""
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now)


class ApiUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("use_"))
    product_id: str = ""
    exercise_id: str = ""
    kind: str = "solve"
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    estimated_cost: float = 0.0
    cost_known: bool = False
    created_at: str = Field(default_factory=utc_now)


class ProductStats(BaseModel):
    total: int = 0
    completed: int = 0
    in_progress: int = 0
    failed: int = 0
    exercises: int = 0
    verified: int = 0
    review_required: int = 0


# --------------------------------------------------------------- library layer
# Chapter 26 records.  These are separate from the production entities above:
# a product family is a reusable template, a category/tag is classification,
# an asset is a content-addressed file, and a version is an immutable release.


class ProductFamily(BaseModel):
    """Reusable product-family template (the default one is 闲鱼教辅)."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("fam_"))
    code: str = "xianyu-jiaofu"
    name: str = "闲鱼教辅"
    description: str = ""
    is_default: bool = True
    brand_name: str = "全网同名爱做题的大肥鱼"
    visual_style: dict[str, Any] = Field(default_factory=dict)
    image_template: dict[str, Any] = Field(default_factory=dict)
    detail_template: dict[str, Any] = Field(default_factory=dict)
    intro_template: str = ""
    pdf_template: str = "templates/latex/solution.tex.j2"
    watermark: dict[str, Any] = Field(default_factory=dict)
    pricing: dict[str, Any] = Field(default_factory=dict)
    default_category_id: str = ""
    delivery_rules: dict[str, Any] = Field(default_factory=dict)
    netdisk_rules: dict[str, Any] = Field(default_factory=dict)
    version: int = 1
    active: bool = True
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)


class Category(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("cat_"))
    code: str = ""
    name: str = ""
    parent_id: str = ""
    sort_order: int = 0
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)


class Tag(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("tag_"))
    name: str = ""
    color: str = ""
    created_at: str = Field(default_factory=utc_now)


class BookVersion(BaseModel):
    """One immutable production release of a textbook."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("ver_"))
    book_id: str = ""
    version_label: str = ""
    status: BookVersionStatus = BookVersionStatus.DRAFT
    notes: str = ""
    product_id: str = ""
    created_by: str = "owner"
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)


class Asset(BaseModel):
    """Content-addressed file metadata; bytes live in the asset store."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("ast_"))
    file_name: str = ""
    file_type: str = ""
    file_size: int = 0
    sha256: str = ""
    local_path: str = ""
    cloud_path: str = ""
    version: str = "1"
    license_source: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now)


class AssetLink(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("lnk_"))
    asset_id: str = ""
    owner_type: str = ""  # book | version | sku | product
    owner_id: str = ""
    role: str = "attachment"
    created_at: str = Field(default_factory=utc_now)


class BatchJob(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("bat_"))
    kind: str = ""
    status: BatchStatus = BatchStatus.PREVIEW
    total: int = 0
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    actor: str = "owner"
    options: dict[str, Any] = Field(default_factory=dict)
    preview: list[dict[str, Any]] = Field(default_factory=list)
    report: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    created_at: str = Field(default_factory=utc_now)
    started_at: str = ""
    finished_at: str = ""


class BatchJobItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("bit_"))
    job_id: str = ""
    entity_type: str = "sku"
    entity_id: str = ""
    status: str = "PENDING"  # PENDING | SUCCESS | FAILED | SKIPPED
    message: str = ""
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now)


class AuditEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("aud_"))
    actor: str = "owner"
    action: str = ""
    entity_type: str = ""
    entity_id: str = ""
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    detail: str = ""
    created_at: str = Field(default_factory=utc_now)
