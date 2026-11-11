"""Build two small synthetic volumes for the two-volume SKU acceptance test.

This is deliberately synthetic test content, not a real textbook.  Each volume
has its own chapters, exercise text and cover title so the upper/lower volume
production paths cannot accidentally share answers.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pymupdf as fitz

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from build_sample import BODY, HEADING1, HEADING2, PageWriter  # noqa: E402


def build_volume(path: Path, volume: str) -> Path:
    upper = volume == "上册"
    doc = fitz.open()
    title = "线性代数 上册" if upper else "线性代数 下册"
    chapter1 = "第一章  向量空间" if upper else "第一章  特征值与特征向量"
    chapter2 = "第二章  线性方程组" if upper else "第二章  二次型"
    section1 = "1.1  基本概念" if upper else "1.1  特征值"
    section2 = "2.1  消元法" if upper else "2.1  二次型的标准形"
    exercises1 = (
        [
            "1. 证明：数域 F 上线性空间的零向量唯一。",
            "2. 判断集合 {(x,y) | x + y = 1} 是否构成 R 上的线性空间。",
        ]
        if upper
        else [
            "1. 求矩阵 A = [[2,1],[1,2]] 的全部特征值。",
            "2. 证明：相似矩阵具有相同的特征多项式。",
        ]
    )
    exercises2 = (
        [
            "1. 解线性方程组 x + y = 2, 2x - y = 1。",
            "2. 讨论方程组 x + y + z = 1, 2x + 2y + 2z = 2 的解的情况。",
        ]
        if upper
        else [
            "1. 写出二次型 f(x,y) = x^2 + 2xy + y^2 的矩阵。",
            "2. 用正交变换化二次型 x^2 + 4xy + y^2 为标准形。",
        ]
    )

    cover = PageWriter(doc)
    cover.space(120)
    cover.line(title, size=30, gap=16)
    cover.line("习题答案与详细解析", size=18, gap=12)
    cover.space(190)
    cover.line("文链家两册套装验收样张 · 合成内容", size=11)
    cover.finish()

    toc = PageWriter(doc)
    toc.line("目  录", size=18, gap=14)
    toc.line(f"{chapter1} ………………………………… 3", size=11)
    toc.line(f"{chapter2} ………………………………… 4", size=11)
    toc.finish()

    page1 = PageWriter(doc, page_label="3")
    page1.line(chapter1, size=HEADING1, gap=12)
    page1.line("本章给出基本定义与主要结论，例题从略。" if upper else "本章讨论特征值与特征向量的计算与性质。", size=BODY)
    page1.space()
    page1.line(section1, size=HEADING2, gap=8)
    page1.line("定义 1.1  本节的核心概念由下列条件给出。" if upper else "定义 1.1  若 Ax = λx 且 x ≠ 0，则称 λ 为特征值。", size=BODY)
    page1.space()
    page1.line("习题 1.1", size=HEADING2, gap=8)
    for line in exercises1:
        page1.line(line, size=BODY)
    page1.finish()

    page2 = PageWriter(doc, page_label="4")
    page2.line(chapter2, size=HEADING1, gap=12)
    page2.line("本章讨论后续计算与应用。" if upper else "本章讨论二次型的矩阵表示与标准形。", size=BODY)
    page2.space()
    page2.line(section2, size=HEADING2, gap=8)
    page2.line("定义 2.1  本节概念按教材定义给出。" if upper else "定义 2.1  二次型可用对称矩阵表示。", size=BODY)
    page2.space()
    page2.line("习题 2.1", size=HEADING2, gap=8)
    for line in exercises2:
        page2.line(line, size=BODY)
    page2.finish()

    doc.set_toc(
        [
            [1, chapter1, 3],
            [2, section1, 3],
            [2, "习题 1.1", 3],
            [1, chapter2, 4],
            [2, section2, 4],
            [2, "习题 2.1", 4],
        ]
    )
    doc.set_metadata(
        {
            "title": f"{title} 习题答案与详细解析（测试样本）",
            "author": "WenLianJia Test Fixture",
            "subject": "线性代数",
        }
    )
    try:
        doc.subset_fonts()
    except Exception:
        pass
    doc.save(path, deflate=True)
    doc.close()
    return path


def main() -> int:
    for volume, name in (("上册", "sample_linear_algebra_upper.pdf"), ("下册", "sample_linear_algebra_lower.pdf")):
        target = build_volume(ROOT / name, volume)
        print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
