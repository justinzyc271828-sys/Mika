# Architecture

## Runtime shape

```
PySide6 GUI (main thread)
  └─ PipelineWorker / ExerciseWorker / ConnectionWorker / OAuthWorker (QThread)
       └─ Pipeline (core/pipeline.py)
            parse → chapters → exercises → solve → verify → pdf → images
            → records → upload → share → cleanup
```

The GUI never performs PDF parsing, model calls, LaTeX compilation, uploads, or
long file operations on the UI thread.  Workers communicate with Qt signals;
the pipeline additionally emits structured progress dictionaries.

## Persistent state

SQLite (WAL) is the source of truth:

| Table | Purpose |
| --- | --- |
| `products` | SKU, title, subject, source hash, status, counts, metadata |
| `jobs` | stage checkpoints, progress, options, resume count, error |
| `pdf_pages` | page text, dimensions, OCR confidence, screenshot path |
| `chapters` | stable chapter ids, title, page range |
| `exercises` | stable per-question ids, text, images, type, confidence, status |
| `solutions` | model, prompt version, JSON content, tokens, cost, status |
| `verifications` | independent review/reconcile verdict and evidence |
| `generated_files` | kind/path/size/SHA-256 |
| `uploads` | remote path, size, status, error, verified flag |
| `shares` | URL, password, period, verified state |
| `api_usage` | per-call token/cost accounting |
| `books` | bibliographic identity, ISBN, cover source/license, verification |
| `skus` | single/bundle sale entities, integer-cent prices, production references |
| `book_cache` | DeepSeek Search/Fetch responses with expiry |
| `logs`, `settings`, `perf_notes` | diagnostics and local state |

Connections are thread-local.  JSON columns are decoded through Pydantic models.
Stage completion is recorded in `jobs.stage_status`; completed stages are reused
on resume, and failed/pending work is retried.

## Filesystem contract

`AppPaths` owns one data root (`WLJ_DATA_DIR`, default
`WenLianJia_Data/`).  Product packages have eight directories.  The source PDF
is copied into `00_原始教材` with its original hash; the original path remains
untouched.  Catalogue data lives under `02_已完成/00_书目` and
`02_已完成/01_销售商品`; a SKU package contains `01_商品图片`, `02_商品信息`, and
`03_交付记录`.

## Parsing

`PdfParser` inspects the text layer, renders page screenshots, extracts embedded
images, and decides whether a page is scanned using text volume and image
coverage.  Scanned pages go through RapidOCR by default; PaddleOCR is an optional
adapter and is not installed in the verified environment.  Unreadable text is
never replaced with guessed content: a page screenshot is retained instead.

`layout_analyzer` derives font-size headings; `ChapterDetector` prefers the PDF
outline, then heading candidates, then a whole-book fallback.
`ExerciseDetector` finds exercise sections and numbered starts, stops at generic
section headings (for example `本章小结`), and records per-chapter counts and
low-confidence items.

## AI solving and independent review

`SolverService` builds a math/science prompt, attaches at most two page images
when the exercise is scanned or figure-dependent, and requests a strict JSON
schema.  It persists `AI_DRAFT` (or `FAILED`) and never marks its own output as
correct.

`AnswerVerifier` first asks the reviewer model to solve independently, without
the candidate answer.  It then asks for a reconcile verdict and structured
findings.  `VERIFIED` requires `pass`, no hard structural failure, and no
high/blocker finding.  A low-similarity warning does not by itself fail an
answer.  Optional escalation to a second model always forces human review.

`quality_control` performs structural checks (answer presence, minimum steps,
placeholder phrases, detail length), pure-arithmetic checks (SymPy is available
for deeper work; the generic path only evaluates unambiguous digit arithmetic),
and text similarity.  It never claims formal proof validity.

## PDF generation

Jinja2 templates produce `ctexbook` source with A4 pages, TOC, running headers,
`amsmath`, `longtable`, `needspace`, and `hyperref`.  `DocumentBuilder` runs
`xelatex` twice with `-interaction=nonstopmode -halt-on-error`, promotes the
result into `04_最终PDF`, and inspects page count, text length, Chinese glyphs,
replacement glyphs, and overfull boxes.  Strict mode refuses to build if any
question lacks a solution or final answer.

Watermarking is a Pillow-rendered transparent PNG (real Chinese font, configured
rotation/opacity/color) inserted by PyMuPDF on every page.  It refuses to
overwrite its input.

## Delivery and cloud

`ProductManager` hashes every delivered PDF/image, writes JSON/Markdown/text
records, and computes acceptance checks.  Cloud checks start as `pending`; only
verified uploads and a verified share become `pass`.

`BaiduNetdisk` implements OAuth authorization-code and refresh flows, DPAPI
token storage, app-root path guarding, precreate/4 MB block upload/create,
list/metadata checks, optional download+SHA-256, and share creation.  Cloud
errors are classified (`needs_auth`, `needs_manual`, retryable) rather than
swallowed.  The official upload scope is `/apps/{appname}`; the client reports
that limitation instead of bypassing it.

`CleanupManager` is two-phase: `evaluate → stage → restore/purge`.  Staging
moves only intermediate directories into a manifest-backed holding area.
Purging requires the exact string `确认删除`, a path inside the staging root, and
the eligibility checks.  The only source copy is never deleted by default.

## Catalogue, pricing and book lookup

`CatalogManager` owns the three-layer split: `Book` (bibliographic identity),
`Product` (a produced volume), and `SalesSKU` (a sale entity).  A bundle stores
only `SkuItem` references; `build_delivery` resolves existing production PDFs
from the database or `04_最终PDF` and never copies or regenerates them.  Money is
stored as integer cents; input yuan is parsed with `Decimal` and rounded
half-up to two decimals.  A bundle baseline is the sum of its parts'
`original_price_cents` (falling back to `price_cents`); exceeding it requires
the explicit `allow_above_parts` flag.

`SkuImageGenerator` renders seven 1080×1440 PNGs from real book/SKU/production
data and actual PDF pages.  A verified cover is shown only when provided; the
otherwise self-designed visual is explicitly labelled as not the publisher's
cover.

`BookLookupService` extracts identity hints from a PDF (metadata, ISBN regex,
publisher/edition/year/author heuristics), then queries DeepSeek Search/Fetch,
ISBN/title candidates, and a local cache.  It downloads a cover only after
content-type/size/dimension checks and records source URL/license.  It selects a
candidate only when there is exactly one strong ISBN/title match; ambiguity or a
missing source yields `NEEDS_REVIEW`, never a guessed cover.

`CatalogManager.upload_sku` uploads the SKU’s PDFs, seven images and the
`02_商品信息` files into `01_习题详解`, `02_商品图片`, and `03_商品信息`, then
re-reads the upload records.  `verify_sku_uploads` checks each remote size and
optionally downloads and compares SHA-256; only then are records marked
`verified`.  The GUI exposes this as “上传 SKU 到网盘”; no live account was
available for the acceptance run.

## Security boundary

- DPAPI (`CryptProtectData`/`CryptUnprotectData`) protects API keys and OAuth
  tokens; `settings.json` contains no secrets.
- Logging redacts `sk-`, `Bearer`, access/refresh tokens, and common secret keys.
- The Baidu client never logs a token or full URL containing one.
- No credential is bundled in the repository or release zip.
