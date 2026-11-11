"""Book identity lookup through DeepSeek Search/Fetch and optional catalogues.

The service never invents a bibliographic record or a cover.  Search results
are evidence, not facts: candidates must be corroborated by an ISBN or by a
strong title/author/publisher match before a cover can be marked verified.
When evidence is ambiguous the result is ``NEEDS_REVIEW`` and the GUI lets the
owner pick a candidate.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import fitz
import httpx
from PIL import Image

from models.schemas import ApiUsage, BookCandidate, Subject
from .config import SettingsManager
from .db import Database
from .logging_setup import get_logger
from .paths import AppPaths
from .secrets import SecretStore

logger = get_logger("book_lookup")

ISBN10_RE = re.compile(r"(?<!\d)(\d{9}[\dXx])(?!\d)")
ISBN13_RE = re.compile(r"(?<!\d)(97[89]\d{10})(?!\d)")
YEAR_RE = re.compile(r"(19|20)\d{2}")
IMAGE_RE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)")
IMG_SRC_RE = re.compile(r"<img[^>]+src=[\"'](https?://[^\"']+)[\"']", re.I)


class BookLookupError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0, code: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


@dataclass
class LookupOutcome:
    book: dict[str, Any]
    candidates: list[BookCandidate] = field(default_factory=list)
    status: str = "NEEDS_REVIEW"
    warnings: list[str] = field(default_factory=list)
    calls: int = 0
    cost: float = 0.0
    cost_known: bool = True
    cached: bool = False
    cover_path: Path | None = None
    selected: BookCandidate | None = None


@dataclass
class ExtractedIdentity:
    title: str = ""
    original_title: str = ""
    authors: list[str] = field(default_factory=list)
    translators: list[str] = field(default_factory=list)
    publisher: str = ""
    edition: str = ""
    publication_year: str = ""
    isbn_10: str = ""
    isbn_13: str = ""
    volumes: int = 1
    language: str = "zh"
    subject: Subject = Subject.OTHER
    cover_url: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "original_title": self.original_title,
            "authors": list(self.authors),
            "translators": list(self.translators),
            "publisher": self.publisher,
            "edition": self.edition,
            "publication_year": self.publication_year,
            "isbn_10": self.isbn_10,
            "isbn_13": self.isbn_13,
            "volumes": self.volumes,
            "language": self.language,
            "subject": self.subject.value,
            "cover_url": self.cover_url,
        }


def _clean_isbn(value: str) -> tuple[str, str]:
    digits = re.sub(r"[^0-9Xx]", "", value or "").upper()
    if len(digits) == 13 and digits.startswith(("978", "979")):
        return "", digits
    if len(digits) == 10:
        return digits, ""
    return "", ""


def _similarity(left: str, right: str) -> float:
    from difflib import SequenceMatcher

    a = re.sub(r"\s+", "", (left or "").lower())
    b = re.sub(r"\s+", "", (right or "").lower())
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def extract_identity(pdf_path: Path, *, max_pages: int = 12) -> ExtractedIdentity:
    """Best-effort bibliographic hints from the first pages of a PDF."""
    result = ExtractedIdentity()
    try:
        document = fitz.open(pdf_path)
    except Exception as exc:
        raise BookLookupError(f"无法打开 PDF：{exc}") from exc
    try:
        metadata = document.metadata or {}
        if metadata.get("title"):
            result.title = str(metadata["title"]).strip()
        if metadata.get("author"):
            result.authors = [
                item.strip()
                for item in re.split(r"[;；,，、]", str(metadata["author"]))
                if item.strip()
            ]
        pages_text: list[str] = []
        for index in range(min(max_pages, document.page_count)):
            pages_text.append(document[index].get_text("text"))
        text = "\n".join(pages_text)
    finally:
        document.close()

    compact = re.sub(r"[ \t]+", " ", text)
    isbn13 = ISBN13_RE.search(compact.replace("-", ""))
    if isbn13:
        result.isbn_13 = isbn13.group(1)
    isbn10 = ISBN10_RE.search(compact.replace("-", ""))
    if isbn10:
        result.isbn_10 = isbn10.group(1)

    for line in compact.splitlines():
        value = line.strip()
        if not value:
            continue
        if "出版社" in value and not result.publisher:
            result.publisher = re.sub(r".*?出版社", "出版社", value).strip(" ：:")
            result.publisher = value[:60]
        if ("版" in value or "Edition" in value) and not result.edition:
            match = re.search(r"(第[一二三四五六七八九十0-9]+版|[0-9]{4}年?第?[0-9]*版)", value)
            if match:
                result.edition = match.group(1)
        if "著" in value or "编著" in value or "主编" in value:
            if not result.authors:
                name = re.split(r"[著编主]", value)[0].strip(" ：:")
                if 1 < len(name) < 30:
                    result.authors = [name]
        if "译" in value and not result.translators:
            name = value.split("译")[0].strip(" ：:")
            if 1 < len(name) < 30:
                result.translators = [name]
    years = YEAR_RE.findall(compact)
    if years:
        published = [m.group(0) for m in YEAR_RE.finditer(compact)]
        result.publication_year = max(published)
    if not result.title:
        lines = [line.strip() for line in pages_text[0].splitlines() if line.strip()]
        for line in lines[:8]:
            if 2 <= len(line) <= 60 and "ISBN" not in line.upper():
                result.title = line
                break
    if "线性代数" in result.title:
        result.subject = Subject.MATHEMATICS
    elif "物理" in result.title:
        result.subject = Subject.PHYSICS
    elif "化学" in result.title:
        result.subject = Subject.CHEMISTRY
    elif "计算机" in result.title or "程序" in result.title:
        result.subject = Subject.COMPUTER_SCIENCE
    return result


class DeepSeekTools:
    """Minimal official DeepSeek Search and Fetch client."""

    def __init__(
        self,
        settings: SettingsManager,
        secrets: SecretStore,
        db: Database | None = None,
        client: httpx.Client | None = None,
    ):
        self.settings = settings
        self.secrets = secrets
        self.db = db
        self.base_url = str(settings.get("deepseek.base_url", "https://api.deepseek.com")).rstrip("/")
        self.search_path = str(settings.get("deepseek.search_path", "/v1/tools/search"))
        self.fetch_path = str(settings.get("deepseek.fetch_path", "/v1/tools/fetch"))
        self.timeout = float(settings.get("deepseek.timeout_seconds", 60))
        self.max_retries = int(settings.get("deepseek.max_retries", 2))
        self.calls = 0
        self.cost = 0.0
        self.cost_known = True
        self._client = client

    def api_key(self) -> str:
        return (
            self.secrets.get("deepseek_api_key")
            or str(self.settings.get("deepseek.api_key", ""))
            or ""
        )

    def configured(self) -> tuple[bool, str]:
        if not self.settings.get("deepseek.enabled", False):
            return False, "DeepSeek 检索未启用"
        if not self.api_key():
            return False, "未配置 DeepSeek API Key"
        return True, ""

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key()}",
            "Content-Type": "application/json",
        }

    def _record(self, kind: str) -> None:
        price = float(
            self.settings.get(
                "deepseek.search_price_per_1000" if kind == "deepseek_search" else "deepseek.fetch_price_per_1000",
                0.0,
            )
            or 0.0
        )
        cost = price / 1000.0
        self.calls += 1
        self.cost += cost
        if price <= 0:
            self.cost_known = False
        if self.db is not None:
            self.db.add_usage(
                ApiUsage(
                    kind=kind,
                    model="deepseek-search" if kind == "deepseek_search" else "deepseek-fetch",
                    estimated_cost=cost,
                    cost_known=price > 0,
                )
            )

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.base_url + path
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.post(url, headers=self._headers(), json=payload)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(min(4.0, 1.0 * (2**attempt)))
                    continue
                raise BookLookupError(f"DeepSeek 网络错误：{exc}") from exc
            if response.status_code == 200:
                data = response.json()
                if not isinstance(data, dict):
                    raise BookLookupError("DeepSeek 返回格式异常")
                return data
            if response.status_code in (408, 429, 500, 502, 503, 504) and attempt < self.max_retries:
                time.sleep(min(4.0, 1.0 * (2**attempt)))
                continue
            try:
                detail = response.json().get("error", {})
            except Exception:
                detail = {}
            message = detail.get("message") if isinstance(detail, dict) else ""
            raise BookLookupError(
                f"DeepSeek {response.status_code}：{message or response.text[:200]}",
                status_code=response.status_code,
                code=str(detail.get("code", "")) if isinstance(detail, dict) else "",
            )
        raise BookLookupError(f"DeepSeek 请求失败：{last_error}")

    def search(
        self,
        query: str,
        *,
        count: int | None = None,
        need_content: bool | None = None,
    ) -> dict[str, Any]:
        configured, reason = self.configured()
        if not configured:
            raise BookLookupError(reason)
        payload: dict[str, Any] = {
            "query": query[:128],
            "count": int(count or self.settings.get("deepseek.search_count", 10)),
            "need_content": bool(
                self.settings.get("deepseek.need_content", False)
                if need_content is None
                else need_content
            ),
        }
        data = self._post(self.search_path, payload)
        self._record("deepseek_search")
        return data

    def fetch(self, url: str) -> dict[str, Any]:
        configured, reason = self.configured()
        if not configured:
            raise BookLookupError(reason)
        data = self._post(self.fetch_path, {"url": url})
        self._record("deepseek_fetch")
        return data


class BookLookupService:
    def __init__(
        self,
        settings: SettingsManager,
        db: Database,
        secrets: SecretStore,
        paths: AppPaths,
        *,
        tools: DeepSeekTools | None = None,
        client: httpx.Client | None = None,
    ):
        self.settings = settings
        self.db = db
        self.secrets = secrets
        self.paths = paths
        self.tools = tools or DeepSeekTools(settings, secrets, db, client)
        self._client = client

    def configured(self) -> tuple[bool, str]:
        return self.tools.configured()

    # ------------------------------------------------------------------ public

    def lookup(
        self,
        *,
        title: str = "",
        authors: Iterable[str] | None = None,
        publisher: str = "",
        edition: str = "",
        isbn: str = "",
        pdf_path: Path | None = None,
        force: bool = False,
    ) -> LookupOutcome:
        hints = ExtractedIdentity()
        if pdf_path:
            hints = extract_identity(pdf_path)
        for key, value in (
            ("title", title),
            ("publisher", publisher),
            ("edition", edition),
        ):
            if value:
                setattr(hints, key, value)
        if authors:
            hints.authors = [str(item).strip() for item in authors if str(item).strip()]
        if isbn:
            hints.isbn_10, hints.isbn_13 = _clean_isbn(isbn)
            if not hints.isbn_10 and not hints.isbn_13:
                raise BookLookupError("ISBN 格式无效")

        cache_key = hashlib.sha256(
            json.dumps(
                {
                    "title": hints.title.lower().strip(),
                    "authors": sorted(a.lower() for a in hints.authors),
                    "publisher": hints.publisher.lower().strip(),
                    "edition": hints.edition,
                    "isbn": hints.isbn_13 or hints.isbn_10,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        if not force:
            cached = self.db.cache_get(cache_key)
            if cached:
                try:
                    payload = cached
                    if isinstance(payload, dict) and "payload_json" in payload:
                        payload = json.loads(payload["payload_json"])
                    candidates = [BookCandidate(**item) for item in payload.get("candidates", [])]
                    return LookupOutcome(
                        book=payload.get("book", hints.as_dict()),
                        candidates=candidates,
                        status=payload.get("status", "NEEDS_REVIEW"),
                        warnings=["使用缓存结果"] + list(payload.get("warnings", [])),
                        cached=True,
                        selected=BookCandidate(**payload["selected"]) if payload.get("selected") else None,
                    )
                except Exception:
                    pass

        configured, reason = self.configured()
        if not configured:
            return LookupOutcome(
                book=hints.as_dict(),
                status="NEEDS_REVIEW",
                warnings=[reason, "未执行联网检索，未填写任何未经证实的书目信息"],
            )

        candidates: list[BookCandidate] = []
        warnings: list[str] = []
        queries = self._queries(hints)
        for query in queries[:2]:
            try:
                result = self.tools.search(query, count=int(self.settings.get("deepseek.search_count", 10)))
            except BookLookupError as exc:
                warnings.append(str(exc))
                continue
            for item in self._rank_search_results(result.get("results", []), hints):
                candidates.append(item)
        candidates = self._merge_candidates(candidates)
        # Fetch the strongest official-looking pages to corroborate metadata/cover.
        fetched = 0
        for candidate in candidates[:5]:
            if fetched >= 3:
                break
            if not candidate.source_url:
                continue
            try:
                page = self.tools.fetch(candidate.source_url)
            except BookLookupError as exc:
                warnings.append(str(exc))
                continue
            fetched += 1
            content = str(page.get("content", ""))
            self._enrich(candidate, content, hints)
            cover_url = self._cover_url(candidate, content)
            if cover_url:
                candidate.cover_url = cover_url
        candidates.sort(key=lambda item: item.score, reverse=True)
        candidates = candidates[: int(self.settings.get("deepseek.max_candidates", 8))]
        selected = self._select(candidates, hints)
        book = self._book_payload(hints, selected)
        status = "VERIFIED" if selected and selected.cover_url and self._strong_match(selected, hints) else "NEEDS_REVIEW"
        cover_path: Path | None = None
        if selected and selected.cover_url:
            try:
                cover_path = self._download_cover(selected.cover_url, hints)
                if cover_path:
                    book["cover_image"] = str(cover_path)
                    book["cover_source_url"] = selected.cover_url
                    book["source_license"] = selected.source_license
                    book["source_verified"] = status == "VERIFIED"
                    book["verification_status"] = status
                else:
                    warnings.append("封面下载或尺寸校验未通过，已保留来源但未标记为已验证")
            except Exception as exc:
                warnings.append(f"封面获取失败：{exc}")
        book["verification_status"] = status
        book["metadata"] = {
            "query": queries,
            "candidate_count": len(candidates),
            "fetched_pages": fetched,
            "warnings": warnings,
        }
        payload = {
            "book": book,
            "candidates": [item.model_dump(mode="json") for item in candidates],
            "selected": selected.model_dump(mode="json") if selected else None,
            "status": status,
            "warnings": warnings,
        }
        self.db.cache_set(
            cache_key,
            "deepseek+books",
            payload,
            days=int(self.settings.get("deepseek.cache_days", 30)),
        )
        return LookupOutcome(
            book=book,
            candidates=candidates,
            status=status,
            warnings=warnings,
            calls=self.tools.calls,
            cost=self.tools.cost,
            cost_known=self.tools.cost_known,
            cover_path=cover_path,
            selected=selected,
        )

    # ------------------------------------------------------------------ parsing

    @staticmethod
    def _queries(hints: ExtractedIdentity) -> list[str]:
        queries: list[str] = []
        if hints.isbn_13 or hints.isbn_10:
            queries.append(f'"{hints.isbn_13 or hints.isbn_10}" 教材 出版社')
        if hints.title:
            parts = [f'"{hints.title}"']
            if hints.authors:
                parts.append(hints.authors[0])
            if hints.publisher:
                parts.append(hints.publisher)
            queries.append(" ".join(parts))
            queries.append(f"{hints.title} 原版 封面 ISBN")
        return [query for query in dict.fromkeys(queries) if query.strip()]

    @staticmethod
    def _rank_search_results(results: list[dict[str, Any]], hints: ExtractedIdentity) -> list[BookCandidate]:
        output: list[BookCandidate] = []
        for item in results[:15]:
            title = str(item.get("title", "")).strip()
            snippet = str(item.get("snippet", "") or item.get("summary", "")).strip()
            url = str(item.get("url", "")).strip()
            authority = float(item.get("authority", 0) or 0)
            score = float(item.get("rerank_score", 0) or 0)
            if not title or not url:
                continue
            title_match = _similarity(title, hints.title) if hints.title else 0.0
            combined = score * 10 + authority * 4 + title_match * 35
            if hints.isbn_13 and hints.isbn_13 in (snippet + title).replace("-", ""):
                combined += 80
            output.append(
                BookCandidate(
                    title=title,
                    publisher="",
                    isbn_13=hints.isbn_13 if hints.isbn_13 and hints.isbn_13 in snippet.replace("-", "") else "",
                    source_url=url,
                    source_name=urlparse(url).netloc,
                    source_license="search-result",
                    authority=authority,
                    score=round(combined, 2),
                    reasons=[f"搜索排序 {score:.2f}", f"来源权威度 {authority:.0f}"],
                )
            )
        return output

    @staticmethod
    def _cover_url(candidate: BookCandidate, content: str) -> str:
        urls: list[str] = []
        urls.extend(IMAGE_RE.findall(content or ""))
        urls.extend(IMG_SRC_RE.findall(content or ""))
        for url in urls:
            lower = url.lower()
            if not lower.startswith(("http://", "https://")):
                continue
            if any(token in lower for token in ("logo", "icon", "avatar", "banner", "qrcode")):
                continue
            if any(ext in lower for ext in (".jpg", ".jpeg", ".png", ".webp")):
                return url
        return ""

    @staticmethod
    def _enrich(candidate: BookCandidate, content: str, hints: ExtractedIdentity) -> None:
        compact = re.sub(r"[ \t]+", " ", content or "")
        isbn13 = ISBN13_RE.search(compact.replace("-", ""))
        isbn10 = ISBN10_RE.search(compact.replace("-", ""))
        if isbn13:
            candidate.isbn_13 = isbn13.group(1)
        if isbn10:
            candidate.isbn_10 = isbn10.group(1)
        for line in compact.splitlines():
            value = line.strip(" #*-")
            if not value:
                continue
            if "出版社" in value and len(value) < 80:
                cleaned = re.sub(r"^.*?出版社\s*[：:]\s*", "", value).strip()
                candidate.publisher = (cleaned or value)[:80]
            if ("版" in value or "Edition" in value) and not candidate.edition:
                match = re.search(r"(第[一二三四五六七八九十0-9]+版|[0-9]{4}年?第?[0-9]*版)", value)
                if match:
                    candidate.edition = match.group(1)
        years = [m.group(0) for m in YEAR_RE.finditer(compact)]
        if years:
            candidate.publication_year = years[0]
        title_match = _similarity(candidate.title, hints.title) if hints.title else 0.0
        author_match = any(author and author in compact for author in hints.authors)
        publisher_match = bool(hints.publisher and hints.publisher in compact)
        candidate.score += title_match * 20 + (25 if author_match else 0) + (20 if publisher_match else 0)
        if candidate.isbn_13 and candidate.isbn_13 == hints.isbn_13:
            candidate.score += 100
            candidate.reasons.append("ISBN-13 完全匹配")
        if title_match >= 0.8:
            candidate.reasons.append("书名高度匹配")
        if author_match:
            candidate.reasons.append("作者匹配")
        if publisher_match:
            candidate.reasons.append("出版社匹配")
        candidate.reasons.append(f"已抓取正文 {len(content)} 字符")

    @staticmethod
    def _merge_candidates(candidates: list[BookCandidate]) -> list[BookCandidate]:
        merged: dict[str, BookCandidate] = {}
        for candidate in candidates:
            key = candidate.isbn_13 or candidate.isbn_10 or candidate.source_url
            if key in merged:
                first = merged[key]
                if candidate.cover_url and not first.cover_url:
                    first.cover_url = candidate.cover_url
                first.score = max(first.score, candidate.score)
                first.reasons = list(dict.fromkeys(first.reasons + candidate.reasons))
            else:
                merged[key] = candidate
        return list(merged.values())

    @staticmethod
    def _strong_match(candidate: BookCandidate, hints: ExtractedIdentity) -> bool:
        if hints.isbn_13 and candidate.isbn_13 == hints.isbn_13:
            return True
        if hints.isbn_10 and candidate.isbn_10 == hints.isbn_10:
            return True
        if not hints.title:
            return False
        return _similarity(candidate.title, hints.title) >= 0.8

    def _select(
        self, candidates: list[BookCandidate], hints: ExtractedIdentity
    ) -> BookCandidate | None:
        if not candidates:
            return None
        strong = [candidate for candidate in candidates if self._strong_match(candidate, hints)]
        if len(strong) == 1 and strong[0].score >= 45:
            return strong[0]
        if len(strong) > 1:
            # More than one edition matches: never guess silently.
            return None
        return None

    @staticmethod
    def _book_payload(hints: ExtractedIdentity, selected: BookCandidate | None) -> dict[str, Any]:
        book = hints.as_dict()
        if selected:
            book.update(
                {
                    "title": selected.title or book["title"],
                    "original_title": selected.original_title or book["original_title"],
                    "authors": selected.authors or book["authors"],
                    "publisher": selected.publisher or book["publisher"],
                    "edition": selected.edition or book["edition"],
                    "publication_year": selected.publication_year or book["publication_year"],
                    "isbn_10": selected.isbn_10 or book["isbn_10"],
                    "isbn_13": selected.isbn_13 or book["isbn_13"],
                    "cover_source_url": selected.cover_url,
                    "source_verified": False,
                }
            )
        return book

    # ------------------------------------------------------------------ cover

    def _download_cover(self, url: str, hints: ExtractedIdentity) -> Path | None:
        if not url.startswith(("http://", "https://")):
            return None
        client = self._client or httpx.Client(timeout=40, follow_redirects=True)
        own = self._client is None
        try:
            response = client.get(url, headers={"User-Agent": "WenLianJia/1.0"})
            if response.status_code != 200:
                return None
            content_type = response.headers.get("content-type", "").lower()
            if "image" not in content_type:
                return None
            data = response.content
            max_bytes = int(self.settings.get("books.max_cover_bytes", 12_000_000))
            if len(data) > max_bytes:
                return None
            from io import BytesIO

            image = Image.open(BytesIO(data))
            image.load()
            if image.width < int(self.settings.get("books.cover_min_width", 300)):
                return None
            if image.height < int(self.settings.get("books.cover_min_height", 400)):
                return None
            suffix = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(image.format or "", ".jpg")
            book_dir = self.paths.book_dir(self._book_code(hints))
            book_dir.mkdir(parents=True, exist_ok=True)
            target = book_dir / f"cover_original{suffix}"
            target.write_bytes(data)
            return target
        finally:
            if own:
                client.close()

    @staticmethod
    def _book_code(hints: ExtractedIdentity) -> str:
        digest = hashlib.sha1(
            f"{hints.isbn_13}|{hints.title}|{hints.publisher}".encode("utf-8")
        ).hexdigest()[:8].upper()
        return f"BK{digest}"
