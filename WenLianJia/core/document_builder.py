"""Build the final exercise-solution PDF with XeLaTeX.

The builder is deliberately strict: a final deliverable is only produced when
every detected exercise has a solution with a non-empty final answer.  Draft
previews can opt into ``strict=False`` and receive warnings instead.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from PIL import Image

from models.schemas import (
    Chapter,
    Exercise,
    ExerciseStatus,
    Product,
    Solution,
    utc_now,
)
from .config import SettingsManager
from .db import Database
from .latex_utils import (
    escape_latex,
    find_xelatex,
    latex_display,
    latex_inline_answer,
    latex_multiline,
    latex_text,
    sanitize_filename,
    xelatex_version,
)
from .logging_setup import get_logger
from .paths import PROJECT_ROOT, AppPaths

logger = get_logger("document")


class DocumentBuildError(RuntimeError):
    pass


@dataclass
class DocumentBuildResult:
    path: Path
    quick: bool
    pages: int
    characters: int
    chinese_characters: int
    warnings: list[str] = field(default_factory=list)
    build_dir: Path | None = None
    log_path: Path | None = None
    duration_s: float = 0.0


@dataclass
class _ExerciseBundle:
    exercise: Exercise
    chapter: Chapter | None
    solution: Solution


class DocumentBuilder:
    def __init__(self, settings: SettingsManager, db: Database, paths: AppPaths):
        self.settings = settings
        self.db = db
        self.paths = paths
        self.env = Environment(
            loader=FileSystemLoader(str(PROJECT_ROOT / "templates" / "latex")),
            autoescape=False,
            undefined=StrictUndefined,
            trim_blocks=False,
            lstrip_blocks=False,
        )

    def build(
        self,
        product: Product,
        *,
        quick: bool = False,
        strict: bool = True,
    ) -> DocumentBuildResult:
        started = time.time()
        chapters = self.db.list_chapters(product.id)
        exercises = self.db.list_exercises(product.id)
        if not exercises:
            raise DocumentBuildError("没有可排版的习题，请先完成习题识别")
        bundles: list[_ExerciseBundle] = []
        chapter_map = {chapter.id: chapter for chapter in chapters}
        missing: list[Exercise] = []
        for exercise in exercises:
            solution = self.db.latest_solution(exercise.id)
            if solution is None or not solution.content.final_answer.strip():
                missing.append(exercise)
                continue
            bundles.append(
                _ExerciseBundle(
                    exercise=exercise,
                    chapter=chapter_map.get(exercise.chapter_id),
                    solution=solution,
                )
            )
        if missing and strict:
            first = "、".join(exercise.label or exercise.number or exercise.id for exercise in missing[:8])
            raise DocumentBuildError(
                f"{len(missing)} 道题缺少最终答案，不能生成正式 PDF：{first}"
            )
        warnings: list[str] = []
        if missing:
            warnings.append(f"{len(missing)} 道题缺少答案，已在草稿中跳过")

        product_dir = self.paths.product_dir(product.sku, product.title)
        dirs = self.paths.product_subdirs(product_dir)
        build_dir = dirs["pdf"] / ("build_quick" if quick else "build_full")
        build_dir.mkdir(parents=True, exist_ok=True)
        assets_dir = build_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        for old in build_dir.glob("*.tex"):
            old.unlink(missing_ok=True)

        body, body_warnings = self._render_body(
            product, bundles, assets_dir, quick=quick
        )
        warnings.extend(body_warnings)
        cover = self._render_cover(product, len(bundles), quick=quick)
        template = "quick_answers.tex.j2" if quick else "book.tex.j2"
        title = product.source_title or product.title
        context = {
            "margin_mm": int(self.settings.get("latex.margin_mm", 22)),
            "font_main": str(self.settings.get("latex.font_main", "Microsoft YaHei")),
            "font_latin": str(self.settings.get("latex.font_latin", "Cambria")),
            "font_song": str(self.settings.get("latex.font_song", "SimSun")),
            "font_hei": str(self.settings.get("latex.font_hei", "SimHei")),
            "header_left": latex_text(f"{title} · 习题详解"),
            "header_right": latex_text("全网同名爱做题的大肥鱼"),
            "pdf_title": latex_text(f"{title} 习题答案与详细解析"),
            "pdf_author": latex_text(str(self.settings.get("app.brand", "爱做题的大肥鱼"))),
            "cover": cover,
            "body": body,
            "toc": bool(self.settings.get("latex.toc", True)) and not quick,
        }
        tex_text = self.env.get_template(template).render(**context)
        tex_path = build_dir / ("quick_answers.tex" if quick else "exercise_book.tex")
        tex_path.write_text(tex_text, encoding="utf-8")

        pdf_path, compile_log = self._compile(tex_path, build_dir, warnings)
        pdf_path = self._promote(product, pdf_path, quick)
        pages, characters, chinese = self._inspect_pdf(pdf_path, warnings)
        duration = time.time() - started
        logger.info(
            "built %s PDF %s: %d pages, %d characters in %.1fs",
            "quick" if quick else "full",
            pdf_path,
            pages,
            characters,
            duration,
        )
        return DocumentBuildResult(
            path=pdf_path,
            quick=quick,
            pages=pages,
            characters=characters,
            chinese_characters=chinese,
            warnings=warnings,
            build_dir=build_dir,
            log_path=compile_log,
            duration_s=round(duration, 2),
        )

    # ------------------------------------------------------------------ body

    def _render_body(
        self,
        product: Product,
        bundles: list[_ExerciseBundle],
        assets_dir: Path,
        *,
        quick: bool,
    ) -> tuple[str, list[str]]:
        warnings: list[str] = []
        if quick:
            return self._render_quick_body(product, bundles, warnings), warnings
        grouped: dict[str, list[_ExerciseBundle]] = {}
        order: list[str] = []
        for bundle in bundles:
            key = bundle.chapter.id if bundle.chapter else "__none__"
            if key not in grouped:
                grouped[key] = []
                order.append(key)
            grouped[key].append(bundle)

        chunks: list[str] = []
        chapter_number = 0
        for key in order:
            items = grouped[key]
            chapter = items[0].chapter
            chapter_number += 1
            title = chapter.title if chapter else "未归属章节"
            display = self._chapter_display(chapter, chapter_number)
            chunks.append(
                "\\chapter*{" + latex_text(display) + "}\n"
                "\\addcontentsline{toc}{chapter}{" + latex_text(display) + "}\n"
                "\\markboth{" + latex_text(display) + "}{" + latex_text(display) + "}\n"
                "\\thispagestyle{fancy}\n"
            )
            chunks.append(
                "\\noindent\\textcolor{gray}{本章共 "
                + str(len(items))
                + " 道习题。}\\par\\vspace{6pt}\n"
            )
            chunks.append(
                "\\noindent\\textcolor{brandcolor}{\\rule{\\linewidth}{0.8pt}}\\par\\vspace{10pt}\n"
            )
            for index, bundle in enumerate(items, start=1):
                chunks.append(self._render_exercise(bundle, index, assets_dir, warnings))
        return "\n\n".join(chunks), warnings

    def _render_quick_body(
        self,
        product: Product,
        bundles: list[_ExerciseBundle],
        warnings: list[str],
    ) -> str:
        chunks = [
            "\\chapter*{答案速查}",
            "\\addcontentsline{toc}{chapter}{答案速查}",
            "\\markboth{答案速查}{答案速查}",
            "\\thispagestyle{fancy}",
            "\\noindent 本册仅列出每道题的最终答案。完整推导、考点与易错点请查阅《"
            + latex_text(product.source_title or product.title)
            + " 习题答案与详细解析》。\\par\\vspace{10pt}",
        ]
        current_chapter = None
        rows: list[str] = []

        def flush() -> None:
            if not rows:
                return
            chunks.append(
                "\\begin{longtable}{@{}p{0.14\\linewidth}p{0.24\\linewidth}p{0.54\\linewidth}@{}}\n"
                "\\toprule\n\\textbf{题号} & \\textbf{题型} & \\textbf{最终答案}\\\\\n"
                "\\midrule\n\\endhead\n" + "\n".join(rows) + "\n\\bottomrule\n\\end{longtable}\n"
            )
            rows.clear()

        for bundle in bundles:
            chapter_title = (
                self._chapter_display(bundle.chapter, 0)
                if bundle.chapter
                else "未归属章节"
            )
            if chapter_title != current_chapter:
                flush()
                current_chapter = chapter_title
                chunks.append(
                    "\\subsection*{" + latex_text(chapter_title) + "}\n"
                )
            answer = bundle.solution.content.final_answer.strip()
            if len(answer) > 220:
                answer = answer[:220] + "……"
                warnings.append(f"{bundle.exercise.id} 的速查答案过长，已截断")
            rows.append(
                latex_text(bundle.exercise.label or bundle.exercise.number or "")
                + " & "
                + latex_text(self._type_label(bundle.exercise.exercise_type.value))
                + " & "
                + latex_inline_answer(answer)
                + " \\\\\n\\addlinespace[3pt]\n"
            )
        flush()
        return "\n".join(chunks)

    def _render_exercise(
        self,
        bundle: _ExerciseBundle,
        index: int,
        assets_dir: Path,
        warnings: list[str],
    ) -> str:
        exercise = bundle.exercise
        solution = bundle.solution
        content = solution.content
        heading = f"第 {index} 题"
        parts = [
            "\\needspace{6\\baselineskip}",
            "\\subsection*{" + latex_text(heading) + "}",
            "\\addcontentsline{toc}{subsection}{" + latex_text(heading) + "}",
            "\\noindent\\textcolor{gray}{\\small 原书页码："
            + latex_text(str(exercise.page_start or "?"))
            + " \\quad 题型："
            + latex_text(self._type_label(exercise.exercise_type.value))
            + " \\quad 状态："
            + latex_text(self._status_label(solution.status.value))
            + "}\\par\\vspace{4pt}",
        ]
        parts.append("\\noindent\\textbf{【题目】}\\par")
        question_body = re.sub(r"^\s*\d+\s*[.、]\s*", "", exercise.text, count=1).strip()
        parts.append("\\begin{quote}\n" + latex_multiline(question_body) + "\n\\end{quote}")
        if exercise.latex.strip():
            parts.append("\\noindent\\textbf{【题目公式】}\\par")
            parts.append(latex_display(exercise.latex))
        image_paths = self._prepare_images(exercise, assets_dir, warnings)
        for number, image_path in enumerate(image_paths, start=1):
            parts.append(
                "\\begin{center}\n\\includegraphics[width=0.72\\linewidth,height=0.30\\textheight,"
                "keepaspectratio]{" + image_path + "}\\\\\n"
                "\\textcolor{gray}{\\small 题图 " + str(number) + "（原书第 "
                + latex_text(str(exercise.page_start or "?")) + " 页）}\n\\end{center}"
            )
        if content.needs_figure and not image_paths:
            warnings.append(f"{exercise.id} 标注依赖图形，但没有可用题图")
            parts.append(
                "\\noindent\\textcolor{red}{\\small 注意：本题依赖题图，自动识别未获得清晰图像，请核对原书。}\\par"
            )
        if content.analysis.strip():
            parts.append("\\noindent\\textbf{【考点分析】}\\par\n" + latex_multiline(content.analysis))
        if content.knowledge_points:
            points = "；".join(content.knowledge_points[:8])
            parts.append("\\noindent\\textbf{【核心知识点】}\\par\n" + latex_text(points))
        if content.approach.strip():
            parts.append("\\noindent\\textbf{【解题思路】}\\par\n" + latex_multiline(content.approach))
        parts.append("\\noindent\\textbf{【详细解答】}\\par")
        if content.detailed_steps:
            steps = "\n".join(
                "\\item " + latex_multiline(step) for step in content.detailed_steps
            )
            parts.append("\\begin{enumerate}[label=\\arabic*.,leftmargin=2.4em]\n" + steps + "\n\\end{enumerate}")
        else:
            warning = "本题没有详细步骤，已按草稿处理。"
            warnings.append(f"{exercise.id} 没有详细解答步骤")
            parts.append("\\textcolor{red}{" + latex_text(warning) + "}")
        parts.append("\\par\\vspace{4pt}\\needspace{5\\baselineskip}\\noindent\\textcolor{brandcolor}{\\rule{\\linewidth}{0.6pt}}")
        parts.append("\\noindent\\textbf{【最终答案】}\\par")
        parts.append(latex_display(content.final_answer or "（空）"))
        parts.append("\\noindent\\textcolor{brandcolor}{\\rule{\\linewidth}{0.6pt}}\\par\\vspace{6pt}")
        if content.method_summary.strip():
            parts.append("\\noindent\\textbf{【方法总结】}\\par\n" + latex_multiline(content.method_summary))
        if content.pitfalls:
            parts.append("\\noindent\\textbf{【常见错误与注意事项】}\\par")
            items = "\n".join("\\item " + latex_multiline(item) for item in content.pitfalls)
            parts.append("\\begin{itemize}[leftmargin=2.2em,itemsep=3pt]\n" + items + "\n\\end{itemize}")
        parts.append("\\vspace{10pt}\n\\noindent\\textcolor{rulegray}{\\rule{\\linewidth}{0.4pt}}\\vspace{12pt}")
        return "\n\n".join(parts)

    # ----------------------------------------------------------------- cover

    def _render_cover(self, product: Product, count: int, *, quick: bool) -> str:
        brand = str(self.settings.get("app.brand", "全网同名爱做题的大肥鱼"))
        source = product.source_title or product.title
        edition = "答案速查版" if quick else "习题答案与详细解析"
        today = date.today().isoformat()
        stats = (
            f"共 {product.chapter_count or 0} 章 · 收录 {count} 道习题 · "
            f"通过复核 {product.verified_count or 0} 道"
        )
        return "\n".join(
            [
                "\\begin{titlepage}",
                "\\thispagestyle{empty}",
                "\\centering",
                "\\vspace*{2.2cm}",
                "{\\color{brandcolor}\\rule{\\linewidth}{3pt}}\\par",
                "\\vspace{1.6cm}",
                "{\\Huge\\bfseries " + latex_text(source) + "\\par}",
                "\\vspace{0.9cm}",
                "{\\LARGE\\bfseries " + latex_text(edition) + "\\par}",
                "\\vspace{0.6cm}",
                "{\\large\\color{gray} " + latex_text(stats) + "\\par}",
                (
                    "\\vspace{0.35cm}\n{\\small\\color{gray} "
                    + latex_text(str(product.metadata.get("cover_note", "")))
                    + "\\par}"
                    if product.metadata.get("cover_note")
                    else ""
                ),
                "\\vfill",
                "{\\large 整理品牌：\\textbf{" + latex_text(brand) + "}\\par}",
                "\\vspace{0.4cm}",
                "{\\large 版本：v" + latex_text(product.version or "1.0") + "\\quad 生成日期：" + today + "\\par}",
                "\\vspace{0.8cm}",
                "{\\small\\color{gray} 本资料为原创解析作品，仅供学习参考。\\par}",
                "\\vspace{0.6cm}",
                "{\\color{brandcolor}\\rule{\\linewidth}{1pt}}",
                "\\end{titlepage}",
            ]
        )

    # -------------------------------------------------------------- compile

    def _compile(
        self, tex_path: Path, build_dir: Path, warnings: list[str]
    ) -> tuple[Path, Path]:
        exe = find_xelatex(self.settings)
        timeout = int(self.settings.get("latex.compile_timeout", 240))
        log_path = tex_path.with_suffix(".log")
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        command = [
            str(exe),
            "-interaction=nonstopmode",
            "-halt-on-error",
            "-file-line-error",
            tex_path.name,
        ]
        runs = 2
        for run in range(runs):
            result = subprocess.run(
                command,
                cwd=str(build_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=env,
            )
            log_text = ""
            if log_path.exists():
                log_text = log_path.read_text(encoding="utf-8", errors="replace")
            if result.returncode != 0:
                errors = self._latex_errors(log_text or (result.stdout or ""))
                raise DocumentBuildError(
                    "XeLaTeX 编译失败：\n"
                    + "\n".join(errors[:12])
                    + f"\n完整日志：{log_path}"
                )
            if run == 0:
                rerun = "Rerun to get cross-references right" in log_text
                if not rerun and "There were undefined references" not in log_text:
                    # Still run twice: the TOC and LastPage reference need it.
                    pass
        if not log_path.exists():
            warnings.append("未找到 LaTeX 日志文件")
        else:
            text = log_path.read_text(encoding="utf-8", errors="replace")
            missing = sorted(set(re.findall(r"Missing character: There is no (.+?) in font", text)))
            if missing:
                warnings.append("字体缺字：" + "、".join(missing[:10]))
            overfull = len(re.findall(r"Overfull \\hbox", text))
            if overfull:
                warnings.append(f"存在 {overfull} 处排版溢出（Overfull hbox）")
        pdf_path = tex_path.with_suffix(".pdf")
        if not pdf_path.exists():
            raise DocumentBuildError("XeLaTeX 未生成 PDF 文件")
        return pdf_path, log_path

    @staticmethod
    def _latex_errors(text: str) -> list[str]:
        lines: list[str] = []
        for line in text.splitlines():
            if line.startswith("!") or "LaTeX Error" in line or line.startswith("./") and ": " in line:
                lines.append(line.strip())
        return lines or [text.strip()[-1200:]]

    def _promote(self, product: Product, pdf_path: Path, quick: bool) -> Path:
        product_dir = self.paths.product_dir(product.sku, product.title)
        target_dir = self.paths.product_subdirs(product_dir)["pdf"]
        target_dir.mkdir(parents=True, exist_ok=True)
        base = sanitize_filename(product.sku + "_" + (product.source_title or product.title))
        suffix = "答案速查版" if quick else "习题详解"
        target = target_dir / f"{base}_{suffix}_v{product.version or '1.0'}.pdf"
        if target.exists():
            stamp = time.strftime("%Y%m%d_%H%M%S")
            target = target_dir / f"{base}_{suffix}_v{product.version or '1.0'}_{stamp}.pdf"
        shutil.copy2(pdf_path, target)
        return target

    @staticmethod
    def _inspect_pdf(path: Path, warnings: list[str]) -> tuple[int, int, int]:
        try:
            document = fitz.open(path)
        except Exception as exc:  # noqa: BLE001
            raise DocumentBuildError(f"生成的 PDF 无法打开：{exc}") from exc
        try:
            text = "".join(page.get_text() for page in document)
            pages = document.page_count
        finally:
            document.close()
        chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
        characters = len(text)
        if pages < 2:
            warnings.append("生成的 PDF 页数异常偏少")
        if chinese < 50:
            warnings.append("PDF 中文字符过少，可能出现字体或编码问题")
        if "????" in text:
            warnings.append("PDF 文本中存在问号乱码，请检查字体")
        return pages, characters, chinese

    @staticmethod
    def _chapter_display(chapter: Chapter, fallback_number: int) -> str:
        if chapter is None:
            return f"第 {fallback_number} 章"
        title = chapter.title.strip()
        matched = re.match(r"^(第\s*[0-9一二三四五六七八九十百]+\s*章)\s*", title)
        if matched:
            return matched.group(1) + "\u3000" + title[matched.end():].strip()
        number = chapter.number or fallback_number
        return f"第 {number} 章\u3000{title}".strip()

    @staticmethod
    def _type_label(value: str) -> str:
        return {
            "calculation": "计算题",
            "proof": "证明题",
            "choice": "选择题",
            "fill_blank": "填空题",
            "short_answer": "简答题",
            "programming": "编程题",
            "experiment": "实验题",
            "unknown": "未分类",
        }.get(str(value), str(value))

    @staticmethod
    def _status_label(value: str) -> str:
        return {
            "pending": "待处理",
            "solving": "解答中",
            "ai_draft": "AI 初稿",
            "verified": "已自动复核",
            "needs_review": "待人工复核",
            "failed": "处理失败",
            "approved": "已人工确认",
        }.get(str(value), str(value))

    @staticmethod
    def _prepare_images(
        exercise: Exercise, assets_dir: Path, warnings: list[str]
    ) -> list[str]:
        paths: list[str] = []
        for index, source in enumerate(exercise.images[:3], start=1):
            source_path = Path(source)
            if not source_path.exists():
                continue
            target = assets_dir / f"{sanitize_filename(exercise.id, 'ex')}_{index}.png"
            try:
                with Image.open(source_path) as image:
                    image = image.convert("RGB")
                    image.thumbnail((1800, 1800))
                    image.save(target, "PNG")
                paths.append(f"assets/{target.name}")
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"题图 {source_path.name} 无法转换：{exc}")
        return paths
