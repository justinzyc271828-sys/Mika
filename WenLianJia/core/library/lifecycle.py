"""The nine-state selling lifecycle and its explicit transition table.

Production status (``products.status``) is owned by the pipeline.  A SKU's
selling lifecycle is separate: a finished production can still be a draft SKU,
and a published SKU can be paused without touching its PDFs.
"""

from __future__ import annotations

from models.schemas import SkuLifecycle

LIFECYCLE_LABELS: dict[SkuLifecycle, str] = {
    SkuLifecycle.DRAFT: "草稿",
    SkuLifecycle.PENDING_PRODUCTION: "等待生产",
    SkuLifecycle.PRODUCING: "生产中",
    SkuLifecycle.REVIEW_REQUIRED: "待审核",
    SkuLifecycle.READY: "已准备完成",
    SkuLifecycle.PUBLISHED: "已发布",
    SkuLifecycle.PAUSED: "暂停销售",
    SkuLifecycle.ARCHIVED: "已归档",
    SkuLifecycle.FAILED: "生产失败",
}

TRANSITIONS: dict[SkuLifecycle, set[SkuLifecycle]] = {
    SkuLifecycle.DRAFT: {
        SkuLifecycle.PENDING_PRODUCTION,
        SkuLifecycle.ARCHIVED,
    },
    SkuLifecycle.PENDING_PRODUCTION: {
        SkuLifecycle.PRODUCING,
        SkuLifecycle.FAILED,
        SkuLifecycle.ARCHIVED,
    },
    SkuLifecycle.PRODUCING: {
        SkuLifecycle.REVIEW_REQUIRED,
        SkuLifecycle.FAILED,
        SkuLifecycle.ARCHIVED,
    },
    SkuLifecycle.REVIEW_REQUIRED: {
        SkuLifecycle.READY,
        SkuLifecycle.PRODUCING,
        SkuLifecycle.FAILED,
        SkuLifecycle.ARCHIVED,
    },
    SkuLifecycle.READY: {
        SkuLifecycle.PUBLISHED,
        SkuLifecycle.PAUSED,
        SkuLifecycle.ARCHIVED,
    },
    SkuLifecycle.PUBLISHED: {
        SkuLifecycle.PAUSED,
        SkuLifecycle.ARCHIVED,
    },
    SkuLifecycle.PAUSED: {
        SkuLifecycle.PUBLISHED,
        SkuLifecycle.ARCHIVED,
    },
    SkuLifecycle.ARCHIVED: {
        SkuLifecycle.DRAFT,
    },
    SkuLifecycle.FAILED: {
        SkuLifecycle.PENDING_PRODUCTION,
        SkuLifecycle.ARCHIVED,
    },
}


def can_transition(current: SkuLifecycle, target: SkuLifecycle) -> bool:
    if current == target:
        return True
    return target in TRANSITIONS.get(current, set())


def require_transition(current: SkuLifecycle, target: SkuLifecycle) -> None:
    if not can_transition(current, target):
        raise ValueError(
            f"SKU 生命周期不能从 {LIFECYCLE_LABELS[current]} 直接变为 "
            f"{LIFECYCLE_LABELS[target]}"
        )


def timestamps_for(target: SkuLifecycle, now: str) -> dict[str, str]:
    if target == SkuLifecycle.PUBLISHED:
        return {"published_at": now, "paused_at": ""}
    if target == SkuLifecycle.PAUSED:
        return {"paused_at": now}
    if target == SkuLifecycle.ARCHIVED:
        return {"archived_at": now}
    if target == SkuLifecycle.DRAFT:
        return {"archived_at": "", "paused_at": ""}
    return {}
