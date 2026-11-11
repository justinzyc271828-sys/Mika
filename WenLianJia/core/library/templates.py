"""Product-family templates and the per-book override resolver.

The family is a reusable production and sales specification.  A new book copies
the family's current values; a book may override individual leaves.  Updating a
family therefore never rewrites an existing book unless the owner explicitly
runs the bulk-apply action.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from models.schemas import ProductFamily

DEFAULT_FAMILY_CODE = "xianyu-jiaofu"
DEFAULT_FAMILY_NAME = "闲鱼教辅"

DEFAULT_FAMILY: dict[str, Any] = {
    "code": DEFAULT_FAMILY_CODE,
    "name": DEFAULT_FAMILY_NAME,
    "description": "闲鱼教辅默认商品系列模板：教材习题解析 PDF、商品图片与网盘交付。",
    "is_default": True,
    "brand_name": "全网同名爱做题的大肥鱼",
    "visual_style": {
        "palette": ["#F5F1E8", "#2F3A45", "#C4553B"],
        "font": "Microsoft YaHei",
        "cover_layout": "top-title-bottom-badge",
    },
    "image_template": {
        "cover_width": 1200,
        "cover_height": 1600,
        "gallery_count": 7,
        "show_publisher_cover": True,
        "show_watermark": True,
    },
    "detail_template": {
        "sections": ["教材信息", "包含内容", "交付方式", "版本说明"],
        "show_page_count": True,
        "show_table_of_contents": True,
    },
    "intro_template": (
        "《{title}》{volume}习题答案与详细解析。"
        "包含 {exercise_count} 道习题的完整解答，保留原书页码，PDF 排版清晰。"
        "下单后通过百度网盘交付，文件可长期保存。"
    ),
    "pdf_template": "templates/latex/solution.tex.j2",
    "watermark": {
        "text": "全网同名爱做题的大肥鱼",
        "font": "msyh.ttc",
        "size": 42,
        "color": "#9AA0A6",
        "opacity": 0.18,
        "rotation": 35,
        "position": "center",
    },
    "pricing": {
        "currency": "CNY",
        "min_price_cents": 100,
        "round_to_cents": 100,
        "bundle_discount": 0.9,
        "allow_above_parts": False,
    },
    "default_category_id": "",
    "delivery_rules": {
        "require_manifest": True,
        "require_receipt_reminder": True,
        "no_logistics": True,
        "keep_old_version_files": True,
    },
    "netdisk_rules": {
        "root": "/爱做题的大肥鱼_商品库",
        "share_period": 0,
        "random_password": True,
        "one_folder_per_sku": True,
    },
}

# Leaves that may be overridden per book and the range/type they must satisfy.
_OVERRIDE_VALIDATORS = {
    "watermark.opacity": lambda value: isinstance(value, (int, float)) and 0 <= value <= 1,
    "watermark.size": lambda value: isinstance(value, int) and 8 <= value <= 200,
    "watermark.rotation": lambda value: isinstance(value, (int, float)) and -90 <= value <= 90,
    "pricing.min_price_cents": lambda value: isinstance(value, int) and value >= 0,
    "pricing.round_to_cents": lambda value: isinstance(value, int) and value >= 1,
    "pricing.bundle_discount": lambda value: isinstance(value, (int, float)) and 0 < value <= 1,
    "image_template.gallery_count": lambda value: isinstance(value, int) and 1 <= value <= 20,
}


def family_payload(family: ProductFamily) -> dict[str, Any]:
    return {
        "code": family.code,
        "name": family.name,
        "description": family.description,
        "is_default": family.is_default,
        "brand_name": family.brand_name,
        "visual_style": copy.deepcopy(family.visual_style),
        "image_template": copy.deepcopy(family.image_template),
        "detail_template": copy.deepcopy(family.detail_template),
        "intro_template": family.intro_template,
        "pdf_template": family.pdf_template,
        "watermark": copy.deepcopy(family.watermark),
        "pricing": copy.deepcopy(family.pricing),
        "default_category_id": family.default_category_id,
        "delivery_rules": copy.deepcopy(family.delivery_rules),
        "netdisk_rules": copy.deepcopy(family.netdisk_rules),
    }


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _leaf_paths(value: Any, prefix: str = "") -> dict[str, Any]:
    if not isinstance(value, dict):
        return {prefix: value}
    result: dict[str, Any] = {}
    for key, item in value.items():
        child = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            result.update(_leaf_paths(item, child))
        else:
            result[child] = item
    return result


def validate_overrides(overrides: dict[str, Any]) -> list[str]:
    """Return human-readable validation errors for a per-book override map."""

    errors: list[str] = []
    for path, value in _leaf_paths(overrides).items():
        validator = _OVERRIDE_VALIDATORS.get(path)
        if validator is not None and not validator(value):
            errors.append(f"{path} 的取值不合法：{value!r}")
    return errors


@dataclass
class ResolvedTemplate:
    data: dict[str, Any]
    sources: dict[str, str] = field(default_factory=dict)

    def get(self, path: str, default: Any = None) -> Any:
        current: Any = self.data
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current

    def source_of(self, path: str) -> str:
        return self.sources.get(path, "family")

    def format_intro(self, values: dict[str, Any]) -> str:
        template = str(self.data.get("intro_template") or "")
        try:
            return template.format(**values)
        except (KeyError, ValueError):
            return template


def resolve_template(
    family: ProductFamily, overrides: dict[str, Any] | None = None
) -> ResolvedTemplate:
    base = family_payload(family)
    merged = deep_merge(base, overrides or {})
    sources: dict[str, str] = {}
    for path in _leaf_paths(base):
        sources[path] = "family"
    for path in _leaf_paths(overrides or {}):
        sources[path] = "override"
    return ResolvedTemplate(data=merged, sources=sources)
