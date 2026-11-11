# 文链家 WenLianJia

Windows desktop production tool for turning a textbook PDF into a reviewed,
layout-finished exercise-solution product.

For the project purpose, repository organization, architecture, data model,
verified state and known boundaries, read
[`PROJECT-OVERVIEW.md`](PROJECT-OVERVIEW.md).

This repository is a complete local implementation: PDF inspection and OCR,
chapter/exercise detection, per-question AI solving, independent review,
XeLaTeX PDF generation, watermarking, seven e-commerce images, delivery records,
Baidu Netdisk upload/share plumbing, and a two-phase cleanup guard.  A catalog
layer adds textbook metadata, volumes, single-volume and bundle sales SKUs,
integer-cent pricing, per-SKU images/manifests, and optional DeepSeek
Search/Fetch lookup of verified bibliographic data and covers.

> **Honest status (v1.1.0):** the local pipeline, two-volume catalogue demo and
> a real OpenAI acceptance run are implemented and verified.  OpenAI `gpt-4.1`
> solved and independently reviewed an 8-exercise synthetic book (24 calls,
> 7 verified, 1 correctly sent to human review).  DeepSeek Search/Fetch and Baidu
> Netdisk calls remain **not verified** because this machine has no owner
> credentials for them.  The app never fabricates a successful model call,
> search result, cover, upload, share link, or cloud hash.

## What is verified

- Synthetic 5-page Chinese textbook: 2 chapters, 8 exercises, 0 low-confidence
  detections, 0 warnings.
- Deterministic end-to-end run: 8/8 answers reviewed, full PDF 12 pages, quick
  PDF 2 pages, 7 product images, delivery manifest with nine SHA-256 entries.
- Two-volume acceptance run: separate upper/lower productions (4/4 reviewed
  each), Book `LA001`, single SKUs at ¥3.99, bundle at ¥5.00/¥7.98, seven PNGs
  per SKU, bundle manifest with two PDF references and no duplicate AI/PDF work.
- 42 pytest tests pass (`python -m pytest -q`), including the chapter-26
  product-library and offscreen product-center checks.
- Real OpenAI acceptance: `gpt-4.1` solved 8 synthetic exercises and an
  independent reviewer passed 7, sent 1 to `NEEDS_REVIEW`; 24 calls, 24,477
  input / 11,616 output tokens, cost correctly recorded as unknown without
  configured prices.  See `samples/真实AI测试样张/`.
- Chapter-26 acceptance run: 100 books / 300 SKUs, pagination, batch price
  changes, bundle reuse, version switching, shared SHA-256 assets,
  backup/restore and CSV/XLSX export.
- Real RapidOCR pass on a scanned textbook page (32 lines, no invented text).
- Real visible Windows GUI launch, seven pages, catalogue dialog, product
  center, DPAPI secret storage, settings save, and a clean shutdown.
- XeLaTeX/ctex compilation and real Chinese/Greek/math typography.

## Not verified without owner credentials

- DeepSeek Search/Fetch calls, returned metadata, and real cover downloads.
- Baidu OAuth, upload, remote listing, download hash, and share creation.
- Paid Baidu “文件分享服务” permission.  The share API is a paid interface and
  may return a permission error; the app surfaces that state instead of
  pretending a link exists.
- OpenAI cost accounting: the real run used no configured model prices, so cost
  was recorded as unknown rather than invented.

See `docs/KNOWN_ISSUES.md` for the exact boundary.

## Quick start

Requirements:

- Windows 10/11 x64.
- Python 3.11–3.13 (tested with 3.13.13).
- TeX Live with `xelatex`, `ctex`, `xeCJK`, `amsmath`, `fancyhdr`,
  `needspace`, `hyperref` (tested with TeX Live 2026).
- Microsoft YaHei / SimSun / SimHei fonts (standard on Chinese Windows).
- Network access for OpenAI and Baidu, when those stages are enabled.

```powershell
# from the repository root
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1
```

`install.ps1` creates `.venv`, installs `requirements.txt`, initializes the
SQLite database, and runs the environment doctor.  Manual equivalent:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe .\scripts\init_db.py
.\.venv\Scripts\python.exe .\main.py
```

Data is written under `WenLianJia_Data/` next to the source by default.  Set
`WLJ_DATA_DIR` to use another disk/folder.  The database, settings, encrypted
secrets, logs, and all product packages live there; the source tree is not
mutated by a production run.

## First-run configuration

Open **设置**:

1. **模型与费用** — OpenAI API key, base URL (blank uses the official API),
   main/review models, concurrency, token limits, and per-million-token prices.
   Prices are user-supplied.  If a price is missing, the UI records tokens with
   `cost_known=false`; it never invents a price.
2. **检索与书目** — separate DeepSeek API key for Search/Fetch, enable switch,
   per-1,000-request prices, result count, cache days, and cover policy.  The
   DeepSeek key is never sent to OpenAI.  Search/Fetch are optional; without a
   key the app reports “未配置” and requires manual metadata.
3. **排版与水印** — XeLaTeX path (auto-detected if blank), fonts, watermark text,
   angle, opacity, size, and color.  Default watermark: `全网同名爱做题的大肥鱼`,
   35°, 18% opacity, `#9AA0A6`.
