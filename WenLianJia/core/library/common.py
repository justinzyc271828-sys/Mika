"""Small shared types for the product-library layer."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class LibraryError(Exception):
    """A user-correctable catalogue or library error."""


@dataclass
class Page(Generic[T]):
    items: list[T] = field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 50

    @property
    def pages(self) -> int:
        if self.page_size <= 0:
            return 1
        return max(1, (self.total + self.page_size - 1) // self.page_size)

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": self.items,
            "total": self.total,
            "page": self.page,
            "page_size": self.page_size,
            "pages": self.pages,
        }


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def clamp_page(page: int, page_size: int, *, max_page_size: int = 500) -> tuple[int, int]:
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 50), max_page_size))
    return page, page_size
