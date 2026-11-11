"""Baidu Netdisk client built on the official OAuth / xpan / PCS APIs.

Important limits of the official platform, surfaced instead of hidden:

* Uploads made with an app's OAuth token are only allowed below
  ``/apps/{app_name}``.  The configured human-friendly root is therefore
  prefixed automatically and the UI shows the effective path.
* The share endpoint used here belongs to the paid "文件分享服务" product and
  must be enabled in the developer console.  A permission error is reported as
  ``needs_manual`` rather than being treated as a successful share.
* The ``md5`` returned by ``list``/``meta`` is a *cloud* hash, not the MD5 of
  the local bytes.  Content integrity is only claimed after an optional
  download-and-SHA-256 check.

No credentials or tokens are written to logs, product files or the database.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import secrets
import string
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote

import httpx

from models.schemas import Product, ShareRecord, UploadRecord, utc_now
from .config import SettingsManager
from .db import Database
from .logging_setup import get_logger
from .paths import AppPaths
from .secrets import SecretStore

log = get_logger(__name__)

OAUTH_AUTHORIZE = "https://openapi.baidu.com/oauth/2.0/authorize"
OAUTH_TOKEN = "https://openapi.baidu.com/oauth/2.0/token"
PAN_HOST = "https://pan.baidu.com"
PCS_LOCATE = "https://d.pcs.baidu.com/rest/2.0/pcs/file"
SHARE_ENDPOINT = "https://pan.baidu.com/apaas/1.0/share/set"
TOKEN_SECRET_KEY = "baidu_token"
BLOCK_SIZE = 4 * 1024 * 1024

_ERRNO_TEXT = {
    -1: "百度网盘参数错误",
    -3: "文件不存在",
    -4: "文件已存在",
    -6: "access_token 无效，请重新授权",
    -7: "文件或目录无权限（上传必须位于 /apps/{应用名} 下）",
    -8: "文件已存在",
    -9: "父目录不存在",
    -10: "云端容量不足",
    -11: "文件不存在或已删除",
    111: "access_token 已过期，请重新授权或刷新",
    31023: "参数错误",
    31066: "文件不存在",
    31190: "待上传文件不存在",
    31355: "uploadid 无效",
    31363: "文件大小超出限制",
    31400: "路径不合法",
    42000: "分享功能未开通或权限不足",
}


class NetdiskError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        errno: int | None = None,
        needs_auth: bool = False,
        needs_manual: bool = False,
        retryable: bool = False,
        raw: Any = None,
    ):
        super().__init__(message)
        self.errno = errno
        self.needs_auth = needs_auth
        self.needs_manual = needs_manual
        self.retryable = retryable
        self.raw = raw

    def as_dict(self) -> dict[str, Any]:
        return {
            "message": str(self),
            "errno": self.errno,
            "needs_auth": self.needs_auth,
            "needs_manual": self.needs_manual,
            "retryable": self.retryable,
        }


@dataclass
class NetdiskStatus:
    configured: bool
    authorized: bool
    app_key: str = ""
    app_name: str = ""
    root_dir: str = ""
    effective_root: str = ""
    user_name: str = ""
    vip_type: int = 0
    expires_at: float = 0.0
    message: str = ""
    share_configured: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "authorized": self.authorized,
            "app_key": self.app_key,
            "app_name": self.app_name,
            "root_dir": self.root_dir,
            "effective_root": self.effective_root,
            "user_name": self.user_name,
            "vip_type": self.vip_type,
            "expires_at": self.expires_at,
            "message": self.message,
            "share_configured": self.share_configured,
        }


@dataclass
class UploadBatchResult:
    product_dir: str
    uploads: list[UploadRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    verified: bool = False
    hash_verified: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.uploads) and all(
            item.status == "uploaded" for item in self.uploads
        )


def md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def sha256_file(path: str | Path, chunk: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def block_md5s(path: str | Path, size: int | None = None) -> list[str]:
    result: list[str] = []
    with open(path, "rb") as handle:
        while True:
            block = handle.read(BLOCK_SIZE)
            if not block:
                break
            result.append(md5_bytes(block))
    if not result:
        result.append(md5_bytes(b""))
    if size is not None and size == 0:
        return [md5_bytes(b"")]
    return result


class BaiduNetdisk:
    """Thin, testable wrapper around the official endpoints."""

    def __init__(
        self,
        settings: SettingsManager,
        secrets_store: SecretStore,
        db: Database | None = None,
        paths: AppPaths | None = None,
        client: httpx.Client | None = None,
    ):
        self.settings = settings
        self.secrets = secrets_store
        self.db = db
        self.paths = paths
        self._client = client

    # ------------------------------------------------------------------ config
    @property
    def app_key(self) -> str:
        return str(self.settings.get("netdisk.app_key", "") or "").strip()

    @property
    def app_secret(self) -> str:
        return self.secrets.get("baidu_app_secret")

    @property
    def app_name(self) -> str:
        return str(self.settings.get("netdisk.app_name", "") or "").strip().strip("/")

    @property
    def redirect_uri(self) -> str:
        return str(self.settings.get("netdisk.redirect_uri", "oob") or "oob").strip()

    @property
    def root_dir(self) -> str:
        return str(self.settings.get("netdisk.root_dir", "/爱做题的大肥鱼_商品库") or "").strip()

    @property
    def effective_root(self) -> str:
        root = "/" + self.root_dir.strip("/")
        if root.startswith("/apps/"):
            return root
        if not self.app_name:
            return ""
        return f"/apps/{self.app_name}{root}"

    def configured(self) -> bool:
        return bool(self.app_key and self.app_secret and self.app_name)

    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(120.0, connect=30.0),
                headers={"User-Agent": "pan.baidu.com"},
                follow_redirects=True,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ------------------------------------------------------------------- oauth
    def authorize_url(self, state: str = "") -> str:
        if not self.app_key:
            raise NetdiskError("尚未填写百度网盘 AppKey", needs_auth=True)
        params = {
            "response_type": "code",
            "client_id": self.app_key,
            "redirect_uri": self.redirect_uri,
            "scope": "basic,netdisk",
        }
        if state:
            params["state"] = state
        return f"{OAUTH_AUTHORIZE}?{httpx.QueryParams(params)}"

    def _token_data(self) -> dict[str, Any]:
        raw = self.secrets.get(TOKEN_SECRET_KEY)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def _save_token(self, payload: dict[str, Any]) -> None:
        payload = dict(payload)
        if payload.get("expires_in") and not payload.get("expires_at"):
            payload["expires_at"] = time.time() + int(payload["expires_in"]) - 60
        payload["saved_at"] = utc_now()
        # Keep the old refresh_token if the refresh response omits it.
        old = self._token_data()
        if not payload.get("refresh_token") and old.get("refresh_token"):
            payload["refresh_token"] = old["refresh_token"]
        self.secrets.set(TOKEN_SECRET_KEY, json.dumps(payload, ensure_ascii=False))

    def exchange_code(self, code: str) -> dict[str, Any]:
        code = code.strip()
        if not code:
            raise NetdiskError("授权码为空", needs_auth=True)
        if not (self.app_key and self.app_secret):
            raise NetdiskError("尚未填写 AppKey / SecretKey", needs_auth=True)
        response = self.client().get(
            OAUTH_TOKEN,
            params={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self.app_key,
                "client_secret": self.app_secret,
                "redirect_uri": self.redirect_uri,
            },
        )
        data = self._json(response, "换取 access_token 失败")
        if "error" in data or not data.get("access_token"):
            raise NetdiskError(
                f"换取 access_token 失败：{data.get('error_description') or data.get('error') or '未知错误'}",
                needs_auth=True,
                raw=data,
            )
        self._save_token(data)
        return data

    def refresh_token(self) -> dict[str, Any]:
        current = self._token_data()
        refresh = current.get("refresh_token", "")
        if not refresh:
            raise NetdiskError("没有 refresh_token，请重新授权", needs_auth=True)
        response = self.client().get(
            OAUTH_TOKEN,
            params={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": self.app_key,
                "client_secret": self.app_secret,
            },
        )
        data = self._json(response, "刷新 access_token 失败")
        if "error" in data or not data.get("access_token"):
            raise NetdiskError(
                f"刷新 access_token 失败：{data.get('error_description') or data.get('error') or '未知错误'}",
                needs_auth=True,
                raw=data,
            )
        self._save_token(data)
        return data

    def access_token(self) -> str:
        data = self._token_data()
        token = str(data.get("access_token", "") or "")
        if not token:
            raise NetdiskError("尚未授权百度网盘", needs_auth=True)
        expires_at = float(data.get("expires_at", 0) or 0)
        if expires_at and expires_at < time.time() + 60:
            token = str(self.refresh_token().get("access_token", ""))
        return token

    def has_token(self) -> bool:
        return bool(self._token_data().get("access_token"))

    def revoke_local_token(self) -> None:
        self.secrets.delete(TOKEN_SECRET_KEY)

    # ------------------------------------------------------------------ http
    @staticmethod
    def _json(response: httpx.Response, context: str) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            raise NetdiskError(
                f"{context}：服务器返回非 JSON（HTTP {response.status_code}）",
                retryable=response.status_code >= 500,
            )
        if not isinstance(data, dict):
            raise NetdiskError(f"{context}：服务器返回格式异常")
        return data

    @staticmethod
    def _check(data: dict[str, Any], context: str, *, allow_exists: bool = False) -> dict[str, Any]:
        raw_errno = data.get("errno", 0)
        try:
            errno = int(raw_errno) if raw_errno is not None else 0
        except (TypeError, ValueError):
            errno = None
        if errno == 0:
            return data
        if allow_exists and errno == -8:
            return data
        code = errno
        reason = _ERRNO_TEXT.get(code or 0) or data.get("errmsg") or data.get("show_msg") or "未知错误"
        raise NetdiskError(
            f"{context}失败（errno={errno}）：{reason}",
            errno=code,
            needs_auth=code in (-6, 111),
            needs_manual=code in (42000,),
            retryable=code in (42905, -10),
            raw=data,
        )

    def _get(self, url: str, params: dict[str, Any], context: str) -> dict[str, Any]:
        try:
            response = self.client().get(url, params=params)
        except httpx.HTTPError as exc:
            raise NetdiskError(f"{context}网络错误：{exc}", retryable=True)
        data = self._json(response, context)
        return self._check(data, context)

    def _post(
        self,
        url: str,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        context: str = "请求",
        *,
        allow_exists: bool = False,
        raw_response: bool = False,
    ) -> dict[str, Any]:
        try:
            response = self.client().post(url, params=params, data=data or {}, files=files)
        except httpx.HTTPError as exc:
            raise NetdiskError(f"{context}网络错误：{exc}", retryable=True)
        parsed = self._json(response, context)
        if raw_response:
            return parsed
        return self._check(parsed, context, allow_exists=allow_exists)

    # --------------------------------------------------------------- user info
    def user_info(self) -> dict[str, Any]:
        token = self.access_token()
        data = self._get(
            f"{PAN_HOST}/rest/2.0/xpan/nas",
            {"method": "uinfo", "access_token": token, "vip_version": "v2"},
            "获取用户信息",
        )
        return data

    def status(self, *, fetch_user: bool = False) -> NetdiskStatus:
        token_data = self._token_data()
        configured = self.configured()
        authorized = bool(token_data.get("access_token"))
        status = NetdiskStatus(
            configured=configured,
            authorized=authorized,
            app_key=(self.app_key[:4] + "…") if self.app_key else "",
            app_name=self.app_name,
            root_dir=self.root_dir,
            effective_root=self.effective_root,
            expires_at=float(token_data.get("expires_at", 0) or 0),
            share_configured=False,
        )
        if not configured:
            status.message = "请填写 AppKey、SecretKey 与应用名（应用名用于 /apps/{应用名} 路径）"
            return status
        if not self.effective_root:
            status.message = "无法计算云端根目录：请填写应用名，或将根目录设为 /apps/... 开头"
            return status
        if authorized and fetch_user:
            try:
                info = self.user_info()
                status.user_name = str(info.get("netdisk_name") or info.get("baidu_name") or "")
                status.vip_type = int(info.get("vip_type", 0) or 0)
                status.authorized = True
                status.message = "授权有效"
            except NetdiskError as exc:
                status.authorized = not exc.needs_auth
                status.message = str(exc)
        elif authorized:
            status.message = "已保存授权令牌"
        else:
            status.message = "尚未授权"
        return status

    # ------------------------------------------------------------- remote path
    def normalize_path(self, path: str) -> str:
        if not path:
            raise NetdiskError("云端路径为空")
        path = "/" + path.strip().replace("\\", "/").strip("/")
        while "//" in path:
            path = path.replace("//", "/")
        return path

    def guard_path(self, path: str) -> str:
        path = self.normalize_path(path)
        root = self.effective_root
        if not root:
            raise NetdiskError("未配置云端根目录；官方上传接口要求根目录位于 /apps/{应用名} 下")
        if path != root and not path.startswith(root + "/"):
            raise NetdiskError(f"拒绝访问根目录之外的路径：{path}")
        return path

    def product_remote_dir(self, product: Product) -> str:
        safe_sku = _safe_component(product.sku or "NO-SKU")
        safe_title = _safe_component(product.title or "未命名商品")
        return self.guard_path(f"{self.effective_root}/{safe_sku}_{safe_title}")

    # ------------------------------------------------------------- directory
    def _create_dir_once(self, path: str) -> dict[str, Any]:
        token = self.access_token()
        return self._post(
            f"{PAN_HOST}/rest/2.0/xpan/file",
            data={"path": path, "isdir": "1", "rtype": "0"},
            params={"method": "create", "access_token": token},
            context=f"创建目录 {path}",
            allow_exists=True,
        )

    def ensure_dir(self, path: str, *, create_root: bool = False) -> dict[str, Any]:
        path = self.guard_path(path)
        root = self.effective_root
        if path == root and not create_root:
            return {"path": path, "fs_id": 0, "created": False}
        parts = [part for part in path.strip("/").split("/") if part]
        current = ""
        last: dict[str, Any] = {}
        for part in parts:
            current += "/" + part
            if current == root and not create_root:
                continue
            try:
                last = self._create_dir_once(current)
            except NetdiskError as exc:
                if exc.errno == -8:
                    last = {"path": current, "exists": True}
                    continue
                raise
        return last

    def list_dir(self, remote_dir: str) -> list[dict[str, Any]]:
        token = self.access_token()
        remote_dir = self.guard_path(remote_dir)
        entries: list[dict[str, Any]] = []
        start = 0
        while True:
            data = self._get(
                f"{PAN_HOST}/rest/2.0/xpan/file",
                {
                    "method": "list",
                    "access_token": token,
                    "dir": remote_dir,
                    "start": start,
                    "limit": 1000,
                    "web": 0,
                },
                f"列出目录 {remote_dir}",
            )
            page = data.get("list") or []
            entries.extend(page)
            if len(page) < 1000:
                break
            start += len(page)
            if len(entries) > 20000:
                raise NetdiskError("目录文件数量异常，已停止列举")
        return entries

    def stat(self, remote_path: str, *, dlink: bool = False) -> dict[str, Any]:
        token = self.access_token()
        remote_path = self.guard_path(remote_path)
        params: dict[str, Any] = {"method": "meta", "access_token": token, "path": remote_path}
        if dlink:
            params["dlink"] = 1
        data = self._get(f"{PAN_HOST}/rest/2.0/xpan/file", params, f"查询文件 {remote_path}")
        return data

    # ------------------------------------------------------------------ upload
    def locate_upload_host(self, remote_path: str, uploadid: str) -> str:
        token = self.access_token()
        data = self._get(
            PCS_LOCATE,
            {
                "method": "locateupload",
                "appid": str(self.settings.get("netdisk.pcs_appid", "250528")),
                "access_token": token,
                "path": remote_path,
                "uploadid": uploadid,
                "upload_version": "2.0",
            },
            "获取上传域名",
        )
        host = str(data.get("host", "") or "").strip()
        if not host:
            servers = data.get("servers") or []
            if servers and isinstance(servers[0], dict):
                host = str(servers[0].get("server", "") or "")
        if not host:
            raise NetdiskError("上传域名响应中没有 host", raw=data)
        host = host.replace("https://", "").replace("http://", "").strip("/")
        return f"https://{host}"

    def upload_file(
        self,
        local_path: str | Path,
        remote_path: str,
        *,
        product_id: str = "",
        progress: Callable[[int, int], None] | None = None,
        rtype: int = 3,
    ) -> UploadRecord:
        local = Path(local_path)
        if not local.is_file():
            raise NetdiskError(f"本地文件不存在：{local}")
        remote_path = self.guard_path(remote_path)
        size = local.stat().st_size
        digest = hashlib.sha256()
        md5s: list[str] = []
        with open(local, "rb") as handle:
            while True:
                block = handle.read(BLOCK_SIZE)
                if not block:
                    break
                digest.update(block)
                md5s.append(md5_bytes(block))
        if not md5s:
            md5s = [md5_bytes(b"")]
        sha256 = digest.hexdigest()

        record = UploadRecord(
            product_id=product_id,
            local_path=str(local),
            remote_path=remote_path,
            size=size,
            sha256=sha256,
            status="uploading",
            attempts=1,
        )
        if self.db:
            self.db.save_upload(record)
        try:
            record = self._upload_blocks(local, remote_path, size, md5s, record, progress, rtype)
        except NetdiskError as exc:
            record.status = "failed"
            record.error = str(exc)
            if self.db:
                self.db.update_upload(record.id, status="failed", error=record.error, attempts=1)
            raise
        return record

    def _upload_blocks(
        self,
        local: Path,
        remote_path: str,
        size: int,
        md5s: list[str],
        record: UploadRecord,
        progress: Callable[[int, int], None] | None,
        rtype: int,
    ) -> UploadRecord:
        token = self.access_token()
        pre = self._post(
            f"{PAN_HOST}/rest/2.0/xpan/file",
            data={
                "path": remote_path,
                "size": str(size),
                "isdir": "0",
                "block_list": json.dumps(md5s),
                "autoinit": "1",
                "rtype": str(rtype),
            },
            params={"method": "precreate", "access_token": token},
            context=f"预上传 {remote_path}",
        )
        uploadid = str(pre.get("uploadid", "") or "")
        if not uploadid:
            raise NetdiskError("预上传响应缺少 uploadid", raw=pre)
        missing = pre.get("block_list") or []
        try:
            missing_indexes = sorted({int(item) for item in missing})
        except (TypeError, ValueError):
            missing_indexes = list(range(len(md5s)))
        if missing_indexes:
            host = self.locate_upload_host(remote_path, uploadid)
            with open(local, "rb") as handle:
                for index in missing_indexes:
                    if index < 0 or index >= len(md5s):
                        raise NetdiskError(f"预上传返回非法分片序号 {index}", raw=pre)
                    handle.seek(index * BLOCK_SIZE)
                    chunk = handle.read(BLOCK_SIZE)
                    if not chunk and size > 0:
                        raise NetdiskError(f"读取本地分片 {index} 失败")
                    expected = md5s[index]
                    if md5_bytes(chunk) != expected:
                        raise NetdiskError(f"本地文件在上传过程中发生变化（分片 {index}）")
                    data = self._post(
                        f"{host}/rest/2.0/pcs/superfile2",
                        params={
                            "method": "upload",
                            "access_token": token,
                            "type": "tmpfile",
                            "path": remote_path,
                            "uploadid": uploadid,
                            "partseq": index,
                        },
                        files={"file": ("blob", chunk, "application/octet-stream")},
                        context=f"上传分片 {index + 1}/{len(md5s)}",
                    )
                    remote_md5 = str(data.get("md5", "") or "")
                    if remote_md5 and remote_md5.lower() != expected.lower():
                        raise NetdiskError(
                            f"分片 {index} 的云端 MD5 与本地不一致，已停止",
                            raw={"expected": expected, "remote": remote_md5},
                        )
                    if progress:
                        done = min(size, (index + 1) * BLOCK_SIZE)
                        progress(done, size)
        finished = self._post(
            f"{PAN_HOST}/rest/2.0/xpan/file",
            data={
                "path": remote_path,
                "size": str(size),
                "isdir": "0",
                "block_list": json.dumps(md5s),
                "uploadid": uploadid,
                "rtype": str(rtype),
            },
            params={"method": "create", "access_token": token},
            context=f"创建云端文件 {remote_path}",
        )
        fs_id = int(finished.get("fs_id", 0) or 0)
        cloud_size = int(finished.get("size", size) or 0)
        if cloud_size and cloud_size != size:
            raise NetdiskError(f"云端文件大小不一致：本地 {size}，云端 {cloud_size}")
        record.remote_md5 = str(finished.get("md5", "") or "")
        record.status = "uploaded"
        record.error = ""
        record.attempts = 1
        if self.db:
            self.db.update_upload(
                record.id,
                status="uploaded",
                remote_md5=record.remote_md5,
                error="",
                size=size,
                sha256=record.sha256,
            )
        if progress:
            progress(size, size)
        return record

    # --------------------------------------------------------------- product
    def upload_product(
        self,
        product: Product,
        full_pdf: str | Path,
        quick_pdf: str | Path | None = None,
        image_files: Iterable[str | Path] | None = None,
        info_files: Iterable[str | Path] | None = None,
        source_pdf: str | Path | None = None,
        *,
        verify: bool = True,
        verify_download_hash: bool | None = None,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> UploadBatchResult:
        if not self.configured():
            raise NetdiskError("百度网盘尚未配置 AppKey/SecretKey/应用名")
        product_dir = self.product_remote_dir(product)
        dirs = {
            "pdf": f"{product_dir}/01_习题详解",
            "images": f"{product_dir}/02_商品图片",
            "info": f"{product_dir}/03_商品信息",
        }
        for path in dirs.values():
            self.ensure_dir(path)
        if source_pdf is not None:
            dirs["source"] = f"{product_dir}/04_原始教材"
            self.ensure_dir(dirs["source"])

        plan: list[tuple[Path, str]] = []
        plan.append((Path(full_pdf), f"{dirs['pdf']}/{_safe_component(Path(full_pdf).name)}"))
        if quick_pdf:
            plan.append((Path(quick_pdf), f"{dirs['pdf']}/{_safe_component(Path(quick_pdf).name)}"))
        for image in image_files or []:
            plan.append((Path(image), f"{dirs['images']}/{_safe_component(Path(image).name)}"))
        for info in info_files or []:
            plan.append((Path(info), f"{dirs['info']}/{_safe_component(Path(info).name)}"))
        if source_pdf is not None:
            plan.append((Path(source_pdf), f"{dirs['source']}/{_safe_component(Path(source_pdf).name)}"))

        result = UploadBatchResult(product_dir=product_dir)
        existing = {item["path"]: item for item in self.list_dir(product_dir)} if verify else {}
        for local, remote in plan:
            if not local.is_file():
                result.errors.append(f"缺少本地交付文件：{local}")
                continue
            try:
                stat = existing.get(remote)
                if (
                    stat
                    and not stat.get("isdir")
                    and int(stat.get("size", -1)) == local.stat().st_size
                ):
                    digest = sha256_file(local)
                    record = UploadRecord(
                        product_id=product.id,
                        local_path=str(local),
                        remote_path=remote,
                        size=local.stat().st_size,
                        sha256=digest,
                        status="uploaded",
                        attempts=0,
                    )
                    if self.db:
                        self.db.save_upload(record)
                    result.uploads.append(record)
                    if progress:
                        progress(remote, 1, 1)
                    continue
                record = self.upload_file(
                    local,
                    remote,
                    product_id=product.id,
                    progress=(
                        (lambda done, total, r=remote: progress(r, done, total))
                        if progress
                        else None
                    ),
                )
                result.uploads.append(record)
            except NetdiskError as exc:
                result.errors.append(f"{local.name}: {exc}")
                if exc.needs_auth:
                    break

        if verify:
            result.verified, result.hash_verified, detail = self.verify_product(
                product,
                result.uploads,
                verify_download_hash=verify_download_hash,
            )
            result.details.update(detail)
        else:
            result.verified = False
            result.details["note"] = "未执行云端核对"
        return result

    def verify_product(
        self,
        product: Product,
        uploads: Iterable[UploadRecord],
        *,
        verify_download_hash: bool | None = None,
    ) -> tuple[bool, bool, dict[str, Any]]:
        product_dir = self.product_remote_dir(product)
        remote_entries = {item["path"]: item for item in self.list_dir(product_dir)}
        details: dict[str, Any] = {"checked": 0, "size_ok": 0, "hash_ok": 0, "missing": [], "mismatch": []}
        all_size_ok = True
        all_hash_ok = True
        check_hash = (
            bool(self.settings.get("netdisk.verify_download_hash", False))
            if verify_download_hash is None
            else verify_download_hash
        )
        for record in uploads:
            details["checked"] += 1
            remote = remote_entries.get(record.remote_path)
            if not remote:
                details["missing"].append(record.remote_path)
                all_size_ok = False
                all_hash_ok = False
                continue
            if int(remote.get("size", -1)) != int(record.size):
                details["mismatch"].append(record.remote_path)
                all_size_ok = False
                all_hash_ok = False
                continue
            details["size_ok"] += 1
            if not check_hash:
                all_hash_ok = False
                record.status = "verified"
                if self.db:
                    self.db.update_upload(record.id, status="verified", error="")
                continue
            if not record.sha256:
                all_hash_ok = False
                continue
            actual = self.download_sha256(record.remote_path)
            if actual == record.sha256:
                details["hash_ok"] += 1
                record.status = "verified"
                if self.db:
                    self.db.update_upload(record.id, status="verified", error="")
            else:
                all_hash_ok = False
                details["mismatch"].append(record.remote_path)
        details["hash_checked"] = check_hash
        if not check_hash:
            details["hash_note"] = "未下载校验内容；百度 list/meta 的 md5 是云端哈希，不能等同于本地文件 MD5"
        details["verified"] = all_size_ok and not details["missing"] and not details["mismatch"]
        return details["verified"], bool(check_hash and all_hash_ok), details

    def download_sha256(self, remote_path: str) -> str:
        meta = self.stat(remote_path, dlink=True)
        dlink = str(meta.get("dlink", "") or "")
        if not dlink:
            raise NetdiskError(f"云端未返回下载直链：{remote_path}")
        token = self.access_token()
        digest = hashlib.sha256()
        try:
            with self.client().stream(
                "GET", dlink, params={"access_token": token}
            ) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes(1024 * 512):
                    digest.update(chunk)
        except httpx.HTTPError as exc:
            raise NetdiskError(f"下载校验失败 {remote_path}：{exc}", retryable=True)
        return digest.hexdigest()

    # ------------------------------------------------------------------ share
    def _share_password(self) -> str:
        mode = str(self.settings.get("netdisk.share_password_mode", "random") or "random")
        if mode in {"none", "无", "off"}:
            return ""
        configured = str(self.settings.get("netdisk.share_password", "") or "")
        if configured:
            return configured
        alphabet = string.ascii_lowercase + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(4))

    def create_share(self, product: Product, **kwargs) -> ShareRecord:
        return self.create_share_dir(
            self.product_remote_dir(product), product_id=product.id, **kwargs
        )

    def create_share_dir(
        self,
        remote_dir: str,
        *,
        product_id: str,
        period: int | None = None,
        password: str | None = None,
        fsid_list: list[int] | None = None,
    ) -> ShareRecord:
        if not self.app_key:
            raise NetdiskError("尚未填写百度网盘 AppKey")
        self.ensure_dir(remote_dir)
        if fsid_list is None:
            folder = self.stat(remote_dir)
            fs_id = int(folder.get("fs_id", 0) or 0)
            if not fs_id:
                listed = self.list_dir(remote_dir)
                matching = [item for item in listed if item.get("path") == remote_dir]
                fs_id = int(matching[0].get("fs_id", 0)) if matching else 0
            if not fs_id:
                raise NetdiskError("无法取得商品目录 fs_id，未创建分享")
            fsid_list = [fs_id]
        period_days = int(period if period is not None else self.settings.get("netdisk.share_period", 0))
        pwd = self._share_password() if password is None else password
        token = self.access_token()
        app_id = str(self.settings.get("netdisk.app_id", "") or "").strip() or self.app_key
        data = self._post(
            SHARE_ENDPOINT,
            params={"product": "netdisk", "appid": app_id, "access_token": token},
            data={
                "fsid_list": json.dumps(fsid_list),
                "period": str(period_days),
                "pwd": pwd,
            },
            context="创建分享链接",
            raw_response=True,
        )
        if int(data.get("errno", 0) or 0) != 0:
            errno = data.get("errno")
            raise NetdiskError(
                f"创建分享链接失败（errno={errno}）：该接口属于付费“文件分享服务”，"
                "需在开放平台购买并开通分享权限；当前未获得可用分享链接",
                errno=int(errno) if str(errno).lstrip("-").isdigit() else None,
                needs_manual=True,
                raw=data,
            )
        payload = data.get("data") or {}
        link = str(payload.get("link") or payload.get("short_url") or "")
        share = ShareRecord(
            product_id=product_id,
            url=link,
            password=str(payload.get("pwd") or pwd),
            period="permanent" if period_days == 0 else f"{period_days}天",
            status="created_unverified",
            verified=False,
            details={
                "share_id": payload.get("share_id"),
                "short_url": payload.get("short_url"),
                "period_days": period_days,
                "fsid_list": fsid_list,
                "api": "apaas/1.0/share/set",
                "note": "链接由官方接口创建；可访问性尚未通过人工/浏览器确认。",
            },
        )
        if self.db:
            self.db.add_share(share)
        return share

    def probe_share_link(self, url: str) -> dict[str, Any]:
        """Best-effort reachability probe.

        A 200 response from Baidu's share landing page does not prove that the
        recipient can open and download the files (Baidu may show a login or
        verification page).  The result is evidence, never an automatic pass.
        """
        result: dict[str, Any] = {
            "url": url,
            "reachable": False,
            "status_code": 0,
            "final_url": "",
            "markers": [],
            "verified": False,
            "note": "仅做页面可达性探测，不能等同于人工确认提取码与下载权限",
        }
        try:
            response = self.client().get(url, timeout=30.0)
            result["status_code"] = response.status_code
            result["final_url"] = str(response.url)
            text = response.text[:200000]
            result["reachable"] = 200 <= response.status_code < 400
            for marker in ("分享的文件已经被取消", "你访问的页面不存在", "链接不存在", "分享已过期", "输入提取码"):
                if marker in text:
                    result["markers"].append(marker)
            if result["reachable"] and not any(
                marker in result["markers"]
                for marker in ("分享的文件已经被取消", "你访问的页面不存在", "链接不存在", "分享已过期")
            ):
                result["note"] = "页面可访问；仍需人工确认提取码、文件列表与下载权限"
        except httpx.HTTPError as exc:
            result["note"] = f"页面探测失败：{exc}"
        return result

    def record_manual_verification(self, share_id: str, *, passed: bool, note: str = "") -> None:
        if not self.db:
            raise NetdiskError("数据库不可用，无法记录分享验证")
        row = self.db.query_one("SELECT details_json FROM shares WHERE id=?", (share_id,))
        if not row:
            raise NetdiskError("分享记录不存在")
        try:
            details = json.loads(row["details_json"] or "{}")
        except (ValueError, TypeError):
            details = {}
        details["manual_verification"] = {"passed": passed, "note": note, "at": utc_now()}
        self.db.execute(
            "UPDATE shares SET verified=?, verified_at=?, status=?, details_json=? WHERE id=?",
            (
                1 if passed else 0,
                utc_now() if passed else "",
                "verified_manual" if passed else "needs_manual_verification",
                json.dumps(details, ensure_ascii=False),
                share_id,
            ),
        )


def _safe_component(value: str) -> str:
    cleaned = "".join(ch for ch in value if ch not in '\\/:*?"<>|\r\n\t').strip()
    cleaned = cleaned.replace(" ", "_")
    return cleaned[:100] or "未命名"


def format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    if value < 1024 * 1024 * 1024:
        return f"{value / 1024 / 1024:.1f} MB"
    return f"{value / 1024 / 1024 / 1024:.2f} GB"
