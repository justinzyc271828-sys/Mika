# 习题识别（视觉兜底）

当 PDF 文本层缺失、OCR 置信度低，或版式分析无法判断某一段是否为课后习题时，使用本提示词让多模态模型只做“识别与转录”，不做解答。

## 输入

- 教材页面截图（可能包含正文、例题、习题、页眉页脚）。
- 已知的章节标题与候选页码。

## 任务

1. 判断页面中是否存在课后习题区（习题、练习、思考题、复习题、Problems、Exercises 等）。
2. 只转录确实属于习题的题目，保持原编号、顺序与分页。
3. 数学公式输出 LaTeX；无法辨认的符号用 `[?]` 标记，禁止猜测补全。
4. 图表只描述其存在与图号，不虚构图形内容。
5. 区分正文、例题和习题；例题不得混入课后习题。
6. 返回严格 JSON，不要 Markdown 代码围栏。

## 输出 JSON

```json
{
  "has_exercises": true,
  "section_title": "习题 1.1",
  "questions": [
    {
      "number": "1",
      "text": "题目原文，公式使用 $...$",
      "page": 12,
      "exercise_type": "calculation|proof|choice|fill_blank|short_answer|programming|experiment|unknown",
      "unreadable": false
    }
  ],
  "warnings": []
}
```

`page` 使用输入中标注的原始 PDF 页码。`unreadable=true` 表示存在无法可靠转录的公式或图形，必须保留原页截图供人工复核。
