"""Build a small, synthetic Chinese textbook PDF used by parser tests.

The document is generated locally (no third-party textbook content) and has a
real text layer, an outline, two chapters, section headings and numbered
exercises.  It is intentionally small so detection regressions are easy to
reason about.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf as fitz

BODY = 10.5
SMALL = 9
HEADING1 = 20
HEADING2 = 14
FONT_CANDIDATES = [
    Path("C:/Windows/Fonts/msyh.ttc"),
    Path("C:/Windows/Fonts/simhei.ttf"),
    Path("C:/Windows/Fonts/simsun.ttc"),
]


def _font_file() -> str:
    for candidate in FONT_CANDIDATES:
        if candidate.exists():
            return str(candidate)
    return ""


class PageWriter:
    def __init__(self, doc: fitz.Document, title: str = "", page_label: str = ""):
        self.doc = doc
        self.page = doc.new_page()
        self.y = 72.0
        self.title = title
        self.page_label = page_label
        self.font_name = "china-s"
        font_file = _font_file()
        if font_file:
            try:
                self.page.insert_font(fontname="wlbody", fontfile=font_file)
                self.font_name = "wlbody"
            except Exception:
                self.font_name = "china-s"

    def line(
        self,
        text: str,
        *,
        size: float = BODY,
        font: str = "",
        gap: float = 6.0,
        indent: float = 0.0,
    ) -> None:
        if self.y > 760:
            raise RuntimeError("page overflow in fixture builder")
        rect = fitz.Rect(72 + indent, self.y, 523, self.y + size * 2.2)
        self.page.insert_textbox(rect, text, fontname=font or self.font_name, fontsize=size)
        self.y += size * 1.55 + gap

    def space(self, amount: float = 8.0) -> None:
        self.y += amount

    def finish(self) -> None:
        if self.page_label:
            self.page.insert_textbox(
                fitz.Rect(280, 790, 320, 815),
                self.page_label,
                fontname=self.font_name,
                fontsize=SMALL,
                align=1,
            )


def build(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()

    cover = PageWriter(doc, page_label="")
    cover.space(120)
    cover.line("线性代数", size=30, gap=16)
    cover.line("习题答案与详细解析", size=18, gap=12)
    cover.space(200)
    cover.line("文链家测试教材 · 合成样本", size=11)
    cover.finish()

    toc_page = PageWriter(doc, page_label="")
    toc_page.line("目  录", size=18, gap=14)
    toc_page.line("第一章  线性空间 ………………………………… 3", size=11)
    toc_page.line("第二章  线性变换 ………………………………… 5", size=11)
    toc_page.finish()

    page1 = PageWriter(doc, page_label="3")
    page1.line("第一章  线性空间", size=HEADING1, gap=12)
    page1.line("本章讨论数域上的线性空间、基与维数，以及线性映射的基本性质。", size=BODY)
    page1.space()
    page1.line("1.1  向量空间", size=HEADING2, gap=8)
    page1.line("定义 1.1  设 V 是一个非空集合，F 是一个数域。", size=BODY)
    page1.line("若 V 上定义了加法与数乘，并满足八条公理，则称 V 为 F 上的线性空间。", size=BODY)
    page1.line("例 1.1  实数域上的 n 元有序数组构成线性空间 R^n。", size=BODY)
    page1.space()
    page1.line("习题 1.1", size=HEADING2, gap=8)
    page1.line("1. 设 V 是数域 F 上的线性空间，证明 V 中零向量唯一。", size=BODY)
    page1.line("2. 判断下列集合是否构成 R 上的线性空间，并说明理由：", size=BODY)
    page1.line("（1）所有满足 x + y + z = 1 的三元实数组；", size=BODY, indent=12)
    page1.line("（2）所有满足 x + 2y = 0 的三元实数组。", size=BODY, indent=12)
    page1.finish()

    page2 = PageWriter(doc, page_label="4")
    page2.line("3. 设 α1, α2, α3 线性无关，证明 α1 + α2, α2 + α3, α3 + α1 也线性无关。", size=BODY)
    page2.line("4. 求向量组 (1,2,1), (2,4,2), (1,1,0) 的秩与一个极大线性无关组。", size=BODY)
    page2.line("5. 设 dim V = n，证明 V 中任意 n + 1 个向量必线性相关。", size=BODY)
    page2.space()
    page2.line("本章小结", size=HEADING2, gap=8)
    page2.line("本章的核心是基与维数。下一章讨论线性变换。", size=BODY)
    page2.finish()

    page3 = PageWriter(doc, page_label="5")
    page3.line("第二章  线性变换", size=HEADING1, gap=12)
    page3.line("2.1  线性变换的定义", size=HEADING2, gap=8)
    page3.line("定义 2.1  若映射 T: V → W 保持加法与数乘，则称 T 为线性映射。", size=BODY)
    page3.space()
    page3.line("习题 2.1", size=HEADING2, gap=8)
    page3.line("1. 设 T: R^3 → R^3 由 T(x,y,z) = (x+y, y+z, z+x) 定义，求 T 的矩阵表示。", size=BODY)
    page3.line("2. 证明：线性变换把零向量映为零向量。", size=BODY)
    page3.line("3. 设 A 为 n 阶方阵，证明 A 可逆当且仅当 det(A) ≠ 0。", size=BODY)
    page3.finish()

    doc.set_toc(
        [
            [1, "第一章  线性空间", 3],
            [2, "1.1  向量空间", 3],
            [2, "习题 1.1", 3],
            [1, "第二章  线性变换", 5],
            [2, "2.1  线性变换的定义", 5],
            [2, "习题 2.1", 5],
        ]
    )
    doc.set_metadata(
        {
            "title": "线性代数 习题答案与详细解析（测试样本）",
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


if __name__ == "__main__":
    target = Path(__file__).with_name("sample_linear_algebra.pdf")
    build(target)
    print(target)
