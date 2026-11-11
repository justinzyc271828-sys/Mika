"""Configuration management.

Defaults live in code so a fresh checkout is runnable.  User changes are stored
as JSON under ``WenLianJia_Data/03_系统配置``.  Secrets are deliberately absent
from this file and are handled by :mod:`core.secrets`.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

from .paths import AppPaths, get_paths

DEFAULTS: dict[str, Any] = {
    "app": {
        "version": "1.1.0",
        "brand": "全网同名爱做题的大肥鱼",
        "organization": "爱做题的大肥鱼",
        "last_open_product": "",
    },
    "openai": {
        "base_url": "",
        "model": "gpt-4.1",
        "review_model": "",
        "escalation_model": "",
        "fast_model": "",
        "api_mode": "responses",
        "timeout_seconds": 240,
        "max_retries": 3,
        "max_concurrency": 4,
        "max_output_tokens": 4500,
        "max_images": 2,
        "temperature": 0.2,
        "verify_enabled": True,
        "auto_escalate": False,
        "prompt_version": "1.0",
    },
    "budget": {
        "enforce": False,
        "currency": "USD",
        "per_book": 0.0,
        "daily": 0.0,
        "prices": {},
    },
    "deepseek": {
        "enabled": False,
        "base_url": "https://api.deepseek.com",
        "search_path": "/v1/tools/search",
        "fetch_path": "/v1/tools/fetch",
        "timeout_seconds": 60,
        "max_retries": 2,
        "search_count": 10,
        "need_content": False,
        "cache_days": 30,
        "min_authority": 2.0,
        "min_score": 1.5,
        "max_candidates": 8,
        "search_price_per_1000": 0.75,
        "fetch_price_per_1000": 0.75,
    },
    "books": {
        "auto_lookup": True,
        "allow_manual_candidate": True,
        "cover_min_width": 300,
        "cover_min_height": 400,
        "max_cover_bytes": 12000000,
        "title_similarity": 0.6,
        "require_verified_cover": False,
    },
    "image_api": {
        "enabled": False,
        "base_url": "",
        "model": "",
        "size": "1024x1536",
        "quality": "medium",
        "timeout_seconds": 180,
    },
    "watermark": {
        "text": "全网同名爱做题的大肥鱼",
        "font": "C:/Windows/Fonts/msyh.ttc",
        "font_index": 0,
        "font_size": 42,
        "color": "#9AA0A6",
        "opacity": 0.18,
        "rotation": 35,
        "position": "center",
        "tile": False,
        "tile_gap": 260,
    },
    "latex": {
        "engine": "xelatex",
        "xelatex_path": "",
        "font_main": "Microsoft YaHei",
        "font_latin": "Cambria",
        "font_song": "SimSun",
        "font_hei": "SimHei",
        "paper": "a4paper",
        "margin_mm": 22,
        "compile_timeout": 240,
        "keep_aux": False,
        "toc": True,
        "quick_answer_edition": True,
    },
    "pipeline": {
        "max_concurrency": 4,
        "auto_verify": True,
        "reuse_by_hash": True,
        "stop_on_error": False,
        "sample_pages": 6,
        "ocr_dpi": 180,
        "render_dpi": 150,
        "min_text_chars": 80,
    },
    "netdisk": {
        "app_key": "",
        "app_name": "",
        "app_id": "",
        "pcs_appid": "250528",
        "redirect_uri": "oob",
        "root_dir": "/爱做题的大肥鱼_商品库",
        "create_product_dirs": True,
        "share_period": 0,
        "share_password_mode": "random",
        "share_password": "",
        "upload_chunk_mb": 4,
        "verify_download_hash": False,
        "auto_upload": False,
        "auto_share": False,
        "include_source": False,
    },
    "cleanup": {
        "enabled": False,
        "stage_days": 7,
        "delete_source": False,
        "require_cloud_verification": True,
    },
    "ui": {
        "theme": "light",
        "language": "zh_CN",
        "window_width": 1440,
        "window_height": 900,
    },
    "logging": {
        "level": "INFO",
        "max_mb": 10,
        "backups": 10,
    },
}


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class SettingsManager:
    """Thread-safe JSON settings with atomic writes."""

    def __init__(self, paths: AppPaths, path: Path | None = None):
        self.paths = paths
        self.path = path or paths.settings
        self._lock = threading.RLock()
        self._data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> dict[str, Any]:
        with self._lock:
            if self.path.exists():
                try:
                    saved = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(saved, dict):
                        self._data = _deep_merge(DEFAULTS, saved)
                except (OSError, ValueError):
                    self._data = copy.deepcopy(DEFAULTS)
            else:
                self._data = copy.deepcopy(DEFAULTS)
                self.save()
            return copy.deepcopy(self._data)

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temp, self.path)

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def get(self, dotted: str, default: Any = None) -> Any:
        with self._lock:
            node: Any = self._data
            for part in dotted.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return copy.deepcopy(node)

    def set(self, dotted: str, value: Any, *, save: bool = True) -> None:
        parts = dotted.split(".")
        with self._lock:
            node = self._data
            for part in parts[:-1]:
                if not isinstance(node.get(part), dict):
                    node[part] = {}
                node = node[part]
            node[parts[-1]] = value
            if save:
                self.save()

    def update(self, values: dict[str, Any], *, save: bool = True) -> None:
        with self._lock:
            self._data = _deep_merge(self._data, values)
            if save:
                self.save()

    def reset_section(self, section: str) -> None:
        with self._lock:
            self._data[section] = copy.deepcopy(DEFAULTS.get(section, {}))
            self.save()

    def effective_root(self) -> Path:
        return self.paths.root


def load_settings(data_root: Path | None = None) -> tuple[SettingsManager, AppPaths]:
    paths = get_paths(data_root)
    return SettingsManager(paths), paths


def ensure_default_model_profiles(paths: AppPaths) -> Path:
    """Create a price table with no invented prices.

    The user must fill in the prices that apply to their own account.  Until
    then the application reports token counts and marks costs as unknown.
    """

    path = paths.model_profiles
    if path.exists():
        return path
    payload = {
        "version": 1,
        "note": "Prices must be filled by the owner; empty prices yield unknown cost, never a guessed value.",
        "profiles": {
            "main": {"model": "gpt-4.1", "input_per_million": None, "output_per_million": None},
            "review": {"model": "", "input_per_million": None, "output_per_million": None},
            "fast": {"model": "", "input_per_million": None, "output_per_million": None},
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_model_profiles(paths: AppPaths) -> dict[str, Any]:
    path = ensure_default_model_profiles(paths)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "profiles": {}}


def environment_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
