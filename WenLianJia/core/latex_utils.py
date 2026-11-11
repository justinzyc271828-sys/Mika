"""LaTeX helpers: escaping, model-text handling and engine discovery."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from .config import SettingsManager
from .logging_setup import get_logger

logger = get_logger("latex")

_SPECIAL = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}

_MATH_SPLIT = re.compile(r"(\$\$.*?\$\$|\$.*?\$|\\\[.*?\\\]|\\\(.*?\\\))", re.S)
_DISPLAY_MATH_SPLIT = re.compile(r"(\$\$.*?\$\$|\\\[.*?\\\])", re.S)


def escape_latex(text: str) -> str:
    return "".join(_SPECIAL.get(char, char) for char in str(text or ""))


def _strip_math_delimiters(segment: str) -> str:
    segment = segment.strip()
    for left, right in (("$$", "$$"), ("$", "$"), ("\\[", "\\]"), ("\\(", "\\)")):
        if segment.startswith(left) and segment.endswith(right):
            return segment[len(left) : len(segment) - len(right)].strip()
    return segment


def latex_text(text: str) -> str:
    """Escape plain text while preserving explicit inline/display math."""
    text = str(text or "").replace("\r\n", "\n")
    parts: list[str] = []
    for segment in _MATH_SPLIT.split(text):
        if not segment:
            continue
        stripped = segment.strip()
        if stripped.startswith("$$") and stripped.endswith("$$") and len(stripped) > 4:
            parts.append("\\[" + _strip_math_delimiters(stripped) + "\\]")
        elif stripped.startswith("$") and stripped.endswith("$") and len(stripped) > 2:
            parts.append("$" + _strip_math_delimiters(stripped) + "$")
        elif stripped.startswith("\\[") and stripped.endswith("\\]"):
            parts.append("\\[" + _strip_math_delimiters(stripped) + "\\]")
        elif stripped.startswith("\\(") and stripped.endswith("\\)"):
            parts.append("$" + _strip_math_delimiters(stripped) + "$")
        else:
            parts.append(escape_latex(segment))
    return "".join(parts)


def latex_multiline(text: str) -> str:
    """Escape model text, preserving display math and readable line structure.

    Model answers often contain Markdown-style bullets and one newline per line.
    A raw newline is only a space in LaTeX, so bullets were previously flattened
    into one long paragraph.  Plain lines become paragraphs, consecutive
    ``-``/``*`` lines become an itemize block, and numbered lines become an
    enumerate block.  Display/inline math is rendered separately so a multi-line
    ``$$...$$`` block is never split.
    """

    raw = str(text or "").replace("\r\n", "\n")
    if not raw.strip():
        return ""
    parts: list[str] = []
    position = 0
    for match in _DISPLAY_MATH_SPLIT.finditer(raw):
        parts.append(_plain_blocks(raw[position : match.start()]))
        parts.append(latex_text(match.group(0)))
        position = match.end()
    parts.append(_plain_blocks(raw[position:]))
    return "".join(part for part in parts if part).strip()


def _plain_blocks(text: str) -> str:
    """Render plain text lines as paragraphs, bullet lists and numbered lists."""

    blocks: list[str] = []
    paragraph: list[str] = []
    bullets: list[str] = []
    numbered: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(latex_text(" ".join(part.strip() for part in paragraph)))
            paragraph.clear()

    def flush_bullets() -> None:
        if bullets:
            items = "\n".join("\\item " + latex_text(item) for item in bullets)
            blocks.append(
                "\\begin{itemize}[leftmargin=1.8em,itemsep=2pt,topsep=2pt]\n"
                + items
                + "\n\\end{itemize}"
            )
            bullets.clear()

    def flush_numbered() -> None:
        if numbered:
            items = "\n".join("\\item " + latex_text(item) for item in numbered)
            blocks.append(
                "\\begin{enumerate}[label=\\arabic*.,leftmargin=2.2em,itemsep=2pt,topsep=2pt]\n"
                + items
                + "\n\\end{enumerate}"
            )
            numbered.clear()

    def flush_all() -> None:
        flush_paragraph()
        flush_bullets()
        flush_numbered()

    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            flush_all()
            continue
        if re.match(r"^[-*]\s+", stripped):
            flush_paragraph()
            flush_numbered()
            bullets.append(re.sub(r"^[-*]\s+", "", stripped))
            continue
        if re.match(r"^\d+[.)、]\s+", stripped):
            flush_paragraph()
            flush_bullets()
            numbered.append(re.sub(r"^\d+[.)、]\s+", "", stripped))
            continue
        flush_bullets()
        flush_numbered()
        paragraph.append(stripped)
    flush_all()
    return "\n\n".join(blocks)


def latex_display(value: str) -> str:
    """Render an answer field as display math when it looks mathematical."""
    value = str(value or "").strip()
    if not value:
        return ""
    stripped = _strip_math_delimiters(value)
    if re.search(r"[\u3400-\u9fff]", stripped):
        return latex_text(value)
    if re.search(r"\\[a-zA-Z]+|[\^_{}]|=", stripped):
        return "\\[\n" + stripped + "\n\\]"
    return latex_text(value)


def latex_inline_answer(value: str) -> str:
    """Compact answer rendering for table cells (no display-math blocks)."""
    value = str(value or "").strip()
    if not value:
        return ""
    stripped = _strip_math_delimiters(value)
    if re.search(r"[\u3400-\u9fff]", stripped):
        return latex_text(value)
    if re.search(r"\\[a-zA-Z]+|[\^_{}]|=", stripped):
        return "$" + stripped + "$"
    return latex_text(value)


def sanitize_filename(name: str, fallback: str = "document") -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name or "")).strip(" .")
    name = re.sub(r"\s+", " ", name)
    return (name or fallback)[:120]


def find_xelatex(settings: SettingsManager) -> Path:
    configured = str(settings.get("latex.xelatex_path", "") or "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    which = shutil.which("xelatex")
    if which:
        candidates.append(Path(which))
    for base in (r"D:\Workspaces\Tool\TeXLive\bin\windows", r"C:\texlive\2025\bin\windows", r"C:\texlive\2024\bin\windows"):
        candidates.append(Path(base) / "xelatex.exe")
    env_tex = os.environ.get("TEXLIVE_BIN", "")
    if env_tex:
        candidates.append(Path(env_tex) / "xelatex.exe")
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "未找到 xelatex。请安装 TeX Live，或在设置中填写 latex.xelatex_path。"
    )


def xelatex_version(exe: Path) -> str:
    try:
        result = subprocess.run(
            [str(exe), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return (result.stdout or result.stderr or "").splitlines()[0].strip()
    except Exception as exc:  # noqa: BLE001 - diagnostic only
        return f"unknown ({exc})"
