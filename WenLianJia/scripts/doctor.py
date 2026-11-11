"""Environment and credential doctor for WenLianJia."""

from __future__ import annotations

import importlib
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REQUIRED_MODULES = [
    "PySide6",
    "pymupdf",
    "openai",
    "PIL",
    "sympy",
    "jinja2",
    "pydantic",
    "httpx",
    "qrcode",
    "rapidocr",
    "onnxruntime",
]


def status(ok: bool, label: str, detail: str = "") -> bool:
    mark = "OK  " if ok else "MISS"
    print(f"[{mark}] {label}{(': ' + detail) if detail else ''}")
    return ok


def info(label: str, detail: str = "") -> None:
    print(f"[INFO] {label}{(': ' + detail) if detail else ''}")


def main() -> int:
    failures = 0
    print(f"python: {sys.version.split()[0]}  ({sys.executable})")
    if sys.version_info < (3, 11):
        print("Python 3.11+ is required.")
        failures += 1

    for module in REQUIRED_MODULES:
        try:
            importlib.import_module(module)
            status(True, f"import {module}")
        except Exception as exc:  # noqa: BLE001 - doctor reports the real error
            failures += 1
            status(False, f"import {module}", str(exc))

    try:
        from core.config import SettingsManager
        from core.db import Database
        from core.latex_utils import find_xelatex
        from core.paths import get_paths
        from core.secrets import SecretStore
        from core.ai_client import AiClient
        from core.baidu_netdisk import BaiduNetdisk
        from core.book_lookup import DeepSeekTools

        paths = get_paths()
        status(True, "data root", str(paths.root))
        status(paths.root.is_dir() and os.access(paths.root, os.W_OK), "data root writable")
        settings = SettingsManager(paths)
        db = Database(paths.database)
        db.query("SELECT 1")
        status(True, "database", str(paths.database))
        store = SecretStore(paths.secrets)
        key = store.get("openai_api_key")
        info("OpenAI key", "configured" if key else "not configured")
        ai = AiClient(settings, store, db=db)
        info("OpenAI config", ai.configured()[1])
        netdisk = BaiduNetdisk(settings, store, db=db, paths=paths)
        info("Baidu config", netdisk.status().message)
        deepseek = DeepSeekTools(settings, store, db=db)
        info("DeepSeek key", "configured" if store.get("deepseek_api_key") else "not configured")
        info("DeepSeek config", deepseek.configured()[1])

        try:
            xelatex = find_xelatex(settings)
            status(True, "xelatex", str(xelatex))
        except FileNotFoundError as exc:
            failures += 1
            status(False, "xelatex", str(exc))
        fonts = [Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/simhei.ttf")]
        status(all(font.is_file() for font in fonts), "Chinese fonts", ", ".join(f.name for f in fonts))
    except Exception as exc:  # noqa: BLE001
        failures += 1
        status(False, "project imports", str(exc))

    print()
    if failures:
        print(f"doctor found {failures} blocking issue(s)")
        return 1
    print("doctor: all required local checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
