"""End-to-end production pipeline.

The pipeline is intentionally boring: every stage writes its result to disk or
SQLite before the next stage starts, so a crash or a pause only repeats the
work that was not checkpointed.  A failed external permission is never turned
into a success; the local package still completes and the cloud stages are
recorded as blocked or skipped.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from models.schemas import (
    Chapter,
    Exercise,
    ExerciseStatus,
    GeneratedFile,
    Job,
    JobStatus,
    PdfPage,
    Product,
    ShareRecord,
    UploadRecord,
    utc_now,
)

from .ai_client import AiClient, BudgetGuard
from .ai_solver import SolveOutcome, SolveError, SolverService
from .answer_verifier import AnswerVerifier, VerificationError, VerifyOutcome
from .baidu_netdisk import BaiduNetdisk, NetdiskError
from .chapter_detector import ChapterDetector
from .cleanup_manager import CleanupError, CleanupManager
from .config import SettingsManager
from .db import Database
from .document_builder import DocumentBuildError, DocumentBuilder
from .exercise_detector import ExerciseDetector
from .image_generator import ImageGenerationError, ImageGenerator
from .latex_utils import sanitize_filename
from .layout_analyzer import TextLine, detect_headings
from .logging_setup import get_logger
from .paths import AppPaths
from .pdf_parser import PdfError, PdfParser, pdf_inspect, sha256_file
from .product_manager import ProductInfoResult, ProductManager
from .watermark import WatermarkError, Watermarker

log = get_logger("pipeline")

STAGES = (
    "parse",
    "chapters",
    "exercises",
    "solve",
    "verify",
    "pdf",
    "images",
    "records",
    "upload",
    "share",
    "cleanup",
)
STAGE_WEIGHTS = {
    "parse": 0.12,
    "chapters": 0.04,
    "exercises": 0.08,
    "solve": 0.34,
    "verify": 0.20,
    "pdf": 0.10,
    "images": 0.06,
    "records": 0.02,
    "upload": 0.03,
    "share": 0.01,
    "cleanup": 0.00,
}


class PipelineError(RuntimeError):
    pass


class PipelinePaused(Exception):
    pass


class PipelineCancelled(Exception):
    pass


class PipelineControl:
    """Pause/cancel flags shared with the GUI thread."""

    def __init__(self) -> None:
        self._paused = threading.Event()
        self._cancelled = threading.Event()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def cancel(self) -> None:
        self._cancelled.set()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def check(self) -> None:
        if self.cancelled:
            raise PipelineCancelled("任务已取消")
        if self.paused:
            raise PipelinePaused("任务已暂停")


@dataclass
class PipelineResult:
    job: Job
    product: Product
    full_pdf: Path | None = None
    quick_pdf: Path | None = None
    images: list[Path] = field(default_factory=list)
    info_files: list[Path] = field(default_factory=list)
    uploads: list[UploadRecord] = field(default_factory=list)
    share: ShareRecord | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    paused: bool = False
    cancelled: bool = False
    cloud_state: str = "disabled"

    @property
    def ok(self) -> bool:
        return self.job.status == JobStatus.COMPLETED and not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "job": self.job.model_dump(mode="json"),
            "product_id": self.product.id,
            "full_pdf": str(self.full_pdf) if self.full_pdf else "",
            "quick_pdf": str(self.quick_pdf) if self.quick_pdf else "",
            "images": [str(path) for path in self.images],
            "info_files": [str(path) for path in self.info_files],
            "uploads": [record.model_dump(mode="json") for record in self.uploads],
            "share": self.share.model_dump(mode="json") if self.share else None,
            "warnings": self.warnings,
            "errors": self.errors,
            "paused": self.paused,
            "cancelled": self.cancelled,
            "cloud_state": self.cloud_state,
        }


@dataclass
class _RunContext:
    product: Product
    product_dir: Path
    dirs: dict[str, Path]
    job: Job
    parsed_dir: Path | None = None
    full_pdf: Path | None = None
    quick_pdf: Path | None = None
    images: list[Path] = field(default_factory=list)
    info: ProductInfoResult | None = None
    uploads: list[UploadRecord] = field(default_factory=list)
    share: ShareRecord | None = None
    lines: list[TextLine] = field(default_factory=list)
    toc: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stage_details: dict[str, str] = field(default_factory=dict)
    cloud_state: str = "disabled"


class Pipeline:
    def __init__(
        self,
        settings: SettingsManager,
        db: Database,
        paths: AppPaths,
        *,
        ai: AiClient | None = None,
        solver: SolverService | None = None,
        verifier: AnswerVerifier | None = None,
        netdisk: BaiduNetdisk | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.paths = paths
        self.ai = ai
        self._solver = solver
        self._verifier = verifier
        self._netdisk = netdisk
        self._solver_injected = solver is not None
        self._verifier_injected = verifier is not None
        self._on_progress = on_progress

    # ------------------------------------------------------------------ public

    def create_job(self, product_id: str, options: dict[str, Any] | None = None) -> Job:
        product = self.db.get_product(product_id)
        if product is None:
            raise PipelineError("商品不存在")
        for job in self.db.latest_jobs(200):
            if job.product_id == product_id and job.status in (JobStatus.PENDING, JobStatus.RUNNING):
                raise PipelineError("该商品已有正在运行的任务")
        opts = self._normalise_options(options or {})
        job = Job(product_id=product_id, status=JobStatus.PENDING, options=opts, current_stage="queued")
        job.stage_status = {stage: "waiting" for stage in STAGES}
        self.db.create_job(job)
        return job

    def start(
        self,
        product_id: str,
        options: dict[str, Any] | None = None,
        control: PipelineControl | None = None,
    ) -> PipelineResult:
        job = self.create_job(product_id, options)
        return self.run(job.id, control=control)

    def run(
        self,
        job_id: str,
        *,
        control: PipelineControl | None = None,
        options: dict[str, Any] | None = None,
    ) -> PipelineResult:
        control = control or PipelineControl()
        self._control = control
        job = self.db.get_job(job_id)
        if job is None:
            raise PipelineError("任务不存在")
        product = self.db.get_product(job.product_id)
        if product is None:
            raise PipelineError("任务关联的商品不存在")
        if job.status == JobStatus.COMPLETED and not (options or {}).get("force_all"):
            return PipelineResult(job=job, product=product)
        if job.status not in (JobStatus.PENDING, JobStatus.PAUSED, JobStatus.FAILED,
                              JobStatus.CANCELLED, JobStatus.RUNNING):
            raise PipelineError(f"任务状态 {job.status.value} 不能继续")

        opts = self._normalise_options({**job.options, **(options or {})})
        product_dir = self.paths.product_dir(product.sku, product.title)
        dirs = self.paths.product_subdirs(product_dir)
        ctx = _RunContext(
            product=product,
            product_dir=product_dir,
            dirs=dirs,
            job=job,
            parsed_dir=dirs["parsed"],
        )
        self._set_job(job, status=JobStatus.RUNNING, started_at=job.started_at or utc_now(),
                      finished_at="", error="", current_stage="parse", progress=0.0,
                      message="开始生产")
        self.db.update_product(product.id, status="processing")
        ctx.product = self.db.get_product(product.id) or product
        log.info("pipeline start job=%s product=%s options=%s", job.id, product.sku, opts)

        try:
            for stage in STAGES:
                if stage not in self._enabled_stages(opts):
                    self._mark_stage(job, stage, "skipped", "未启用")
                    continue
                if (
                    job.stage_status.get(stage) == "completed"
                    and stage not in ("pdf", "images", "records")
                    and not self._stage_forced(stage, opts)
                ):
                    self._mark_stage(job, stage, "completed", "已完成，跳过")
                    continue
                control.check()
                self._mark_stage(job, stage, "running", "正在处理")
                getattr(self, f"_stage_{stage}")(ctx, opts)
                control.check()
                self._mark_stage(job, stage, "completed", ctx.stage_details.get(stage, "完成"))
            self._finish(ctx, opts)
        except PipelinePaused as exc:
            self._pause_job(ctx, str(exc))
        except PipelineCancelled as exc:
            self._set_job(job, status=JobStatus.CANCELLED, error=str(exc), message="任务已取消",
                          finished_at=utc_now())
            self.db.update_product(product.id, status="cancelled")
            ctx.warnings.append(str(exc))
        except Exception as exc:  # noqa: BLE001 - the job must record every failure
            log.exception("pipeline failed job=%s", job.id)
            message = str(exc) or exc.__class__.__name__
            ctx.errors.append(message)
            failed_stage = job.current_stage or "unknown"
            self._mark_stage(job, failed_stage, "failed", message)
            self._set_job(
                job,
                status=JobStatus.FAILED,
                error=message,
                message=f"生产失败：{message}",
                finished_at=utc_now(),
            )
            self.db.update_product(product.id, status="failed")
        finally:
            self.db.refresh_product_counts(product.id)
            self._write_report(ctx)
            self._save_state(ctx)
            self._set_job(job, stage_status=job.stage_status, current_stage=job.current_stage)
        return PipelineResult(
            job=self.db.get_job(job.id) or job,
            product=self.db.get_product(product.id) or ctx.product,
            full_pdf=ctx.full_pdf,
            quick_pdf=ctx.quick_pdf,
            images=ctx.images,
            info_files=self._info_files(ctx),
            uploads=ctx.uploads,
            share=ctx.share,
            warnings=ctx.warnings,
            errors=ctx.errors,
            paused=job.status == JobStatus.PAUSED,
            cancelled=job.status == JobStatus.CANCELLED,
            cloud_state=ctx.cloud_state,
        )

    # ------------------------------------------------------------------ stages

    def _stage_parse(self, ctx: _RunContext, opts: dict[str, Any]) -> None:
        force = self._stage_forced("parse", opts)
        existing_pages = self.db.list_pages(ctx.product.id)
        lines_file = ctx.dirs["parsed"] / "lines.jsonl"
        if existing_pages and lines_file.exists() and not force:
            ctx.lines = self._load_lines(lines_file)
            self._progress(ctx, "parse", 1.0, f"已解析 {len(existing_pages)} 页")
            self._discover_outputs(ctx)
            return

        source = self._ensure_source_copy(ctx)
        info = pdf_inspect(source)
        ctx.product = self.db.get_product(ctx.product.id) or ctx.product
        self._set_job(ctx.job, message=f"解析 PDF：{info['page_count']} 页")
        parser = PdfParser(
            ocr_engine=str(self.settings.get("pdf.ocr_engine", "auto")),
            render_dpi=int(self.settings.get("pipeline.render_dpi", 150)),
            ocr_dpi=int(self.settings.get("pipeline.ocr_dpi", 180)),
            min_text_chars=int(self.settings.get("pipeline.min_text_chars", 80)),
        )
        result = parser.parse(
            source,
            ctx.dirs["parsed"],
            progress=lambda page, total, message: self._progress(
                ctx, "parse", page / max(1, total), message
            ),
            should_stop=lambda: self._control_cancelled(),
        )
        pages = [
            PdfPage(
                product_id=ctx.product.id,
                page_number=page.page_number,
                width=page.width,
                height=page.height,
                text=page.text,
                blocks=page.blocks,
                is_scanned=page.is_scanned,
                ocr_confidence=page.ocr_confidence,
                image_path=page.image_path,
                has_images=page.has_images,
                has_formula=page.has_formula,
            )
            for page in result.pages
        ]
        self.db.replace_pages(ctx.product.id, pages)
        self.db.update_product(
            ctx.product.id,
            total_pages=result.page_count,
            source_sha256=result.sha256,
            source_path=str(source),
        )
        ctx.lines = result.lines
        ctx.toc = result.toc
        ctx.stage_details["parse"] = (
            f"{result.page_count} 页，{len(result.lines)} 行"
            + (f"，OCR {len(result.scanned_pages)} 页" if result.scanned_pages else "")
        )
        self._progress(ctx, "parse", 1.0, ctx.stage_details["parse"])
        log.info("parsed %s pages=%d scanned=%d", ctx.product.sku, result.page_count,
                 len(result.scanned_pages))

    def _stage_chapters(self, ctx: _RunContext, opts: dict[str, Any]) -> None:
        force = self._stage_forced("chapters", opts)
        existing = self.db.list_chapters(ctx.product.id)
        if existing and not force:
            ctx.stage_details["chapters"] = f"{len(existing)} 章"
            self._progress(ctx, "chapters", 1.0, ctx.stage_details["chapters"])
            return
        self._load_context(ctx)
        detector = ChapterDetector()
        result = detector.detect(
            self.db.list_pages(ctx.product.id),
            ctx.toc,
            ctx.lines,
            detect_headings(ctx.lines),
        )
        chapters: list[Chapter] = []
        for index, detected in enumerate(result.chapters):
            chapter = Chapter(
                id=self._stable_id("ch", ctx.product.id, f"{detected.number:03d}_{index}"),
                product_id=ctx.product.id,
                number=int(detected.number),
                title=str(detected.title).strip() or f"第 {detected.number} 章",
                start_page=max(1, int(detected.start_page)),
                end_page=max(1, int(detected.end_page)),
                level=max(1, int(detected.level)),
            )
            chapters.append(chapter)
        self.db.replace_chapters(ctx.product.id, chapters)
        self._write_json(ctx.dirs["exercises"] / "chapters.json", [c.model_dump(mode="json") for c in chapters])
        ctx.warnings.extend(result.warnings)
        ctx.stage_details["chapters"] = f"{len(chapters)} 章（{result.method}）"
        self._progress(ctx, "chapters", 1.0, ctx.stage_details["chapters"])
        self.db.update_product(ctx.product.id, chapter_count=len(chapters))
        log.info("chapters %s: %d via %s", ctx.product.sku, len(chapters), result.method)

    def _stage_exercises(self, ctx: _RunContext, opts: dict[str, Any]) -> None:
        force = self._stage_forced("exercises", opts)
        existing = self.db.list_exercises(ctx.product.id)
        if existing and not force:
            ctx.stage_details["exercises"] = f"{len(existing)} 道习题"
            self._progress(ctx, "exercises", 1.0, ctx.stage_details["exercises"])
            return
        self._load_context(ctx)
        chapters = self.db.list_chapters(ctx.product.id)
        if not chapters:
            raise PipelineError("尚未识别章节，无法识别习题")
        pages = self.db.list_pages(ctx.product.id)
        detector = ExerciseDetector()
        result = detector.detect(
            sku=ctx.product.sku or "BOOK",
            chapters=chapters,
            pages=pages,
            lines=ctx.lines,
            headings=detect_headings(ctx.lines),
        )
        chapter_by_number = {chapter.number: chapter for chapter in chapters}
        counters: dict[str, int] = {}
        exercises: list[Exercise] = []
        for global_number, detected in enumerate(result.exercises, start=1):
            chapter = chapter_by_number.get(int(detected.chapter_number))
            if chapter is None:
                chapter = chapters[0]
            local = counters.get(chapter.id, 0) + 1
            counters[chapter.id] = local
            exercise_id = self._exercise_id(ctx.product.sku, chapter.number, local)
            raw = dict(detected.raw)
            raw.update(
                {
                    "chapter_number": chapter.number,
                    "chapter_title": chapter.title,
                    "section_title": detected.section_title,
                    "context_before": detected.context_before,
                    "context_after": detected.context_after,
                    "detector_confidence": detected.confidence,
                }
            )
            exercises.append(
                Exercise(
                    id=exercise_id,
                    product_id=ctx.product.id,
                    chapter_id=chapter.id,
                    number=str(detected.number),
                    global_number=global_number,
                    label=f"第 {chapter.number} 章 第 {local} 题",
                    page_start=int(detected.page_start),
                    page_end=int(detected.page_end),
                    text=detected.text.strip(),
                    latex="",
                    images=list(detected.images),
                    exercise_type=detected.exercise_type,
                    confidence=float(detected.confidence),
                    status=ExerciseStatus.PENDING,
                    raw=raw,
                )
            )
        self.db.replace_exercises(ctx.product.id, exercises)
        chapter_counts: dict[str, int] = {}
        for exercise in exercises:
            chapter_counts[exercise.chapter_id] = chapter_counts.get(exercise.chapter_id, 0) + 1
        self._write_json(
            ctx.dirs["exercises"] / "chapter_counts.json",
            {"per_chapter": result.per_chapter, "by_chapter_id": chapter_counts},
        )
        self._write_json(
            ctx.dirs["exercises"] / "exercises.json",
            {
                "sku": ctx.product.sku,
                "total": len(exercises),
                "per_chapter": result.per_chapter,
                "warnings": result.warnings,
                "exercises": [exercise.model_dump(mode="json") for exercise in exercises],
            },
        )
        ctx.warnings.extend(result.warnings)
        if result.low_confidence:
            ctx.warnings.append(f"{result.low_confidence} 道题识别置信度偏低，建议人工检查。")
        ctx.stage_details["exercises"] = f"{len(exercises)} 道习题"
        self._progress(ctx, "exercises", 1.0, ctx.stage_details["exercises"])
        self.db.refresh_product_counts(ctx.product.id)
        log.info("exercises %s: %d", ctx.product.sku, len(exercises))

    def _stage_solve(self, ctx: _RunContext, opts: dict[str, Any]) -> None:
        exercises = self.db.list_exercises(ctx.product.id)
        todo = [
            exercise
            for exercise in exercises
            if self._stage_forced("solve", opts)
            or exercise.status in (ExerciseStatus.PENDING, ExerciseStatus.SOLVING,
                                   ExerciseStatus.FAILED)
        ]
        if not todo:
            ctx.stage_details["solve"] = "所有题目已有解答"
            self._progress(ctx, "solve", 1.0, ctx.stage_details["solve"])
            return
        solver = self._solver_service()
        guard = BudgetGuard(self.settings, self.db)
        total = len(todo)
        done = 0
        failed = 0
        reused = 0
        concurrency = max(1, int(self.settings.get("openai.max_concurrency", 4)))

        def work(exercise: Exercise) -> tuple[Exercise, SolveOutcome | None, str]:
            try:
                outcome = solver.solve(exercise, ctx.product, force=False)
                return exercise, outcome, outcome.error
            except Exception as exc:  # noqa: BLE001 - one question must not kill the batch
                return exercise, None, f"{exercise.id}: {exc}"

        for start in range(0, total, concurrency):
            self._control_check()
            if not self._solver_injected:
                allowed, reason, details = guard.check(ctx.product.id)
                if not allowed:
                    raise PipelineError(reason)
            batch = todo[start:start + concurrency]
            with ThreadPoolExecutor(max_workers=len(batch), thread_name_prefix="wlj-solve") as pool:
                futures = [pool.submit(work, exercise) for exercise in batch]
                for future in futures:
                    exercise, outcome, error = future.result()
                    done += 1
                    if error:
                        failed += 1
                        ctx.errors.append(error)
                        self.db.update_exercise(exercise.id, status=ExerciseStatus.FAILED)
                    elif outcome and outcome.reused:
                        reused += 1
                    self._progress(
                        ctx,
                        "solve",
                        done / total,
                        f"解答 {done}/{total} 道，失败 {failed} 道",
                    )
            self._write_solutions(ctx)
            self.db.refresh_product_counts(ctx.product.id)
        ctx.stage_details["solve"] = f"完成 {done - failed} 道，复用 {reused} 道，失败 {failed} 道"
        self._write_solutions(ctx)
        log.info("solve %s: %s", ctx.product.sku, ctx.stage_details["solve"])

    def _stage_verify(self, ctx: _RunContext, opts: dict[str, Any]) -> None:
        if not bool(self.settings.get("openai.verify_enabled", True)):
            ctx.warnings.append("独立复核已在设置中关闭，本次解答只能作为草稿，不能进入交付。")
            ctx.stage_details["verify"] = "已跳过（复核未启用）"
            return
        exercises = self.db.list_exercises(ctx.product.id)
        todo: list[tuple[Exercise, Any]] = []
        for exercise in exercises:
            if self._stage_forced("verify", opts):
                solution = self.db.latest_solution(exercise.id)
                if solution:
                    todo.append((exercise, solution))
                continue
            if exercise.status != ExerciseStatus.AI_DRAFT:
                continue
            solution = self.db.latest_solution(exercise.id)
            if solution:
                todo.append((exercise, solution))
        if not todo:
            ctx.stage_details["verify"] = "没有待复核的解答"
            self._progress(ctx, "verify", 1.0, ctx.stage_details["verify"])
            return
        verifier = self._verifier_service()
        total = len(todo)
        done = 0
        verified = 0
        needs_review = 0
        concurrency = max(1, int(self.settings.get("openai.max_concurrency", 4)))

        def work(exercise: Exercise, solution: Any) -> tuple[Exercise, VerifyOutcome | None, str]:
            try:
                outcome = verifier.verify(exercise, ctx.product, solution)
                return exercise, outcome, outcome.error
            except Exception as exc:  # noqa: BLE001
                return exercise, None, f"{exercise.id}: {exc}"

        for start in range(0, total, concurrency):
            self._control_check()
            batch = todo[start:start + concurrency]
            with ThreadPoolExecutor(max_workers=len(batch), thread_name_prefix="wlj-verify") as pool:
                futures = [pool.submit(work, exercise, solution) for exercise, solution in batch]
                for future in futures:
                    exercise, outcome, error = future.result()
                    done += 1
                    if error:
                        needs_review += 1
                        ctx.errors.append(error)
                    elif outcome and outcome.status == ExerciseStatus.VERIFIED:
                        verified += 1
                    else:
                        needs_review += 1
                    self._progress(
                        ctx,
                        "verify",
                        done / total,
                        f"复核 {done}/{total} 道，通过 {verified} 道",
                    )
            self.db.refresh_product_counts(ctx.product.id)
        ctx.stage_details["verify"] = f"通过 {verified} 道，待人工复核 {needs_review} 道"
        log.info("verify %s: %s", ctx.product.sku, ctx.stage_details["verify"])

    def _stage_pdf(self, ctx: _RunContext, opts: dict[str, Any]) -> None:
        force = self._stage_forced("pdf", opts)
        if not force:
            discovered = self._discover_pdfs(ctx)
            if discovered:
                ctx.full_pdf, ctx.quick_pdf = discovered
                ctx.stage_details["pdf"] = "使用已生成的 PDF"
                self._progress(ctx, "pdf", 1.0, ctx.stage_details["pdf"])
                return
        self._control_check()
        self.db.refresh_product_counts(ctx.product.id)
        ctx.product = self.db.get_product(ctx.product.id) or ctx.product
        builder = DocumentBuilder(self.settings, self.db, self.paths)
        self._set_job(ctx.job, message="排版完整解析 PDF")
        full = builder.build(ctx.product, quick=False, strict=False)
        ctx.warnings.extend(full.warnings)
        ctx.full_pdf = self._watermark_build(ctx, full.path, full.build_dir, quick=False)
        self._progress(ctx, "pdf", 0.6, f"完整版 {full.pages} 页，已添加水印")
        quick_path: Path | None = None
        if bool(self.settings.get("latex.quick_answer_edition", True)):
            self._set_job(ctx.job, message="排版答案速查 PDF")
            quick = builder.build(ctx.product, quick=True, strict=False)
            ctx.warnings.extend(quick.warnings)
            quick_path = self._watermark_build(ctx, quick.path, quick.build_dir, quick=True)
            self._progress(ctx, "pdf", 0.95, f"速查版 {quick.pages} 页，已添加水印")
        ctx.quick_pdf = quick_path
        if not ctx.full_pdf or not ctx.full_pdf.exists():
            raise PipelineError("完整解析 PDF 未生成")
        ctx.stage_details["pdf"] = f"完整版 {full.pages} 页" + (
            f"，速查版 {quick.pages} 页" if quick_path else ""
        )
        self._progress(ctx, "pdf", 1.0, ctx.stage_details["pdf"])

    def _stage_images(self, ctx: _RunContext, opts: dict[str, Any]) -> None:
        if not ctx.full_pdf:
            raise PipelineError("尚未生成 PDF，无法生成商品图片")
        generator = ImageGenerator(self.settings, self.db, self.paths)
        deliverables = [ctx.full_pdf] + ([ctx.quick_pdf] if ctx.quick_pdf else [])
        result = generator.generate(
            ctx.product,
            ctx.full_pdf,
            ctx.quick_pdf,
            deliverables=deliverables,
        )
        ctx.images = [Path(path) for path in result.files]
        ctx.warnings.extend(result.warnings)
        ctx.stage_details["images"] = f"{len(ctx.images)} 张商品图片（{result.mode}）"
        self._progress(ctx, "images", 1.0, ctx.stage_details["images"])

    def _stage_records(self, ctx: _RunContext, opts: dict[str, Any]) -> None:
        if not ctx.full_pdf:
            raise PipelineError("尚未生成 PDF，无法生成商品记录")
        manager = ProductManager(self.settings, self.db, self.paths)
        ctx.info = manager.write_all(
            ctx.product,
            ctx.full_pdf,
            ctx.quick_pdf,
            ctx.images,
            uploads=self.db.list_uploads(ctx.product.id),
            acceptance_checks=self._quality_checks(ctx),
        )
        ctx.warnings.extend(ctx.info.warnings)
        ctx.stage_details["records"] = f"商品记录 {len(ctx.info.files)} 个文件"
        self._progress(ctx, "records", 1.0, ctx.stage_details["records"])

    def _stage_upload(self, ctx: _RunContext, opts: dict[str, Any]) -> None:
        if not bool(opts.get("upload")):
            ctx.cloud_state = "disabled"
            ctx.stage_details["upload"] = "未启用云端上传"
            return
        ctx.cloud_state = "requested"
        if not ctx.full_pdf or not ctx.info:
            raise PipelineError("缺少交付文件，无法上传")
        if not self._quality_ready(ctx):
            ctx.cloud_state = "blocked_quality"
            ctx.errors.append("存在未通过复核的题目，按质量门禁未上传云端。")
            ctx.stage_details["upload"] = "质量门禁未通过，未上传"
            return
        netdisk = self._netdisk_client()
        status = netdisk.status()
        if not status.configured:
            ctx.cloud_state = "blocked_config"
            ctx.warnings.append("百度网盘未配置 AppKey/SecretKey/应用名，已跳过上传。")
            ctx.stage_details["upload"] = "未配置百度网盘，已跳过"
            return
        if not status.authorized:
            ctx.cloud_state = "blocked_auth"
            ctx.warnings.append("百度网盘尚未完成 OAuth 授权，已跳过上传。")
            ctx.stage_details["upload"] = "未授权百度网盘，已跳过"
            return
        self._set_job(ctx.job, message="上传交付文件到百度网盘")
        source = ctx.dirs["source"]
        include_source = bool(self.settings.get("netdisk.include_source", False))
        source_file = next((item for item in source.iterdir() if item.is_file()), None) if include_source else None
        result = netdisk.upload_product(
            ctx.product,
            ctx.full_pdf,
            ctx.quick_pdf,
            ctx.images,
            self._info_files(ctx),
            source_file,
            verify=True,
            progress=lambda name, index, total: self._progress(
                ctx, "upload", index / max(1, total), f"上传 {index}/{total}：{name}"
            ),
        )
        ctx.uploads = result.uploads
        ctx.warnings.extend(result.errors)
        if not result.ok:
            ctx.cloud_state = "upload_failed"
            ctx.errors.extend(result.errors)
            ctx.stage_details["upload"] = "上传未全部通过"
            raise PipelineError("百度网盘上传未全部通过：" + "；".join(result.errors[:3]))
        ctx.cloud_state = "uploaded"
        ctx.stage_details["upload"] = f"已上传并核对 {len(ctx.uploads)} 个文件"
        self._progress(ctx, "upload", 1.0, ctx.stage_details["upload"])

    def _stage_share(self, ctx: _RunContext, opts: dict[str, Any]) -> None:
        if not bool(opts.get("share")):
            ctx.stage_details["share"] = "未启用分享链接"
            return
        if ctx.cloud_state != "uploaded":
            ctx.warnings.append("云端上传未完成，已跳过分享链接创建。")
            ctx.stage_details["share"] = "上传未完成，已跳过"
            return
        netdisk = self._netdisk_client()
        self._set_job(ctx.job, message="创建百度网盘分享链接")
        try:
            share = netdisk.create_share(ctx.product)
        except NetdiskError as exc:
            if exc.needs_manual:
                ctx.cloud_state = "share_needs_manual"
                ctx.warnings.append(str(exc))
                ctx.stage_details["share"] = "分享接口不可用，需人工创建"
                return
            raise
        probe = netdisk.probe_share_link(share.url) if share.url else {}
        ctx.share = share
        ctx.cloud_state = "share_created_unverified"
        details = share.details or {}
        details["probe"] = probe
        share = share.model_copy(update={"details": details})
        ctx.share = share
        ctx.stage_details["share"] = "链接已创建，等待人工确认提取码与下载权限"
        self._progress(ctx, "share", 1.0, ctx.stage_details["share"])

    def _stage_cleanup(self, ctx: _RunContext, opts: dict[str, Any]) -> None:
        if not bool(opts.get("cleanup")):
            ctx.stage_details["cleanup"] = "未启用自动清理"
            return
        manager = CleanupManager(self.settings, self.db, self.paths)
        eligibility = manager.evaluate(ctx.product.id)
        if not eligibility.eligible:
            ctx.stage_details["cleanup"] = "清理条件未满足：" + "；".join(eligibility.blockers[:3])
            ctx.warnings.append(ctx.stage_details["cleanup"])
            return
        staged = manager.stage(ctx.product.id)
        ctx.stage_details["cleanup"] = (
            f"已移入待清理目录，{staged.files} 个文件，"
            f"{staged.purge_after} 后可确认删除"
        )

    # ------------------------------------------------------------------ helpers

    def _solver_service(self) -> SolverService:
        if self._solver is not None:
            return self._solver
        if self.ai is None:
            self.ai = AiClient(self.settings, self._secret_store(), self.db)
        ok, reason = self.ai.configured()
        if not ok:
            raise PipelineError(f"OpenAI API 未配置：{reason}")
        self._solver = SolverService(self.settings, self.db, self.ai)
        return self._solver

    def _verifier_service(self) -> AnswerVerifier:
        if self._verifier is not None:
            return self._verifier
        if self.ai is None:
            self.ai = AiClient(self.settings, self._secret_store(), self.db)
        ok, reason = self.ai.configured()
        if not ok:
            raise PipelineError(f"OpenAI API 未配置：{reason}")
        self._verifier = AnswerVerifier(self.settings, self.db, self.ai)
        return self._verifier

    def _secret_store(self):
        from .secrets import SecretStore

        return SecretStore(self.paths.secrets)

    def _netdisk_client(self) -> BaiduNetdisk:
        if self._netdisk is None:
            self._netdisk = BaiduNetdisk(
                self.settings,
                self._secret_store(),
                self.db,
                paths=self.paths,
            )
        return self._netdisk

    def _ensure_source_copy(self, ctx: _RunContext) -> Path:
        source = Path(ctx.product.source_path)
        if not source.exists():
            raise PipelineError(f"原始教材不存在：{source}")
        target_dir = ctx.dirs["source"]
        target = target_dir / sanitize_filename(source.name or "textbook.pdf")
        if source.resolve() != target.resolve():
            if target.exists() and target.stat().st_size == source.stat().st_size:
                if sha256_file(target) == sha256_file(source):
                    pass
                else:
                    target = target.with_name(f"{target.stem}_{int(time.time())}{target.suffix}")
            if not target.exists():
                shutil.copy2(source, target)
        if not target.exists():
            raise PipelineError("无法保存原始教材副本")
        self.db.update_product(ctx.product.id, source_path=str(target), source_sha256=sha256_file(target))
        ctx.product = self.db.get_product(ctx.product.id) or ctx.product
        return target

    def _load_context(self, ctx: _RunContext) -> None:
        lines_file = ctx.dirs["parsed"] / "lines.jsonl"
        if not ctx.lines and lines_file.exists():
            ctx.lines = self._load_lines(lines_file)
        if not ctx.toc:
            document = ctx.dirs["parsed"] / "document.json"
            if document.exists():
                try:
                    ctx.toc = json.loads(document.read_text(encoding="utf-8")).get("toc", []) or []
                except (OSError, ValueError):
                    ctx.toc = []

    @staticmethod
    def _load_lines(path: Path) -> list[TextLine]:
        lines: list[TextLine] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            try:
                item = json.loads(raw)
                bbox = tuple(float(value) for value in item.get("bbox", (0, 0, 0, 0)))
                if len(bbox) != 4:
                    bbox = (0.0, 0.0, 0.0, 0.0)
                lines.append(
                    TextLine(
                        page=int(item.get("page", 1)),
                        text=str(item.get("text", "")),
                        bbox=bbox,  # type: ignore[arg-type]
                        size=float(item.get("size", 0) or 0),
                        bold=bool(item.get("bold", False)),
                        source=str(item.get("source", "text")),
                    )
                )
            except (ValueError, TypeError):
                continue
        return lines

    def _watermark_build(self, ctx: _RunContext, raw: Path, build_dir: Path | None, *, quick: bool) -> Path:
        if not raw.exists():
            raise PipelineError("排版输出不存在")
        target = raw
        staging = (build_dir or ctx.dirs["parsed"]) / ("unwatermarked_quick.pdf" if quick else "unwatermarked_full.pdf")
        staging.parent.mkdir(parents=True, exist_ok=True)
        if staging.exists():
            staging.unlink()
        shutil.move(str(raw), str(staging))
        try:
            result = Watermarker(self.settings).apply(staging, target)
        except WatermarkError as exc:
            shutil.move(str(staging), str(raw))
            raise PipelineError(f"水印添加失败：{exc}") from exc
        if not result.path.exists() or result.path.stat().st_size == 0:
            raise PipelineError("水印 PDF 生成失败")
        return result.path

    def _discover_outputs(self, ctx: _RunContext) -> None:
        pdfs = self._discover_pdfs(ctx)
        if pdfs:
            ctx.full_pdf, ctx.quick_pdf = pdfs

    def _discover_pdfs(self, ctx: _RunContext) -> tuple[Path, Path | None] | None:
        folder = ctx.dirs["pdf"]
        full = sorted(folder.glob("*_习题详解_*.pdf"), key=lambda item: item.stat().st_mtime, reverse=True)
        quick = sorted(folder.glob("*_答案速查版_*.pdf"), key=lambda item: item.stat().st_mtime, reverse=True)
        if not full:
            return None
        return full[0], (quick[0] if quick else None)

    def _quality_ready(self, ctx: _RunContext) -> bool:
        counts = self.db.exercise_status_counts(ctx.product.id)
        total = sum(counts.values())
        good = counts.get(ExerciseStatus.VERIFIED.value, 0) + counts.get(ExerciseStatus.APPROVED.value, 0)
        bad = counts.get(ExerciseStatus.NEEDS_REVIEW.value, 0) + counts.get(ExerciseStatus.FAILED.value, 0)
        return total > 0 and good == total and bad == 0

    def _quality_checks(self, ctx: _RunContext) -> list[dict[str, Any]]:
        counts = self.db.exercise_status_counts(ctx.product.id)
        total = sum(counts.values())
        good = counts.get(ExerciseStatus.VERIFIED.value, 0) + counts.get(ExerciseStatus.APPROVED.value, 0)
        review = counts.get(ExerciseStatus.NEEDS_REVIEW.value, 0)
        failed = counts.get(ExerciseStatus.FAILED.value, 0)
        checks = [
            {
                "name": "习题复核",
                "status": "pass" if total and good == total else "fail",
                "detail": f"共 {total} 道，已通过 {good} 道，待复核 {review} 道，失败 {failed} 道",
            },
            {
                "name": "水印与排版",
                "status": "pass" if ctx.full_pdf and ctx.full_pdf.exists() else "fail",
                "detail": str(ctx.full_pdf or "未生成"),
            },
        ]
        return checks

    def _info_files(self, ctx: _RunContext) -> list[Path]:
        if not ctx.info:
            return []
        return [
            ctx.info.info_json,
            ctx.info.info_markdown,
            ctx.info.file_list,
            ctx.info.delivery_text,
            ctx.info.acceptance,
        ]

    def _write_solutions(self, ctx: _RunContext) -> None:
        exercises = self.db.list_exercises(ctx.product.id)
        payload: list[dict[str, Any]] = []
        for exercise in exercises:
            solution = self.db.latest_solution(exercise.id)
            verification = self.db.latest_verification(exercise.id)
            payload.append(
                {
                    "exercise_id": exercise.id,
                    "number": exercise.number,
                    "chapter": exercise.raw.get("chapter_title", ""),
                    "status": exercise.status.value,
                    "solution": solution.model_dump(mode="json") if solution else None,
                    "verification": verification.model_dump(mode="json") if verification else None,
                }
            )
        self._write_json(ctx.dirs["answers"] / "solutions.json", payload)

    def _finish(self, ctx: _RunContext, opts: dict[str, Any]) -> None:
        counts = self.db.exercise_status_counts(ctx.product.id)
        total = sum(counts.values())
        good = counts.get(ExerciseStatus.VERIFIED.value, 0) + counts.get(ExerciseStatus.APPROVED.value, 0)
        review = counts.get(ExerciseStatus.NEEDS_REVIEW.value, 0)
        failed = counts.get(ExerciseStatus.FAILED.value, 0)
        if ctx.uploads:
            self._rewrite_records(ctx)
            self._reupload_info(ctx)
        share_verified = bool(ctx.share and ctx.share.verified)
        if failed or review:
            status = "needs_review" if review else "failed"
        elif share_verified:
            status = "completed"
        elif ctx.cloud_state in ("share_created_unverified", "share_needs_manual"):
            status = "awaiting_verification"
        elif ctx.cloud_state in ("blocked_config", "blocked_auth", "blocked_quality", "upload_failed"):
            status = "awaiting_cloud"
        else:
            status = "packaged"
        if status == "completed" and good != total:
            status = "needs_review"
        self.db.update_product(ctx.product.id, status=status)
        self.db.refresh_product_counts(ctx.product.id)
        message = self._final_message(ctx, status, total, good, review, failed)
        self._set_job(
            ctx.job,
            status=JobStatus.COMPLETED,
            message=message,
            current_stage="done",
            progress=1.0,
            finished_at=utc_now(),
            error="" if not ctx.errors else "；".join(ctx.errors[:5]),
        )
        ctx.stage_details["done"] = message
        self._write_report(ctx)
        self._save_state(ctx)
        log.info("pipeline complete %s: %s", ctx.product.sku, message)

    def _rewrite_records(self, ctx: _RunContext) -> None:
        if not ctx.full_pdf:
            return
        manager = ProductManager(self.settings, self.db, self.paths)
        try:
            ctx.info = manager.write_all(
                ctx.product,
                ctx.full_pdf,
                ctx.quick_pdf,
                ctx.images,
                share=ctx.share,
                uploads=self.db.list_uploads(ctx.product.id),
                acceptance_checks=self._quality_checks(ctx),
            )
        except Exception as exc:  # noqa: BLE001
            ctx.warnings.append(f"更新交付记录失败：{exc}")

    def _reupload_info(self, ctx: _RunContext) -> None:
        """Refresh cloud copies of the delivery records after upload/share state changes."""
        if not ctx.info or not ctx.full_pdf or not ctx.uploads:
            return
        try:
            netdisk = self._netdisk_client()
            status = netdisk.status()
            if not status.configured or not status.authorized:
                return
            result = netdisk.upload_product(
                ctx.product,
                ctx.full_pdf,
                ctx.quick_pdf,
                ctx.images,
                self._info_files(ctx),
                None,
                verify=True,
            )
            if result.uploads:
                ctx.uploads = result.uploads
            if not result.ok:
                ctx.warnings.append("交付记录更新后未能全部同步到云端：" + "；".join(result.errors[:3]))
        except Exception as exc:  # noqa: BLE001
            ctx.warnings.append(f"交付记录云同步失败：{exc}")

    def _final_message(
        self,
        ctx: _RunContext,
        status: str,
        total: int,
        good: int,
        review: int,
        failed: int,
    ) -> str:
        if status == "completed":
            return f"商品已完成，{total} 道题全部复核，分享链接已验证"
        if status == "needs_review":
            return f"本地商品包已生成，{total} 道题中 {good} 道通过，{review} 道待人工复核"
        if status == "failed":
            return f"生产未完成，{failed} 道题失败，请查看日志"
        if status == "awaiting_verification":
            return "本地商品包与云端文件已完成，分享链接待人工确认"
        if status == "awaiting_cloud":
            return "本地商品包已完成，云端步骤被配置或质量门禁阻止"
        return f"本地商品包已完成（{total} 道题，{good} 道复核通过），云端未启用"

    def _pause_job(self, ctx: _RunContext, reason: str) -> None:
        self._set_job(
            ctx.job,
            status=JobStatus.PAUSED,
            message=f"任务已暂停：{reason}",
            error="",
            finished_at="",
        )
        self.db.update_product(ctx.product.id, status="paused")
        for stage, state in ctx.job.stage_status.items():
            if state == "running":
                ctx.job.stage_status[stage] = "waiting"
        self._set_job(ctx.job, stage_status=ctx.job.stage_status)

    def _write_report(self, ctx: _RunContext) -> None:
        try:
            product = self.db.get_product(ctx.product.id) or ctx.product
            report = {
                "generated_at": utc_now(),
                "job": self.db.get_job(ctx.job.id).model_dump(mode="json") if self.db.get_job(ctx.job.id) else {},
                "product": product.model_dump(mode="json"),
                "stage_details": ctx.stage_details,
                "warnings": ctx.warnings,
                "errors": ctx.errors,
                "cloud_state": ctx.cloud_state,
                "usage": self.db.usage_for_product(product.id),
                "files": {
                    "full_pdf": str(ctx.full_pdf) if ctx.full_pdf else "",
                    "quick_pdf": str(ctx.quick_pdf) if ctx.quick_pdf else "",
                    "images": [str(path) for path in ctx.images],
                },
                "uploads": [item.model_dump(mode="json") for item in self.db.list_uploads(product.id)],
                "share": self.db.latest_share(product.id).model_dump(mode="json")
                if self.db.latest_share(product.id)
                else None,
            }
            path = ctx.dirs["delivery"] / "生产报告.json"
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - a report failure must not erase the product
            log.warning("write production report failed: %s", exc)

    def _save_state(self, ctx: _RunContext) -> None:
        state = {
            "job_id": ctx.job.id,
            "stage_status": ctx.job.stage_status,
            "full_pdf": str(ctx.full_pdf) if ctx.full_pdf else "",
            "quick_pdf": str(ctx.quick_pdf) if ctx.quick_pdf else "",
            "images": [str(path) for path in ctx.images],
            "cloud_state": ctx.cloud_state,
            "updated_at": utc_now(),
        }
        try:
            (ctx.dirs["parsed"] / "pipeline_state.json").write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("save pipeline state failed: %s", exc)

    def _normalise_options(self, options: dict[str, Any]) -> dict[str, Any]:
        opts = dict(options or {})
        opts.setdefault("upload", bool(self.settings.get("netdisk.auto_upload", False)))
        opts.setdefault("share", bool(self.settings.get("netdisk.auto_share", False)))
        opts.setdefault("cleanup", bool(self.settings.get("cleanup.enabled", False)))
        opts.setdefault("force", [])
        if opts.get("force") is True:
            opts["force"] = list(STAGES)
        if isinstance(opts.get("force"), str):
            opts["force"] = [opts["force"]]
        return opts

    def _enabled_stages(self, opts: dict[str, Any]) -> set[str]:
        enabled = set(STAGES)
        if not opts.get("upload"):
            enabled.discard("upload")
            enabled.discard("share")
        if not opts.get("share"):
            enabled.discard("share")
        if not opts.get("cleanup"):
            enabled.discard("cleanup")
        return enabled

    @staticmethod
    def _stage_forced(stage: str, opts: dict[str, Any]) -> bool:
        force = opts.get("force", [])
        return bool(opts.get("force_all")) or stage in force

    def _control_check(self) -> None:
        control = getattr(self, "_control", None)
        if control is not None:
            control.check()

    def _control_cancelled(self) -> bool:
        control = getattr(self, "_control", None)
        return bool(control and control.cancelled)

    def _set_job(self, job: Job, **fields: Any) -> None:
        for key, value in fields.items():
            setattr(job, key, value)
        self.db.update_job(job.id, **fields)

    def _mark_stage(self, job: Job, stage: str, status: str, detail: str = "") -> None:
        job.stage_status[stage] = status
        if status == "running":
            job.current_stage = stage
        self.db.update_job(job.id, stage_status=job.stage_status, current_stage=job.current_stage)
        if detail:
            log.info("stage %s %s: %s", stage, status, detail)

    def _progress(self, ctx: _RunContext, stage: str, fraction: float, message: str) -> None:
        base = 0.0
        for name in STAGES:
            if name == stage:
                break
            base += STAGE_WEIGHTS.get(name, 0.0)
        value = min(0.99, base + STAGE_WEIGHTS.get(stage, 0.0) * max(0.0, min(1.0, fraction)))
        self._set_job(
            ctx.job,
            progress=round(value, 4),
            message=message,
            current_stage=stage,
        )
        if self._on_progress is not None:
            try:
                self._on_progress(
                    {
                        "job_id": ctx.job.id,
                        "product_id": ctx.product.id,
                        "stage": stage,
                        "fraction": fraction,
                        "progress": round(value, 4),
                        "message": message,
                        "stage_status": dict(ctx.job.stage_status),
                    }
                )
            except Exception:  # noqa: BLE001 - a UI callback must never stop production
                log.debug("progress callback failed", exc_info=True)

    @staticmethod
    def _stable_id(prefix: str, product_id: str, suffix: str) -> str:
        clean = "".join(ch for ch in suffix if ch.isalnum() or ch in "-_")
        return f"{prefix}_{product_id[-8:]}_{clean}"[:80]

    @staticmethod
    def _exercise_id(sku: str, chapter_number: int, local: int) -> str:
        clean = "".join(ch for ch in (sku or "BOOK") if ch.isalnum() or ch in "-_").upper()
        return f"{clean}-CH{int(chapter_number):02d}-Q{int(local):03d}"

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_pipeline(
    settings: SettingsManager,
    db: Database,
    paths: AppPaths,
    product_id: str,
    options: dict[str, Any] | None = None,
    *,
    control: PipelineControl | None = None,
    ai: AiClient | None = None,
    solver: SolverService | None = None,
    verifier: AnswerVerifier | None = None,
    netdisk: BaiduNetdisk | None = None,
) -> PipelineResult:
    pipeline = Pipeline(
        settings,
        db,
        paths,
        ai=ai,
        solver=solver,
        verifier=verifier,
        netdisk=netdisk,
    )
    return pipeline.start(product_id, options, control=control)
