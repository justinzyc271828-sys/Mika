# Release notes — v1.1.0

Date: 2026-11-10

Second local release of 文链家 WenLianJia: the v1.0.0 production pipeline plus a
three-layer textbook catalogue, volume/bundle SKUs, integer-cent pricing,
per-SKU images/manifests, and optional DeepSeek Search/Fetch book metadata.

## Included

- PySide6 desktop application with seven pages:
  - 工作台 (dashboard)
  - 新建商品
  - 生产监控 (pause/resume/cancel, stage progress, log tail)
  - 习题复核 (edit, save, manual approve, re-solve, re-verify one question)
  - 商品管理 (open package, continue cloud stages, 书目与销售 SKU dialog)
  - 商品中心 (the concurrent chapter-26 product-library page)
  - 设置 (models, prices, DeepSeek search, watermark, LaTeX, Baidu OAuth,
    data/logs)
- SQLite WAL database with products, books, SKUs, jobs, PDF pages, chapters,
  exercises, solutions, verifications, generated files, uploads, shares, API
  usage, DeepSeek cache, logs, settings, and performance notes.
- Three-layer catalogue: `Book` (bibliographic identity), `Product` (one
  produced volume), `SalesSKU` (single or bundle).  A bundle references existing
  production PDFs and does not re-run AI or regenerate PDFs.
- Integer-cent pricing with Decimal input; per-SKU price, original price,
  discount amount/rate, batch price editing, and a bundle baseline check.
- Seven SKU-specific 1080×1440 images generated from real book/SKU/production
  data, actual PDF pages, counts, and prices.  A verified cover is shown only
  when supplied; otherwise the visual is clearly marked as self-designed.
- Per-SKU delivery directory with `商品介绍.md`, `文件清单.json`, delivery text,
  and acceptance record; PDF references are not copied.
- DeepSeek Search/Fetch client with separate key, cache, official endpoints,
  per-1,000-request cost accounting, and candidate ranking.  Ambiguous metadata
  or covers stay `NEEDS_REVIEW`; the app never invents a cover.
- PDF parsing with text layer and RapidOCR fallback, page screenshots, embedded
  images, TOC/heading chapter detection, exercise detection with per-chapter
  counts and stable ids.
- OpenAI Responses API with Chat Completions fallback, strict solution schema,
  independent reviewer-first verification, optional escalation, retry and cost
  accounting from user-supplied prices.
- XeLaTeX A4 full and quick-answer PDFs, cover, TOC, headers, formulas, images,
  page numbers, and diagonal Chinese watermark (default 全网同名爱做题的大肥鱼,
  35°, 18%).
- Product JSON/Markdown, SHA-256 file manifest, delivery text, acceptance record,
  production report.
- Baidu Netdisk OAuth/token refresh, app-root path guard, precreate + 4 MB block
  upload + create, list/size verification, optional download SHA-256, share
  creation with paid-permission handling, manual share verification.
- Two-phase cleanup: evaluate → stage → restore/purge, exact confirmation,
  source preservation.
- DPAPI-encrypted secrets, redacted logs, no plaintext keys in settings or git.
- Install/start/doctor/DB-init/release scripts, pytest suite, deterministic
  pipeline and two-volume smoke tests, sample output package.

## Verified

- 42 pytest tests pass.
- Real OpenAI acceptance with `gpt-4.1`: 8 synthetic exercises, 24 calls,
  24,477 input / 11,616 output tokens, 7 `VERIFIED`, 1 `NEEDS_REVIEW`, 0
  pipeline errors; 13-page full PDF and 3-page quick PDF.  Cost stayed unknown
  because no model prices were configured.
- Fixture pipeline: 2 chapters, 8/8 verified, 12-page full PDF, 2-page quick PDF,
  7 images, 9 hashed delivery files, no errors/warnings.
- Two-volume acceptance run: separate upper/lower productions, 4/4 verified each,
  Book LA001, single SKUs ¥3.99, bundle ¥5.00/¥7.98, 7 images per SKU, bundle
  manifest with two PDF references and no duplicate AI/PDF work.
- Visible Windows GUI launch, catalogue dialog, and clean shutdown.
- DPAPI settings save and secret delete, including a separate DeepSeek key.
- Real scanned-page RapidOCR pass (32 lines).
- Chinese/Greek/math/watermark XeLaTeX output visually inspected.

## Not verified (requires owner)

- Live DeepSeek Search/Fetch, real bibliographic results, and real cover
  downloads.
- Live Baidu upload/share, per-SKU cloud upload, and paid share permission.
- Human mathematical review of a real textbook’s answers.

The live OpenAI run is a synthetic-textbook acceptance test, not a claim that
every answer is mathematically correct.  Q4 is deliberately retained as a
`NEEDS_REVIEW` example.

## Upgrade notes

The database migration adds `book_id`, `volume_label`, `volume_number`,
`volume_code` to products and creates `books`, `skus`, and `book_cache`
idempotently.  Existing `WenLianJia_Data` remains usable.  Back up the data
folder before changing models or running cleanup.  Never delete the only source
PDF.
