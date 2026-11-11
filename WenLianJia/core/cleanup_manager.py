"""Two-phase cleanup with explicit, evidence-based safety gates.

Nothing is deleted directly from a product folder.  Rebuildable intermediate
folders are first *staged* under ``05_临时缓存/待清理`` with a manifest and a
recovery window.  A second, explicitly confirmed action purges a staging
folder.  Final PDFs, product images, delivery records and (by default) the
original textbook stay in place.

Eligibility is deliberately conservative.  A product is only staged when the
delivery manifest matches the bytes on disk, every delivered file is recorded
as cloud-verified, a share exists and has been verified, and the acceptance
record passed.  The application never invents a pass for a missing share or an
unverified upload.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from models.schemas import Product, utc_now
from .config import SettingsManager
from .db import Database
from .logging_setup import get_logger
from .paths import AppPaths

log = get_logger(__name__)


class CleanupError(RuntimeError):
    pass


@dataclass
class CleanupCheck:
    name: str
    status: str  # pass / fail / pending
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class CleanupEligibility:
    product_id: str
    eligible: bool
    blockers: list[str] = field(default_factory=list)
    checks: list[CleanupCheck] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "product_id": self.product_id,
            "eligible": self.eligible,
            "blockers": self.blockers,
            "checks": [check.as_dict() for check in self.checks],
        }


@dataclass
class StagedCleanup:
    staging_dir: Path
    manifest: Path
    files: int
    bytes: int
    purge_after: str
    product_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "staging_dir": str(self.staging_dir),
            "manifest": str(self.manifest),
            "files": self.files,
            "bytes": self.bytes,
            "purge_after": self.purge_after,
            "product_id": self.product_id,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


class CleanupManager:
    def __init__(self, settings: SettingsManager, db: Database, paths: AppPaths):
        self.settings = settings
        self.db = db
        self.paths = paths
        self.staging_root = self.paths.cache / "待清理"

    # ------------------------------------------------------------- eligibility
    def evaluate(self, product: Product | str) -> CleanupEligibility:
        product_obj = self.db.get_product(product) if isinstance(product, str) else product
        if not product_obj:
            return CleanupEligibility(
                product_id=str(product),
                eligible=False,
                blockers=["商品不存在"],
                checks=[CleanupCheck("商品记录", "fail", "数据库中没有该商品")],
            )
        product = product_obj
        product_dir = self.paths.product_dir(product.sku, product.title)
        result = CleanupEligibility(product_id=product.id, eligible=False)
        checks = result.checks

        def add(name: str, status: str, detail: str = "") -> None:
            checks.append(CleanupCheck(name, status, detail))

        add("商品目录", "pass" if product_dir.is_dir() else "fail", str(product_dir))
        manifest_path = product_dir / "07_交付记录" / "文件清单.json"
        manifest = _read_json(manifest_path)
        acceptance = _read_json(product_dir / "07_交付记录" / "验收记录.json")
        result.manifest = manifest
        add(
            "交付文件清单",
            "pass" if manifest.get("files") else "fail",
            f"{len(manifest.get('files') or [])} 个文件",
        )

        file_ok = True
        hash_ok = True
        files = manifest.get("files") or []
        reported_hash_failure = False
        for item in files:
            raw = str(item.get("path") or item.get("relative_path") or "")
            path = Path(raw)
            if not path.is_absolute():
                path = product_dir / raw
            if not raw or not path.is_file():
                file_ok = False
                hash_ok = False
                add("交付文件存在", "fail", f"缺失：{raw or '未记录路径'}")
                break
            if int(item.get("size", -1)) != path.stat().st_size:
                file_ok = False
                hash_ok = False
                add("交付文件大小", "fail", f"大小变化：{path.name}")
                break
            expected = str(item.get("sha256") or "")
            if not expected or _sha256(path) != expected:
                hash_ok = False
                reported_hash_failure = True
                add("本地文件哈希", "fail", f"哈希不一致：{path.name}")
                break
        if file_ok:
            add("交付文件完整", "pass", f"{len(files)} 个文件")
        if hash_ok and files:
            add("本地文件哈希", "pass", "全部与文件清单一致")
        elif files and not reported_hash_failure:
            add("本地文件哈希", "fail", "无法确认全部文件哈希")

        full_pdfs = [
            item
            for item in files
            if str(item.get("kind", "")) in {"full_pdf", "pdf_full"}
            or "习题详解" in str(item.get("path", ""))
        ]
        quick_pdfs = [
            item
            for item in files
            if str(item.get("kind", "")) in {"quick_pdf", "pdf_quick"}
            or "答案速查" in str(item.get("path", ""))
        ]
        images = [
            item
            for item in files
            if str(item.get("kind", "")) in {"image", "product_image"}
            or str(item.get("kind", "")).startswith("image")
        ]
        add("完整解析 PDF", "pass" if full_pdfs else "fail", f"{len(full_pdfs)} 个")
        add("答案速查 PDF", "pass" if quick_pdfs else "fail", f"{len(quick_pdfs)} 个")
        add("商品图片", "pass" if len(images) >= 7 else "fail", f"{len(images)} 张（要求 7 张）")

        acceptance_status = str(acceptance.get("overall", ""))
        add(
            "验收记录",
            "pass" if acceptance_status == "passed" else "fail",
            acceptance_status or "没有验收记录",
        )

        uploads = self.db.list_uploads(product.id)
        verified = {str(Path(item.local_path).resolve()) for item in uploads if item.status == "verified"}
        delivered_paths: list[Path] = []
        for item in files:
            raw = str(item.get("path") or item.get("relative_path") or "")
            path = Path(raw)
            if raw and not path.is_absolute():
                path = product_dir / raw
            delivered_paths.append(path.resolve())
        missing_uploads = [str(path) for path in delivered_paths if str(path) not in verified]
        add(
            "云端上传核对",
            "pass" if uploads and not missing_uploads else "fail",
            f"{len(uploads)} 条上传记录，{len(verified)} 条已核对"
            + (f"，缺少：{len(missing_uploads)}" if missing_uploads else ""),
        )

        share = self.db.latest_share(product.id)
        add(
            "分享链接",
            "pass" if share and share.url else "fail",
            share.url if share and share.url else "未创建",
        )
        add(
            "分享链接已验证",
            "pass" if share and (share.verified or share.status == "verified_manual") else "fail",
            "已验证" if share and share.verified else "尚未人工确认提取码和下载权限",
        )
        add(
            "云端哈希校验",
            "pass" if bool(self.settings.get("netdisk.verify_download_hash", False)) else "fail",
            "已启用下载后 SHA-256 校验"
            if bool(self.settings.get("netdisk.verify_download_hash", False))
            else "未启用下载校验；百度云端 md5 不是本地文件 MD5，不能冒充哈希验证",
        )
        add(
            "原始教材保留",
            "pass",
            "清理阶段不会移动 00_原始教材"
            + ("（例外：已开启删除原始教材并单独确认）" if self.settings.get("cleanup.delete_source", False) else ""),
        )

        blockers = [f"{check.name}：{check.detail}" for check in checks if check.status == "fail"]
        result.eligible = not blockers
        result.blockers = blockers
        return result

    # ----------------------------------------------------------------- staging
    def stage(
        self,
        product: Product | str,
        *,
        include_intermediates: bool = True,
        include_source: bool | None = None,
    ) -> StagedCleanup:
        product_obj = self.db.get_product(product) if isinstance(product, str) else product
        if not product_obj:
            raise CleanupError("商品不存在")
        eligibility = self.evaluate(product_obj)
        if not eligibility.eligible:
            raise CleanupError("清理条件未满足：" + "；".join(eligibility.blockers))
        product_dir = self.paths.product_dir(product_obj.sku, product_obj.title)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        staging = self.staging_root / f"{product_obj.sku or 'NO-SKU'}_{stamp}"
        staging.mkdir(parents=True, exist_ok=False)

        candidates = [
            product_dir / "01_教材解析",
            product_dir / "02_习题数据库",
            product_dir / "03_AI答案与详解",
        ]
        if include_intermediates:
            candidates.extend(
                [
                    product_dir / "04_最终PDF" / "build_full",
                    product_dir / "04_最终PDF" / "build_quick",
                ]
            )
        source_decision = (
            bool(self.settings.get("cleanup.delete_source", False))
            if include_source is None
            else include_source
        )
        if source_decision:
            candidates.append(product_dir / "00_原始教材")

        moved: list[dict[str, Any]] = []
        total_bytes = 0
        for source in candidates:
            if not source.exists():
                continue
            target = staging / source.name
            if target.exists():
                target = staging / f"{source.name}_{len(moved) + 1}"
            shutil.move(str(source), str(target))
            size, count = _tree_size(target)
            total_bytes += size
            moved.append(
                {
                    "original": str(source),
                    "staged_as": str(target),
                    "files": count,
                    "bytes": size,
                }
            )

        # Remove empty build directory and LaTeX auxiliaries, but keep final PDFs.
        build_root = product_dir / "04_最终PDF"
        if build_root.is_dir():
            for child in build_root.iterdir():
                if child.is_dir() and not any(child.iterdir()):
                    child.rmdir()

        days = max(1, int(self.settings.get("cleanup.stage_days", 7) or 7))
        purge_after = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        manifest = {
            "product_id": product_obj.id,
            "sku": product_obj.sku,
            "title": product_obj.title,
            "created_at": utc_now(),
            "purge_after": purge_after,
            "stage_days": days,
            "moved": moved,
            "kept": [
                str(product_dir / "00_原始教材") if not source_decision else "",
                str(product_dir / "04_最终PDF"),
                str(product_dir / "05_商品图片"),
                str(product_dir / "06_商品信息"),
                str(product_dir / "07_交付记录"),
            ],
            "note": "第一阶段仅移动到待清理目录；恢复窗口内可调用 restore()；删除需要明确确认。",
        }
        manifest_path = staging / "cleanup_manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        self.db.update_product(product_obj.id, status="cleaned_stage1")
        log.info("staged cleanup for %s at %s (%d files)", product_obj.sku, staging, len(moved))
        return StagedCleanup(
            staging_dir=staging,
            manifest=manifest_path,
            files=sum(item["files"] for item in moved),
            bytes=total_bytes,
            purge_after=purge_after,
            product_id=product_obj.id,
        )

    def list_staged(self, *, expired_only: bool = False) -> list[StagedCleanup]:
        if not self.staging_root.is_dir():
            return []
        now = datetime.now(timezone.utc)
        output: list[StagedCleanup] = []
        for manifest_path in sorted(self.staging_root.glob("*/cleanup_manifest.json")):
            data = _read_json(manifest_path)
            if not data:
                continue
            purge_after = str(data.get("purge_after", ""))
            try:
                expired = datetime.fromisoformat(purge_after) <= now if purge_after else False
            except ValueError:
                expired = False
            if expired_only and not expired:
                continue
            output.append(
                StagedCleanup(
                    staging_dir=manifest_path.parent,
                    manifest=manifest_path,
                    files=sum(int(item.get("files", 0)) for item in data.get("moved", [])),
                    bytes=sum(int(item.get("bytes", 0)) for item in data.get("moved", [])),
                    purge_after=purge_after,
                    product_id=str(data.get("product_id", "")),
                )
            )
        return output

    def restore(self, staging_dir: str | Path) -> list[str]:
        staging = Path(staging_dir)
        manifest_path = staging / "cleanup_manifest.json"
        data = _read_json(manifest_path)
        if not data:
            raise CleanupError("待清理清单不存在或损坏")
        restored: list[str] = []
        for item in data.get("moved", []):
            source = Path(str(item.get("original", "")))
            staged = Path(str(item.get("staged_as", "")))
            if not staged.exists():
                continue
            if source.exists():
                raise CleanupError(f"恢复目标已存在，拒绝覆盖：{source}")
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staged), str(source))
            restored.append(str(source))
        manifest_path.unlink(missing_ok=True)
        try:
            staging.rmdir()
        except OSError:
            pass
        self.db.update_product(str(data.get("product_id", "")), status="completed")
        return restored

    def purge(self, staging_dir: str | Path, *, confirmation: str, product_id: str = "") -> dict[str, Any]:
        if confirmation.strip() != "确认删除":
            raise CleanupError("不可逆删除需要输入“确认删除”")
        staging = Path(staging_dir).resolve()
        root = self.staging_root.resolve()
        if root not in staging.parents:
            raise CleanupError(f"拒绝删除待清理目录之外的路径：{staging}")
        if not staging.is_dir():
            raise CleanupError("待清理目录不存在")
        size, count = _tree_size(staging)
        manifest = _read_json(staging / "cleanup_manifest.json")
        shutil.rmtree(staging)
        pid = product_id or str(manifest.get("product_id", ""))
        if pid:
            self.db.update_product(pid, status="cleaned")
        log.info("purged staging folder %s (%d files, %d bytes)", staging, count, size)
        return {"staging_dir": str(staging), "files": count, "bytes": size, "product_id": pid}

    def purge_expired(self) -> list[StagedCleanup]:
        """Return expired stages; deletion still requires confirmation per folder."""
        return self.list_staged(expired_only=True)


def _tree_size(path: Path) -> tuple[int, int]:
    total = 0
    count = 0
    if path.is_file():
        return path.stat().st_size, 1
    for child in path.rglob("*"):
        if child.is_file():
            total += child.stat().st_size
            count += 1
    return total, count


def human_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024**2:
        return f"{value / 1024:.1f} KB"
    if value < 1024**3:
        return f"{value / 1024**2:.1f} MB"
    return f"{value / 1024**3:.2f} GB"