4. **百度网盘** — AppKey, AppSecret, app name, OAuth redirect URI (`oob` works),
   effective root, automatic upload/share switches, and optional download-hash
   verification.  Use **生成授权链接**, open it in a browser, paste the returned
   code, and exchange it.  Tokens are stored DPAPI-encrypted.
5. **数据与日志** — data directory information and log level.

Keys are stored with Windows DPAPI (`CryptProtectData`) in
`03_系统配置/secrets.bin`.  They are never written to `settings.json` or logs.
Log redaction covers `sk-...` keys, bearer tokens, and common token fields.

## Production workflow

1. **新建商品** — choose a PDF, inspect it (pages, text layer, TOC, scanned
   likelihood), fill SKU/title/subject/source metadata, and start.
2. **生产监控** — stage-by-stage progress, pause/resume/cancel, log tail.
   A job checkpoint is written after every stage; restarting resumes without
   redoing completed work.
3. **习题复核** — inspect the detected question text, AI draft, verification
   evidence, edit any field, save (forces `NEEDS_REVIEW`), manually approve, or
   re-solve/re-verify one question.
4. **商品管理** — inspect produced files, open the product folder, continue
   cloud upload/share, and view the local delivery record.  The **书目与销售
   SKU** dialog manages the three layers:
   - **教材实体** — bibliographic data, ISBN, edition, verified cover source.
   - **生产实体** — one parsed volume with its own exercise database and PDFs.
   - **销售实体** — a single-volume SKU or a bundle that references existing
     production PDFs; bundles do not re-run AI or regenerate PDFs.
   Create one SKU per volume first (required as the price baseline), then create
   the bundle.  Prices are stored as integer cents and edited as yuan with two
   decimals.  A bundle above the sum of its parts is rejected unless explicitly
   allowed.  Generate seven SKU-specific images and a delivery manifest from
   this dialog.
5. **商品中心** — the large-catalogue workspace: 11 tabs for overview,
   catalogue search, textbooks/productions, SKUs and prices, series templates,
   categories/tags, assets, versions, Netdisk delivery, batch operations and
   audit statistics.  See `docs/PRODUCT_LIBRARY.md`.
6. **设置** — credentials, models, prices, typesetting, watermark, Netdisk and
   DeepSeek search.

Pipeline stages: `parse → chapters → exercises → solve → verify → pdf → images
→ records → upload → share → cleanup`.

Statuses: `PENDING / SOLVING / AI_DRAFT / VERIFIED / NEEDS_REVIEW / FAILED /
APPROVED`.  `VERIFIED` requires an independent reviewer to reach `pass` with no
hard structural failure and no high/blocker issue.  Model confidence alone is
never treated as correctness.

## Output package

Each product gets eight directories:

```
00_原始教材/
01_教材解析/          document.json, pages.jsonl, lines.jsonl, page screenshots
02_习题数据库/        chapters.json, exercises.json, chapter_counts.json
03_AI答案与详解/      solutions.json
04_最终PDF/           full + quick-answer PDFs, build logs/aux
05_商品图片/          7 PNGs + 商品图片清单.json
06_商品信息/          商品信息.json, 商品介绍.md
07_交付记录/          文件清单.json, 商品交付信息.txt, 验收记录.json, 生产报告.json
```

The full PDF has an A4 cover, TOC, running headers, per-question type/status,
question images, knowledge points, derivation, final answer, summary, and
pitfalls.  The quick edition is a compact answer table.

Beyond per-product packages, the catalogue root is:

```
02_已完成/
  00_书目/{book_code}/          book_metadata.json, verified cover
  01_销售商品/{sku_code}/       01_商品图片/, 02_商品信息/, 03_交付记录/
  02_资产库/{sha[:2]}/           SHA-256 deduplicated shared files
04_日志与记录/
  备份/                          database + metadata backups and manifests
  报告/批量操作/                 batch job reports
```

A SKU delivery manifest references the existing production PDFs; it does not
copy them.  `商品介绍.md` and `验收记录.json` are generated per SKU, and the
bucket price/discount is computed from integer cents.

## 商品中心与大型商品库

The default product family is **闲鱼教辅** (`xianyu-jiaofu`).  Every new book
inherits its brand, image/detail/intro/PDF templates, watermark (18% opacity),
pricing rules, delivery rules and Netdisk directory policy; a book may override
any leaf without changing the family default.  Template edits apply only to new
books unless the operator explicitly chooses **应用到现有教材**.

The catalogue is deliberately four levels:

