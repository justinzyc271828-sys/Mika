"""Dependency-free DPAPI secret storage for Windows.

The application never writes an API key into JSON, logs or the repository.
On Windows the key is encrypted with the current user's DPAPI master key and
stored as a base64 blob.  On non-Windows development machines the module
refuses to persist secrets instead of silently writing plaintext.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .paths import PROJECT_ROOT

_SECRET_KEYS = (
    "openai_api_key",
    "baidu_app_secret",
    "image_api_key",
    "deepseek_api_key",
)


class SecretStoreError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _dpapi_protect(data: bytes) -> bytes:
    if sys.platform != "win32":
        raise SecretStoreError("Secret storage requires Windows DPAPI")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in, keep_in = _blob(data)
    blob_out = _DataBlob()
    if not crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        "WenLianJia",
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    ):
        raise SecretStoreError("CryptProtectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)
        del keep_in


def _dpapi_unprotect(data: bytes) -> bytes:
    if sys.platform != "win32":
        raise SecretStoreError("Secret storage requires Windows DPAPI")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in, keep_in = _blob(data)
    blob_out = _DataBlob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    ):
        raise SecretStoreError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)
        del keep_in


class SecretStore:
    """Small encrypted key/value store with environment fallback."""

    def __init__(self, path: Path):
        self.path = path
        load_dotenv(PROJECT_ROOT / ".env", override=False)

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            packed = json.loads(self.path.read_text(encoding="utf-8"))
            raw = base64.b64decode(packed["blob"])
            return json.loads(_dpapi_unprotect(raw).decode("utf-8"))
        except (OSError, ValueError, KeyError, SecretStoreError):
            return {}

    def _write(self, values: dict[str, str]) -> None:
        payload = json.dumps(values, ensure_ascii=False).encode("utf-8")
        encrypted = _dpapi_protect(payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps({"version": 1, "blob": base64.b64encode(encrypted).decode("ascii")}),
            encoding="utf-8",
        )
        os.replace(temp, self.path)

    def get(self, key: str) -> str:
        env_name = key.upper()
        if env_name in os.environ and os.environ[env_name].strip():
            return os.environ[env_name].strip()
        return self._read().get(key, "")

    def set(self, key: str, value: str) -> None:
        values = self._read()
        if value:
            values[key] = value
        else:
            values.pop(key, None)
        self._write(values)

    def delete(self, key: str) -> None:
        self.set(key, "")

    def has(self, key: str) -> bool:
        return bool(self.get(key))

    def describe(self) -> dict[str, bool]:
        return {key: bool(self.get(key)) for key in _SECRET_KEYS}

    def as_public_dict(self) -> dict[str, Any]:
        """Never returns secret values; used by the settings UI."""
        return {
            "path": str(self.path),
            "keys": self.describe(),
            "dpapi_available": sys.platform == "win32",
        }
