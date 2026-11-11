"""Create the human- and machine-readable product delivery records.

Every value in these files comes from the database or from files that actually
exist on disk.  The module never claims that a missing upload or share has been
verified.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from models.schemas import GeneratedFile, Product, ShareRecord, UploadRecord, utc_now
from .config import SettingsManager
from .db import Database
from .logging_setup import get_logger
from .paths import AppPaths

logger = get_logger("products")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class DeliveryFile:
    path: Path
    kind: str
    size: int
    sha256: str

    def as_dict(self, root: Path | None = None) -> dict[str, Any]:
        try:
            name = self.path.relative_to(root).as_posix() if root else self.path.name
        except ValueError:
            name = self.path.name
        return {
            "name": self.path.name,
            "path": str(self.path),
            "relative_path": name,
            "kind": self.kind,
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass
class ProductInfoResult:
    info_json: Path
    info_markdown: Path
    file_list: Path
    delivery_text: Path
    acceptance: Path
    files: list[DeliveryFile] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class ProductManager:
    def __init__(self, settings: SettingsManager, db: Database, paths: AppPaths):
        self.settings = settings
        self.db = db
        self.paths = paths

    def write_all(
        self,
        product: Product,
        full_pdf: Path,
        quick_pdf: Path | None,
        image_files: list[Path] | None = None,
        *,
        share: ShareRecord | None = None,
        uploads: list[UploadRecord] | None = None,
        acceptance_checks: list[dict[str, Any]] | None = None,
    ) -> ProductInfoResult:
        product_dir = self.paths.product_dir(product.sku, product.title)
        dirs = self.paths.product_subdirs(product_dir)
        info_dir = dirs["info"]
        delivery_dir = dirs["delivery"]
        warnings: list[str] = []
        fresh = self.db.get_product(product.id) or product
        counts = self.db.exercise_status_counts(product.id)
        chapters = self.db.list_chapters(product.id)
        usage = self.db.usage_for_product(product.id)
        total = sum(counts.values())
        solved = counts.get("ai_draft", 0) + counts.get("verified", 0) + counts.get("approved", 0)
        verified = counts.get("verified", 0) + counts.get("approved", 0)
        review = counts.get("needs_review", 0)

        if not full_pdf.exists():
            raise FileNotFoundError(f"完整解析 PDF 不存在：{full_pdf}")
        if quick_pdf and not quick_pdf.exists():
            warnings.append("答案速查 PDF 未生成，交付清单中不包含该文件")
            quick_pdf = None
        image_files = [path for path in (image_files or []) if path.exists()]
        if not image_files:
            warnings.append("商品图片尚未生成，交付清单中不包含图片")

        pdf_pages = 0
        try:
            import pymupdf

            with pymupdf.open(full_pdf) as document:
                pdf_pages = document.page_count
        except Exception as exc:  # noqa: BLE001 - recorded, not hidden
            warnings.append(f"无法读取最终 PDF 页数：{exc}")

        delivery: list[DeliveryFile] = []
        for path, kind in [(full_pdf, "pdf_full"), (quick_pdf, "pdf_quick")]:
            if path:
                delivery.append(DeliveryFile(path, kind, path.stat().st_size, sha256_file(path)))
        for path in image_files:
            delivery.append(DeliveryFile(path, "product_image", path.stat().st_size, sha256_file(path)))

        for item in delivery:
            if not any(record.path == str(item.path) for record in self.db.list_files(product.id)):
                self.db.add_file(
                    GeneratedFile(
                        product_id=product.id,
                        kind=item.kind,
                        path=str(item.path),
                        sha256=item.sha256,
                        size=item.size,
                    )
                )

        files_payload = [item.as_dict(product_dir) for item in delivery]
        share_payload = self._share_payload(share)
        upload_payload = [self._upload_payload(record) for record in (uploads or self.db.list_uploads(product.id))]
        acceptance = self._acceptance(
            product,
            full_pdf,
            quick_pdf,
            image_files,
            uploads or self.db.list_uploads(product.id),
            share,
            acceptance_checks,
        )
        info = {
            "sku": fresh.sku,
            "title": fresh.title,
            "subject": fresh.subject.value,
            "source_title": fresh.source_title,
            "source_author": fresh.source_author,
            "source_edition": fresh.source_edition,
            "total_pages": fresh.total_pages,
            "answer_pdf_pages": pdf_pages,
            "chapter_count": len([chapter for chapter in chapters if chapter.level <= 1]) or len(chapters),
            "exercise_count": total,
            "solved_count": solved,
            "verified_count": verified,
            "review_required": review,
            "files": files_payload,
            "images": [path.name for path in image_files],
            "usage": {
                "calls": usage.get("calls", 0),
                "input_tokens": usage.get("tin", 0),
                "output_tokens": usage.get("tout", 0),
                "estimated_cost": usage.get("cost", 0.0),
                "cost_known": not bool(usage.get("unknown_cost", 0)),
            },
            "share": share_payload,
            "uploads": upload_payload,
            "created_at": fresh.created_at,
            "generated_at": utc_now(),
            "version": fresh.version or "1.0",
        }
        info_json = info_dir / "商品信息.json"
        info_json.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        info_markdown = info_dir / "商品介绍.md"
        info_markdown.write_text(
            self._markdown(fresh, chapters, total, solved, verified, review, pdf_pages, delivery, share_payload),
            encoding="utf-8",
        )
        file_list = delivery_dir / "文件清单.json"
        file_list.write_text(
            json.dumps(
                {
                    "sku": fresh.sku,
                    "generated_at": utc_now(),
                    "root": str(product_dir),
                    "files": files_payload,
                    "count": len(files_payload),
                    "total_bytes": sum(item.size for item in delivery),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        delivery_text = delivery_dir / "商品交付信息.txt"
        delivery_text.write_text(self._delivery_text(fresh, delivery, share_payload, acceptance), encoding="utf-8")
        acceptance_path = delivery_dir / "验收记录.json"
        acceptance_path.write_text(json.dumps(acceptance, ensure_ascii=False, indent=2), encoding="utf-8")

        logger.info("wrote product records for %s (%d files)", fresh.sku, len(delivery))
        return ProductInfoResult(
            info_json=info_json,
            info_markdown=info_markdown,
            file_list=file_list,
            delivery_text=delivery_text,
            acceptance=acceptance_path,
            files=delivery,
            warnings=warnings,
        )

    # ------------------------------------------------------------------ rules

    def _acceptance_checks(
        self,
        full_pdf: Path,
        quick_pdf: Path | None,
        images: list[Path],
        uploads: list[UploadRecord],
        share: ShareRecord | None,
    ) -> list[dict[str, Any]]:
        verified_uploads = [item for item in uploads if item.status in ("verified", "completed")]
        checks = [
            {
                "name": "完整解析 PDF 已生成",
                "status": "pass" if full_pdf.exists() and full_pdf.stat().st_size > 1000 else "fail",
                "evidence": str(full_pdf),
            },
            {
                "name": "答案速查 PDF 已生成",
                "status": "pass" if quick_pdf and quick_pdf.exists() else "pending",
                "evidence": str(quick_pdf) if quick_pdf else "未生成",
            },
            {
                "name": "商品图片已生成",
                "status": "pass" if len(images) >= 7 else ("pending" if not images else "fail"),
                "evidence": f"{len(images)} 张",
            },
            {
                "name": "文件哈希已记录",
                "status": "pass" if full_pdf.exists() else "pending",
                "evidence": "见 文件清单.json",
            },
            {
                "name": "网盘上传已核对",
                "status": "pass" if uploads and len(verified_uploads) == len(uploads) else ("pending" if not uploads else "fail"),
                "evidence": f"{len(verified_uploads)}/{len(uploads)} 个文件标记为已核对",
            },
            {
                "name": "分享链接已创建",
                "status": "pass" if share and share.url else "pending",
                "evidence": share.url if share and share.url else "尚未创建",
            },
            {
                "name": "分享链接已验证",
                "status": "pass" if share and share.verified else "pending",
                "evidence": "平台链接可访问" if share and share.verified else "需要人工验证或平台 API 验证",
            },
        ]
        if not uploads:
            checks[-2]["status"] = "pending"
            checks[-2]["evidence"] = "未配置或未执行百度网盘上传"
        return checks

    def _acceptance(
        self,
        product: Product,
        full_pdf: Path,
        quick_pdf: Path | None,
        images: list[Path],
        uploads: list[UploadRecord],
        share: ShareRecord | None,
        extra: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        checks = self._acceptance_checks(full_pdf, quick_pdf, images, uploads, share)
        if extra:
            checks.extend(extra)
        failed = [check for check in checks if check.get("status") == "fail"]
        pending = [check for check in checks if check.get("status") == "pending"]
        return {
            "sku": product.sku,
            "product_id": product.id,
            "checked_at": utc_now(),
            "overall": "failed" if failed else ("pending" if pending else "passed"),
            "checks": checks,
            "note": "自动验收只覆盖本机可核实的项目；分享链接或云端内容未经人工验证时标记为 pending。",
        }

    @staticmethod
    def _share_payload(share: ShareRecord | None) -> dict[str, Any] | None:
        if not share:
            return None
        return {
            "url": share.url,
            "password": share.password,
            "period": share.period,
            "status": share.status,
            "verified": share.verified,
            "verified_at": share.verified_at,
            "created_at": share.created_at,
        }

    @staticmethod
    def _upload_payload(record: UploadRecord) -> dict[str, Any]:
        return {
            "local_path": record.local_path,
            "remote_path": record.remote_path,
            "size": record.size,
            "sha256": record.sha256,
            "status": record.status,
            "error": record.error,
            "updated_at": record.updated_at,
        }

    def _markdown(
        self,
        product: Product,
        chapters,
        total: int,
        solved: int,
        verified: int,
        review: int,
        pdf_pages: int,
        files: list[DeliveryFile],
        share: dict[str, Any] | None,
    ) -> str:
        chapter_lines = "\n".join(
            f"- {chapter.title}" for chapter in chapters if chapter.level <= 1
        ) or "- 以最终 PDF 目录为准"
        file_lines = "\n".join(f"- {item.path.name}" for item in files) or "- 暂无"
        subject_labels = {
            "mathematics": "数学",
            "physics": "物理",
            "chemistry": "化学",
            "computer_science": "计算机",
            "other": "其他",
        }
        lines = [
            f"# {product.title}",
            "",
            "## 商品信息",
            "",
            f"- SKU：{product.sku}",
            f"- 学科：{subject_labels.get(product.subject.value, product.subject.value)}",
            f"- 教材：{product.source_title or '未填写'}",
            f"- 版本：{product.source_edition or product.version or '未填写'}",
            f"- 作者：{product.source_author or '未填写'}",
            f"- 原书页数：{product.total_pages}",
            f"- 解析 PDF 页数：{pdf_pages}",
            "",
            "## 解析覆盖",
            "",
            f"- 收录习题：{total} 道",
            f"- 已完成解答：{solved} 道",
            f"- 已自动复核：{verified} 道",
            f"- 待人工复核：{review} 道",
            "",
            chapter_lines,
            "",
            "## 解析特点",
            "",
            "- 逐题保留原始页码、题目原文与公式。",
            "- 提供考点分析、解题思路、详细推导、最终答案与方法总结。",
            "- 主模型生成后由独立复核流程重新分析；未通过一致性检查的题目会标记为待人工复核。",
            "- 使用 XeLaTeX 排版，中文、公式与图片按 A4 正式文档输出。",
            "",
            "## 交付文件",
            "",
            file_lines,
            "",
        ]
        if share:
            lines.extend(
                [
                    "## 网盘交付",
                    "",
                    f"- 链接：{share.get('url') or '未生成'}",
                    f"- 提取码：{share.get('password') or '无'}",
                    f"- 有效期：{share.get('period') or '未填写'}",
                    f"- 状态：{'已验证' if share.get('verified') else '待验证'}",
                    "",
                ]
            )
        lines.extend(
            [
                "## 使用与版权说明",
                "",
                "本商品仅交付原创解析、排版成果与必要的合法引用；原始教材的复制与传播需由使用者确认授权。",
                "如对某道题的答案有疑问，请以题目所在页码和解析中的推导过程为准，并在交付记录中登记复核。",
                "",
                f"生成时间：{utc_now()}",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _delivery_text(
        product: Product,
        files: list[DeliveryFile],
        share: dict[str, Any] | None,
        acceptance: dict[str, Any],
    ) -> str:
        lines = [
            f"商品：{product.title}",
            f"SKU：{product.sku}",
            f"生成时间：{utc_now()}",
            "",
            "交付文件：",
        ]
        lines.extend(f"  - {item.path.name}（{item.size} 字节）" for item in files)
        lines.extend(
            [
                "",
                f"网盘链接：{(share or {}).get('url') or '尚未生成'}",
                f"提取码：{(share or {}).get('password') or '无'}",
                f"链接验证：{'已验证' if (share or {}).get('verified') else '待验证'}",
                "",
                f"验收状态：{acceptance.get('overall')}",
                "说明：云端上传、分享权限与链接可用性以实际平台结果为准；未验证项不会标记为成功。",
            ]
        )
        return "\n".join(lines)
