"""Prompt templates loaded from ``prompts/`` with simple placeholder rendering."""

from __future__ import annotations

from pathlib import Path

from .paths import PROJECT_ROOT


class PromptLibrary:
    def __init__(self, root: Path | None = None):
        self.root = root or (PROJECT_ROOT / "prompts")
        self._cache: dict[str, str] = {}

    def load(self, name: str) -> str:
        if name not in self._cache:
            path = self.root / name
            if not path.exists():
                raise FileNotFoundError(f"提示词不存在：{path}")
            self._cache[name] = path.read_text(encoding="utf-8")
        return self._cache[name]

    def render(self, name: str, **values: object) -> str:
        text = self.load(name)
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", str(value))
        return text