```
闲鱼教辅 (product_families)
└── 线性代数 (books)
    ├── 上册 (products / book_versions)
    │   └── LA001-U (skus)
    ├── 下册 (products / book_versions)
    │   └── LA001-L (skus)
    └── 上下册套装 (skus, references the two existing products)
        └── LA001-B
```

- **Storage** — SQLite first, with SQLAlchemy 2.0 ORM tables in the same
  `wenlianjia.db` as the legacy pipeline, a versioned `library_migrations`
  ledger, indexes for title/ISBN/family/category/lifecycle/production/delivery/
  price, and page-size-capped `LIMIT/OFFSET` queries.  The engine URL is
  isolated so a PostgreSQL move does not require rewriting catalogue logic.
- **Lifecycle** — every SKU has an independent `DRAFT → PENDING_PRODUCTION →
  PRODUCING → REVIEW_REQUIRED → READY → PUBLISHED` path plus `PAUSED`,
  `ARCHIVED` and `FAILED`; production status stays on `Product`.
- **Batch** — CSV/XLSX import/export, price/category/tag/version updates,
  enable/disable, intro/image generation, Netdisk upload and share checks.
  Every job is preview → run → report with per-row results and failure retry.
- **Assets** — files are deduplicated by SHA-256 in
  `02_已完成/02_资产库/`; one asset can be referenced by many books/SKUs, and
  deletion is refused while references remain.
- **Versions** — each book keeps `v1.0`, `v1.1`, … with ACTIVE/ARCHIVED state;
  a SKU selects the version it sells, and switching never overwrites delivered
  files.
- **Backup** — timestamped database + metadata backups under
  `04_日志与记录/备份/`, with manifest hashes, integrity checks, safety copies
  and a tested restore path. Startup creates at most one automatic backup per
  24 hours.
- **Acceptance** — `scripts/acceptance_library.py` builds 100 books / 300 SKUs
  and verifies search, pagination, batch pricing, SKU uniqueness, bundle reuse,
  version switching, shared assets, backup/restore and export.



- The official upload API only accepts paths under `/apps/{appname}`.  The app
  therefore uses `/apps/{appname}/爱做题的大肥鱼_商品库` even if a different root is
  typed.
- Listing returns a cloud hash, not the source file MD5.  A size check is not a
  content proof.  Enable **下载后校验 SHA-256** to download the remote file and
  verify it; cleanup eligibility requires this proof.
- Share creation is a paid “文件分享服务” capability.  Without that permission,
  the app records `needs_manual` and no link.
- A share link is created as unverified.  A human must open it and confirm; only
  `record_manual_verification` — exposed through the UI flow — marks it verified.

## Tests

```powershell
$env:PYTHONUTF8=1
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe .\scripts\acceptance_library.py
.\.venv\Scripts\python.exe .\tests\pipeline_smoke.py
.\.venv\Scripts\python.exe .\tests\two_volume_smoke.py
```

The smoke tests use deterministic `FixtureSolver` / `FixtureVerifier` and label
their output “流程测试样张…不代表真实 AI 结果”.  They are pipeline/catalogue tests,
not a claim of model quality.  `two_volume_smoke.py` is the section 25
acceptance case and writes a real production record under
`.tmp_two_volume_test/`.

## Repository map

```
main.py                 GUI entry
core/                   paths, config, DPAPI, DB, PDF/OCR, detectors, AI,
                        quality, verifier, LaTeX, watermark, images, records,
                        Netdisk, cleanup, pipeline, catalog, book lookup,
                        DeepSeek tools, SKU images
core/library/           product family, templates, lifecycle, assets, service,
                        batch, backup, seed, migrations
models/schemas.py       Pydantic records and enums
gui/                    PySide6 theme, widgets, workers, seven pages,
                        catalogue dialog, product center
prompts/                versioned prompt templates
templates/latex/        ctexbook full/quick templates
scripts/                install, start, doctor, DB init, release, OCR smoke
tests/                  pytest suite and deterministic pipeline smoke
samples/                deterministic, two-volume and real-OpenAI samples,
                        each clearly labelled
docs/                   architecture, configuration, auth, test report,
                        known issues, delivery checklist, release notes
```

## Security and privacy

- Do not commit `.env`, `WenLianJia_Data/`, `secrets.bin`, or generated
  student/teacher material.
- Textbooks, page screenshots, questions, and solutions are sent to the
  configured model provider when solving/verification runs.  Confirm you have
  the right to process and redistribute the source.
- The product is an exercise-solution study aid, not a scan of the textbook.
  Do not include the publisher’s cover or copyrighted pages as your own product
  images.  The original cover is used only to identify the edition; when its
  commercial-use permission is not verified, the app uses a clearly labelled
  self-designed cover.  The seven generated images deliberately use the actual
  PDF pages and real counts, not a copied cover.
- Keep the source PDF; cleanup refuses to remove the only copy.

## License / ownership

Commissioned private product.  No open-source license is granted.  Third-party
libraries retain their own licenses.
