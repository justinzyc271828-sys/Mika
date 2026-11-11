"""Previewed, reportable and retryable batch operations.

Every batch starts as a preview job.  The owner sees which rows are valid before
anything is written; running the job records one item row per input row, and a
failed row can be retried without touching the successful rows.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import func, select

from core.catalog import CatalogError, CatalogManager, cents_to_yuan, yuan_to_cents
from core.library.common import LibraryError, Page, clamp_page, dumps, loads
from core.library.orm import BatchItemRow, BatchJobRow
from core.library.service import LibraryService, _batch_from_row
from models.schemas import (
    BatchJob,
    BatchJobItem,
    BatchStatus,
    BookVersionStatus,
    DeliveryStatus,
    SalesSKU,
    SkuItem,
    SkuLifecycle,
    utc_now,
)

IMPORT_KINDS = {
    "import_books",
    "import_skus",
    "update_price",
    "update_category",
    "add_tags",
    "enable_skus",
    "disable_skus",
    "update_version",
    "generate_intro",
    "generate_images",
    "upload_netdisk",
    "check_share",
}
EXPORT_KINDS = {"export_books", "export_skus"}
ALL_KINDS = IMPORT_KINDS | EXPORT_KINDS


def _item_from_row(row: BatchItemRow) -> BatchJobItem:
    return BatchJobItem(
        id=row.id,
        job_id=row.job_id,
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        status=row.status,
        message=row.message,
        before=loads(row.before_json, {}),
        after=loads(row.after_json, {}),
        created_at=row.created_at,
    )


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _money(value: Any) -> Any:
    if value in (None, ""):
        return None
    return value


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value).lower() in {"1", "true", "yes", "y", "是", "启用"}


def read_table(path: Path | str) -> list[dict[str, Any]]:
    source = Path(path)
    if not source.is_file():
        raise LibraryError(f"文件不存在：{source}")
    suffix = source.suffix.lower()
    if suffix in {".csv", ".txt"}:
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    if suffix in {".xlsx", ".xlsm"}:
        from openpyxl import load_workbook

        workbook = load_workbook(source, read_only=True, data_only=True)
        sheet = workbook.active
        rows = sheet.iter_rows(values_only=True)
        try:
            headers = [str(value or "").strip() for value in next(rows)]
        except StopIteration:
            return []
        result = []
        for values in rows:
            if not any(value not in (None, "") for value in values):
                continue
            result.append(
                {
                    headers[index]: values[index]
                    for index in range(min(len(headers), len(values)))
                }
            )
        workbook.close()
        return result
    raise LibraryError("只支持 CSV 或 XLSX 文件")


def write_table(path: Path | str, headers: list[str], rows: Iterable[dict[str, Any]]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = list(rows)
    if destination.suffix.lower() in {".xlsx", ".xlsm"}:
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "商品库"
        sheet.append(headers)
        for row in data:
            sheet.append([row.get(header, "") for header in headers])
        workbook.save(destination)
        return destination
    with destination.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for row in data:
            writer.writerow({header: row.get(header, "") for header in headers})
    return destination


class BatchEngine:
    def __init__(self, service: LibraryService):
        self.service = service
        self.db = service.db
        self.paths = service.paths
        self.settings = service.settings

    # ------------------------------------------------------------------ jobs

    def list_jobs(
        self, *, page: int = 1, page_size: int = 50, kind: str = ""
    ) -> Page[BatchJob]:
        page, page_size = clamp_page(page, page_size)
        with self.service.session() as session:
            query = select(BatchJobRow)
            count_query = select(func.count()).select_from(BatchJobRow)
            if kind:
                query = query.where(BatchJobRow.kind == kind)
                count_query = count_query.where(BatchJobRow.kind == kind)
            total = int(session.scalar(count_query) or 0)
            rows = session.scalars(
                query.order_by(BatchJobRow.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            return Page(
                items=[_batch_from_row(row) for row in rows],
                total=total,
                page=page,
                page_size=page_size,
            )

    def get_job(self, job_id: str) -> BatchJob | None:
        with self.service.session() as session:
            row = session.get(BatchJobRow, job_id)
            return _batch_from_row(row) if row else None

    def job_items(self, job_id: str) -> list[BatchJobItem]:
        with self.service.session() as session:
            rows = session.scalars(
                select(BatchItemRow)
                .where(BatchItemRow.job_id == job_id)
                .order_by(BatchItemRow.created_at, BatchItemRow.id)
            )
            return [_item_from_row(row) for row in rows]

    # --------------------------------------------------------------- preview

    def preview(
        self,
        kind: str,
        rows: list[dict[str, Any]],
        *,
        options: dict[str, Any] | None = None,
        actor: str = "owner",
    ) -> BatchJob:
        if kind not in IMPORT_KINDS:
            raise LibraryError(f"不支持的批量操作：{kind}")
        job = BatchJob(
            kind=kind,
            status=BatchStatus.PREVIEW,
            total=len(rows),
            actor=actor,
            options=options or {},
        )
        preview = []
        item_rows = []
        for index, row in enumerate(rows, start=1):
            valid, message = self._validate(kind, row)
            preview.append(
                {
                    "row": index,
                    "data": row,
                    "valid": valid,
                    "message": message,
                }
            )
            item_rows.append(
                BatchItemRow(
                    id=f"bit_{job.id}_{index}",
                    job_id=job.id,
                    entity_type="row",
                    entity_id=str(index),
                    status="READY" if valid else "INVALID",
                    message=message,
                    before_json=dumps(row),
                    after_json="{}",
                    created_at=utc_now(),
                )
            )
        job.preview = preview
        job.succeeded = sum(1 for entry in preview if entry["valid"])
        job.failed = job.total - job.succeeded
        job.report = {
            "total": job.total,
            "valid": job.succeeded,
            "invalid": job.failed,
            "preview_only": True,
        }
        with self.service.session() as session:
            session.add(
                BatchJobRow(
                    id=job.id,
                    kind=job.kind,
                    status=job.status.value,
                    total=job.total,
                    processed=0,
                    succeeded=0,
                    failed=0,
                    actor=job.actor,
                    options_json=dumps(job.options),
                    preview_json=dumps(job.preview),
                    report_json=dumps(job.report),
                    error="",
                    created_at=job.created_at,
                    started_at="",
                    finished_at="",
                )
            )
            session.flush()
            session.add_all(item_rows)
            session.commit()
        self.service.audit(
            "batch.preview",
            "batch",
            job.id,
            after={"kind": kind, "total": job.total, "invalid": job.failed},
            detail=f"批量操作预览：{kind}",
        )
        return job

    def preview_file(
        self,
        kind: str,
        path: Path | str,
        *,
        options: dict[str, Any] | None = None,
        actor: str = "owner",
    ) -> BatchJob:
        return self.preview(kind, read_table(path), options=options, actor=actor)

    # ------------------------------------------------------------------- run

    def run(self, job_id: str, *, retry_failed: bool = False) -> BatchJob:
        job = self.get_job(job_id)
        if job is None:
            raise LibraryError(f"批量任务不存在：{job_id}")
        if job.kind not in IMPORT_KINDS:
            raise LibraryError(f"该任务不能运行：{job.kind}")
        items = self.job_items(job_id)
        if retry_failed:
            targets = [item for item in items if item.status == "FAILED"]
            if not targets:
                return job
        else:
            targets = [item for item in items if item.status in {"READY", "PENDING"}]
            if not targets:
                raise LibraryError("没有可执行的行；请先修正预览中的错误")
        self._set_job(job_id, status=BatchStatus.RUNNING, started_at=utc_now())
        succeeded = 0
        failed = 0
        errors = []
        for item in targets:
            row = loads(self._item_before(item.id), {})
            try:
                entity_id = self._execute(job.kind, row, job.options)
                succeeded += 1
                self._set_item(item.id, status="SUCCESS", entity_id=entity_id, message="完成")
            except (LibraryError, CatalogError, ValueError, OSError) as exc:
                failed += 1
                message = str(exc)
                errors.append({"row": item.entity_id, "error": message})
                self._set_item(item.id, status="FAILED", message=message)
        processed = succeeded + failed
        status = (
            BatchStatus.COMPLETED
            if failed == 0
            else (BatchStatus.PARTIAL if succeeded else BatchStatus.FAILED)
        )
        report = {
            "total": job.total,
            "processed": processed,
            "succeeded": succeeded,
            "failed": failed,
            "errors": errors,
            "retry": retry_failed,
        }
        self._set_job(
            job_id,
            status=status,
            processed=processed,
            succeeded=succeeded,
            failed=failed,
            report=report,
            finished_at=utc_now(),
        )
        self.service.audit(
            "batch.run",
            "batch",
            job_id,
            after=report,
            detail=f"执行批量操作 {job.kind}",
        )
        return self.get_job(job_id)  # type: ignore[return-value]

    def retry(self, job_id: str) -> BatchJob:
        job = self.get_job(job_id)
        if job is None:
            raise LibraryError(f"批量任务不存在：{job_id}")
        for item in self.job_items(job_id):
            if item.status == "FAILED":
                self._set_item(item.id, status="READY", message="等待重试")
        return self.run(job_id, retry_failed=True)

    # ---------------------------------------------------------------- export

    def export_books(
        self,
        path: Path | str,
        *,
        q: str = "",
        family_id: str = "",
        category_id: str = "",
        actor: str = "owner",
    ) -> BatchJob:
        rows = []
        page = 1
        while True:
            result = self.service.list_books(
                q=q, family_id=family_id, category_id=category_id, page=page, page_size=500
            )
            for book in result.items:
                rows.append(
                    {
                        "教材编号": book.code,
                        "书名": book.title,
                        "作者": ";".join(book.authors),
                        "出版社": book.publisher,
                        "版次": book.edition,
                        "出版年": book.publication_year,
                        "ISBN-13": book.isbn_13,
                        "ISBN-10": book.isbn_10,
                        "学科": book.subject.value,
                        "商品系列": book.family_id,
                        "分类": book.category_id,
                        "状态": book.status,
                        "版本数": len(self.service.list_versions(book.id)),
                    }
                )
            if page >= result.pages:
                break
            page += 1
        headers = list(rows[0].keys()) if rows else [
            "教材编号", "书名", "作者", "出版社", "版次", "出版年",
            "ISBN-13", "ISBN-10", "学科", "商品系列", "分类", "状态", "版本数",
        ]
        destination = write_table(path, headers, rows)
        return self._record_export("export_books", destination, len(rows), actor)

    def export_skus(
        self,
        path: Path | str,
        *,
        q: str = "",
        lifecycle: str = "",
        family_id: str = "",
        category_id: str = "",
        actor: str = "owner",
    ) -> BatchJob:
        rows = []
        page = 1
        while True:
            result = self.service.list_skus(
                q=q,
                lifecycle=lifecycle,
                family_id=family_id,
                category_id=category_id,
                page=page,
                page_size=500,
            )
            for sku in result.items:
                book = self.db.get_book(sku.book_id)
                rows.append(
                    {
                        "SKU编号": sku.sku_code,
                        "教材编号": book.code if book else "",
                        "名称": sku.name,
                        "类型": "套装" if sku.kind == "bundle" else "单册",
                        "售价": cents_to_yuan(sku.price_cents),
                        "原价": cents_to_yuan(sku.original_price_cents),
                        "生命周期": sku.lifecycle.value,
                        "交付状态": sku.delivery_status.value,
                        "生产状态": sku.production_status,
                        "版本": sku.version_id,
                        "启用": "是" if sku.enabled else "否",
                    }
                )
            if page >= result.pages:
                break
            page += 1
        headers = list(rows[0].keys()) if rows else [
            "SKU编号", "教材编号", "名称", "类型", "售价", "原价",
            "生命周期", "交付状态", "生产状态", "版本", "启用",
        ]
        destination = write_table(path, headers, rows)
        return self._record_export("export_skus", destination, len(rows), actor)

    def _record_export(self, kind: str, path: Path, count: int, actor: str) -> BatchJob:
        job = BatchJob(
            kind=kind,
            status=BatchStatus.COMPLETED,
            total=count,
            processed=count,
            succeeded=count,
            actor=actor,
            report={"path": str(path), "count": count},
            started_at=utc_now(),
            finished_at=utc_now(),
        )
        with self.service.session() as session:
            session.add(
                BatchJobRow(
                    id=job.id,
                    kind=job.kind,
                    status=job.status.value,
                    total=job.total,
                    processed=job.processed,
                    succeeded=job.succeeded,
                    failed=0,
                    actor=job.actor,
                    options_json="{}",
                    preview_json="[]",
                    report_json=dumps(job.report),
                    error="",
                    created_at=job.created_at,
                    started_at=job.started_at,
                    finished_at=job.finished_at,
                )
            )
            session.commit()
        self.service.audit(
            "batch.export", "batch", job.id, after=job.report, detail=f"导出 {count} 条记录"
        )
        return job

    # -------------------------------------------------------------- validation

    def _validate(self, kind: str, row: dict[str, Any]) -> tuple[bool, str]:
        try:
            if kind == "import_books":
                if not _text(row.get("教材编号") or row.get("code")):
                    raise LibraryError("缺少教材编号")
                if not _text(row.get("书名") or row.get("title")):
                    raise LibraryError("缺少书名")
            elif kind == "import_skus":
                book_code = _text(row.get("教材编号") or row.get("book_code"))
                if not book_code:
                    raise LibraryError("缺少教材编号")
                book = self.db.get_book_by_code(book_code)
                if book is None:
                    raise LibraryError(f"教材不存在：{book_code}")
                if _money(row.get("价格") or row.get("price")) is None:
                    raise LibraryError("缺少售价")
                sku_kind = _text(row.get("类型") or row.get("kind")) or "单册"
                version_label = _text(row.get("版本") or row.get("version_label"))
                if version_label and not any(
                    v.version_label == version_label
                    for v in self.service.list_versions(book.id)
                ):
                    raise LibraryError(f"版本不存在：{version_label}")
                lifecycle = _text(row.get("生命周期") or row.get("lifecycle")) or "DRAFT"
                try:
                    SkuLifecycle(lifecycle)
                except ValueError as exc:
                    raise LibraryError(f"生命周期无效：{lifecycle}") from exc
                if sku_kind in {"套装", "bundle"}:
                    parts = _text(row.get("分册编号") or row.get("product_codes"))
                    if not parts:
                        raise LibraryError("套装缺少分册编号")
                    for code in parts.replace("；", "|").split("|"):
                        code = code.strip()
                        if code:
                            self._find_product(book.id, code)
                else:
                    product_code = _text(
                        row.get("分册编号")
                        or row.get("product_code")
                        or row.get("volume_code")
                    )
                    if not product_code:
                        raise LibraryError("单册缺少分册编号")
                    self._find_product(book.id, product_code)
            elif kind in {"update_price", "enable_skus", "disable_skus", "update_version",
                          "generate_images", "upload_netdisk", "check_share"}:
                if not _text(row.get("SKU编号") or row.get("sku_code")):
                    raise LibraryError("缺少 SKU 编号")
                self._sku(row)
                if kind == "update_price" and _money(row.get("售价") or row.get("price")) is None:
                    raise LibraryError("缺少售价")
                if kind == "update_version" and not _text(
                    row.get("版本") or row.get("version_label")
                ):
                    raise LibraryError("缺少版本号")
            elif kind == "update_category":
                book_code = _text(row.get("教材编号") or row.get("book_code"))
                if not book_code:
                    raise LibraryError("缺少教材编号")
                book = self.db.get_book_by_code(book_code)
                if book is None:
                    raise LibraryError(f"教材不存在：{book_code}")
                category_code = _text(row.get("分类编号") or row.get("category_code"))
                if not category_code:
                    raise LibraryError("缺少分类编号")
                if not any(c.code == category_code for c in self.service.list_categories()):
                    raise LibraryError(f"分类不存在：{category_code}")
            elif kind == "add_tags":
                entity_type = _text(row.get("对象类型") or row.get("entity_type")) or "sku"
                entity_id = _text(row.get("对象编号") or row.get("entity_id"))
                if not entity_id:
                    raise LibraryError("缺少对象编号")
                if not _text(row.get("标签") or row.get("tag")):
                    raise LibraryError("缺少标签")
                if entity_type == "sku" and self.db.get_sku(entity_id) is None:
                    raise LibraryError(f"SKU 不存在：{entity_id}")
                if entity_type == "book" and self.db.get_book(entity_id) is None:
                    raise LibraryError(f"教材不存在：{entity_id}")
            elif kind == "generate_intro":
                book_code = _text(row.get("教材编号") or row.get("book_code"))
                if not book_code:
                    raise LibraryError("缺少教材编号")
                if self.db.get_book_by_code(book_code) is None:
                    raise LibraryError(f"教材不存在：{book_code}")
            return True, ""
        except (LibraryError, ValueError) as exc:
            return False, str(exc)

    # ---------------------------------------------------------------- execute

    def _execute(self, kind: str, row: dict[str, Any], options: dict[str, Any]) -> str:
        if kind == "import_books":
            return self._import_book(row, options)
        if kind == "import_skus":
            return self._import_sku(row, options)
        if kind == "update_price":
            return self._update_price(row)
        if kind == "update_category":
            return self._update_category(row)
        if kind == "add_tags":
            return self._add_tag(row)
        if kind in {"enable_skus", "disable_skus"}:
            return self._toggle_sku(row, kind == "enable_skus")
        if kind == "update_version":
            return self._update_version(row)
        if kind == "generate_intro":
            return self._generate_intro(row)
        if kind == "generate_images":
            return self._generate_images(row)
        if kind == "upload_netdisk":
            return self._upload_netdisk(row)
        if kind == "check_share":
            return self._check_share(row)
        raise LibraryError(f"不支持的批量操作：{kind}")

    def _import_book(self, row: dict[str, Any], options: dict[str, Any]) -> str:
        code = _text(row.get("教材编号") or row.get("code"))
        existing = self.db.get_book_by_code(code)
        category_value = _text(row.get("分类") or row.get("category_id"))
        category_id = ""
        if category_value:
            category = next(
                (
                    c
                    for c in self.service.list_categories()
                    if c.code == category_value or c.id == category_value
                ),
                None,
            )
            if category is None:
                raise LibraryError(f"分类不存在：{category_value}")
            category_id = category.id
        values = {
            "code": code,
            "title": _text(row.get("书名") or row.get("title")),
            "authors": [
                part.strip()
                for part in _text(row.get("作者") or row.get("authors")).replace("；", ";").split(";")
                if part.strip()
            ],
            "publisher": _text(row.get("出版社") or row.get("publisher")),
            "edition": _text(row.get("版次") or row.get("edition")),
            "publication_year": _text(row.get("出版年") or row.get("publication_year")),
            "isbn_13": _text(row.get("ISBN-13") or row.get("isbn_13")),
            "isbn_10": _text(row.get("ISBN-10") or row.get("isbn_10")),
            "subject": _text(row.get("学科") or row.get("subject")) or "other",
            "category_id": category_id,
            "volumes": int(row.get("册数") or row.get("volumes") or 1),
        }
        if existing:
            if not options.get("update_existing"):
                raise LibraryError(f"教材编号已存在：{code}")
            self.service.update_book(existing.id, **values)
            return existing.id
        book = self.service.create_book(**values)
        return book.id

    def _import_sku(self, row: dict[str, Any], options: dict[str, Any]) -> str:
        book_code = _text(row.get("教材编号") or row.get("book_code"))
        book = self.db.get_book_by_code(book_code)
        if book is None:
            raise LibraryError(f"教材不存在：{book_code}")
        kind = _text(row.get("类型") or row.get("kind")) or "单册"
        price = _money(row.get("售价") or row.get("price"))
        original = _money(row.get("原价") or row.get("original_price"))
        edition = _text(row.get("交付版本") or row.get("edition")) or "detailed"
        lifecycle = _text(row.get("生命周期") or row.get("lifecycle")) or "DRAFT"
        version_label = _text(row.get("版本") or row.get("version_label"))
        version_id = ""
        if version_label:
            versions = self.service.list_versions(book.id)
            match = next((v for v in versions if v.version_label == version_label), None)
            if match is None:
                raise LibraryError(f"版本不存在：{version_label}")
            version_id = match.id
        if kind in {"套装", "bundle"}:
            codes = [
                part.strip()
                for part in _text(row.get("分册编号") or row.get("product_codes")).replace("；", "|").split("|")
                if part.strip()
            ]
            items = []
            for code in codes:
                product = self._find_product(book.id, code)
                items.append(
                    SkuItem(
                        product_id=product.id,
                        volume_label=product.volume_label,
                        edition=edition,
                    )
                )
            sku = self.service.create_bundle(
                book_id=book.id,
                items=items,
                name=_text(row.get("名称") or row.get("name")) or f"{book.title}套装",
                price=price,
                original_price=original,
                sku_code=_text(row.get("SKU编号") or row.get("sku_code")),
                description=_text(row.get("描述") or row.get("description")),
                allow_above_parts=_bool(row.get("允许高于分册") or row.get("allow_above_parts")),
                lifecycle=SkuLifecycle(lifecycle),
                version_id=version_id,
            )
            return sku.id
        product_code = _text(
            row.get("分册编号") or row.get("product_code") or row.get("volume_code")
        )
        if not product_code:
            raise LibraryError("单册缺少分册编号")
        product = self._find_product(book.id, product_code)
        sku = self.service.create_single_sku(
            book_id=book.id,
            product_id=product.id,
            name=_text(row.get("名称") or row.get("name")) or product.title,
            price=price,
            original_price=original,
            edition=edition,
            sku_code=_text(row.get("SKU编号") or row.get("sku_code")),
            description=_text(row.get("描述") or row.get("description")),
            lifecycle=SkuLifecycle(lifecycle),
            version_id=version_id,
        )
        return sku.id

    def _find_product(self, book_id: str, code: str):
        for product in self.db.list_productions(book_id):
            if code in {product.volume_code, product.sku, product.id}:
                return product
        raise LibraryError(f"分册不存在：{code}")

    def _sku(self, row: dict[str, Any]) -> SalesSKU:
        code = _text(row.get("SKU编号") or row.get("sku_code"))
        sku = self.db.get_sku_by_code(code)
        if sku is None:
            raise LibraryError(f"SKU 不存在：{code}")
        return sku

    def _update_price(self, row: dict[str, Any]) -> str:
        sku = self._sku(row)
        price = _money(row.get("售价") or row.get("price"))
        original = _money(row.get("原价") or row.get("original_price"))
        self.service.set_price(sku.id, price, original)
        return sku.id

    def _update_category(self, row: dict[str, Any]) -> str:
        code = _text(row.get("教材编号") or row.get("book_code"))
        book = self.db.get_book_by_code(code)
        if book is None:
            raise LibraryError(f"教材不存在：{code}")
        category_code = _text(row.get("分类编号") or row.get("category_code"))
        category = next(
            (c for c in self.service.list_categories() if c.code == category_code), None
        )
        if category is None:
            raise LibraryError(f"分类不存在：{category_code}")
        self.service.update_book(book.id, category_id=category.id)
        return book.id

    def _add_tag(self, row: dict[str, Any]) -> str:
        entity_type = _text(row.get("对象类型") or row.get("entity_type")) or "sku"
        entity_id = _text(row.get("对象编号") or row.get("entity_id"))
        tag_name = _text(row.get("标签") or row.get("tag"))
        tag = next((t for t in self.service.list_tags() if t.name == tag_name), None)
        if tag is None:
            tag = self.service.create_tag(tag_name)
        self.service.tag_entity(entity_type, entity_id, tag.id)
        return entity_id

    def _toggle_sku(self, row: dict[str, Any], enabled: bool) -> str:
        sku = self._sku(row)
        self.service.update_sku(sku.id, enabled=enabled)
        return sku.id

    def _update_version(self, row: dict[str, Any]) -> str:
        sku = self._sku(row)
        label = _text(row.get("版本") or row.get("version_label"))
        version = next(
            (v for v in self.service.list_versions(sku.book_id) if v.version_label == label),
            None,
        )
        if version is None:
            raise LibraryError(f"版本不存在：{label}")
        self.service.set_sku_version(sku.id, version.id)
        return sku.id

    def _generate_intro(self, row: dict[str, Any]) -> str:
        code = _text(row.get("教材编号") or row.get("book_code"))
        book = self.db.get_book_by_code(code)
        if book is None:
            raise LibraryError(f"教材不存在：{code}")
        template = self.service.resolve_book_template(book.id)
        sku_count = self.service.list_skus(book_id=book.id, page_size=1).total
        intro = template.format_intro(
            {
                "title": book.title,
                "volume": "",
                "exercise_count": sum(
                    p.exercise_count for p in self.db.list_productions(book.id)
                ),
                "sku_count": sku_count,
                "publisher": book.publisher,
                "edition": book.edition,
            }
        )
        metadata = dict(book.metadata)
        metadata["intro"] = intro
        self.service.update_book(book.id, metadata=metadata)
        target = self.paths.book_dir(book.code) / "商品介绍.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(intro, encoding="utf-8")
        return book.id

    def _generate_images(self, row: dict[str, Any]) -> str:
        sku = self._sku(row)
        catalog = CatalogManager(self.settings, self.db, self.paths)
        result = catalog.generate_images(sku)
        image_dir = str(result.files[0].parent) if result.files else ""
        self.service.update_sku(sku.id, image_dir=image_dir)
        return sku.id

    def _upload_netdisk(self, row: dict[str, Any]) -> str:
        sku = self._sku(row)
        catalog = CatalogManager(self.settings, self.db, self.paths)
        catalog.upload_sku(sku)
        self.service.set_delivery_status(sku.id, DeliveryStatus.UPLOADED)
        return sku.id

    def _check_share(self, row: dict[str, Any]) -> str:
        sku = self._sku(row)
        catalog = CatalogManager(self.settings, self.db, self.paths)
        records = catalog.verify_sku_uploads(sku)
        status = DeliveryStatus.VERIFIED if records else DeliveryStatus.NEEDS_MANUAL
        self.service.set_delivery_status(sku.id, status)
        return sku.id

    # --------------------------------------------------------------- helpers

    def _set_job(self, job_id: str, **fields: Any) -> None:
        mapping = {
            "status": "status",
            "processed": "processed",
            "succeeded": "succeeded",
            "failed": "failed",
            "report": "report_json",
            "started_at": "started_at",
            "finished_at": "finished_at",
            "error": "error",
        }
        values = {}
        for key, value in fields.items():
            column = mapping.get(key)
            if column is None:
                continue
            if key == "status":
                values[column] = value.value if isinstance(value, BatchStatus) else str(value)
            elif key == "report":
                values[column] = dumps(value)
            else:
                values[column] = value
        if not values:
            return
        with self.service.session() as session:
            session.execute(
                BatchJobRow.__table__.update().where(BatchJobRow.id == job_id).values(**values)
            )
            session.commit()

    def _set_item(self, item_id: str, **fields: Any) -> None:
        mapping = {
            "status": "status",
            "entity_id": "entity_id",
            "message": "message",
            "after": "after_json",
        }
        values = {}
        for key, value in fields.items():
            column = mapping.get(key)
            if column is None:
                continue
            values[column] = dumps(value) if key == "after" else value
        if values:
            with self.service.session() as session:
                session.execute(
                    BatchItemRow.__table__.update()
                    .where(BatchItemRow.id == item_id)
                    .values(**values)
                )
                session.commit()

    def _item_before(self, item_id: str) -> str:
        with self.service.session() as session:
            row = session.get(BatchItemRow, item_id)
            return row.before_json if row else "{}"

    def export_job_report(self, job_id: str, path: Path | str) -> Path:
        job = self.get_job(job_id)
        if job is None:
            raise LibraryError(f"批量任务不存在：{job_id}")
        items = self.job_items(job_id)
        headers = ["行号", "状态", "对象", "说明"]
        rows = [
            {
                "行号": item.entity_id,
                "状态": item.status,
                "对象": item.entity_type,
                "说明": item.message,
            }
            for item in items
        ]
        destination = write_table(path, headers, rows)
        report = dict(job.report)
        report["exported_report"] = str(destination)
        self._set_job(job_id, report=report)
        return destination
