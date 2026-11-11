# Test report

Date: 2026-11-10 (America/Los_Angeles)
Project: `D:\Workspaces\goods\WenLianJia`
Environment: Windows, Python 3.13.13 virtual environment, PySide6 6.12.0,
PyMuPDF 2.1.0, RapidOCR 3.10.0, TeX Live 2026 xelatex.

## Automated tests

Command:

```powershell
$env:PYTHONUTF8=1
.\.venv\Scripts\python.exe -m pytest -q
```

Result:

```
42 passed, 1 warning in 14.43s
```

The warning is PyMuPDF’s own `fitz` deprecation notice, not a project warning.

Coverage by file:

| File | Cases | What is checked |
| --- | --- | --- |
| `tests/test_pdf_and_detection.py` | 4 | PyMuPDF inspection, text extraction, screenshots, 2 chapters, 8 exercises, per-chapter counts, no swallowed summary |
| `tests/test_ai_and_verification.py` | 4 | AI draft persistence, independent review sequence, blocker → `NEEDS_REVIEW`, cost only with configured prices |
| `tests/test_netdisk.py` | 8 | configuration, effective app root, path guard, OAuth URL, errno classification, share-permission handling, 4 MB blocks, encrypted token |
| `tests/test_delivery_and_cleanup.py` | 4 | nine-file SHA-256 manifest, cloud/share acceptance pending, cleanup refusal, exact purge confirmation |
| `tests/test_catalog_and_lookup.py` | 7 | ISBN/title identity extraction, verified lookup and cache, ambiguous editions → `NEEDS_REVIEW`, DeepSeek request/cost, Decimal bundle pricing, delivery references without copying, SKU upload folder/size verification |
| `tests/test_quality_and_watermark.py` | 7 | placeholders, step count, arithmetic mismatch, Chinese answer not wrapped in math, LaTeX escaping, multiline bullets with display math, watermark on every page |
| `tests/test_library.py` | 7 | product-library family/template inheritance, four-level hierarchy and lifecycle, bundle reuse, asset dedupe/delete protection, batch preview/run/retry/export, backup/restore, 100-book/300-SKU seed pagination |
| `tests/test_product_center_gui.py` | 1 | product-center page instantiates with the shared context, renders 11 tabs, loads seeded rows and resolves book/SKU selection |
| `tests/conftest.py` | — | temporary data root, settings, DB, DPAPI store |

## End-to-end pipeline smoke test

Command:

```powershell
$env:PYTHONUTF8=1
.\.venv\Scripts\python.exe .\tests\pipeline_smoke.py
```

Observed:

- Input: synthetic 5-page Chinese PDF, 2 chapters, 8 exercises.
- Deterministic solver/verifier (explicitly labelled fixture; no API calls).
- 8/8 exercises `verified`; 0 errors, 0 warnings.
- Full PDF: 12 pages, 259,027 bytes.
- Quick PDF: 2 pages, 177,442 bytes.
- Seven 1080×1440 product PNGs.
- Nine delivery files hashed; acceptance remains `pending` for cloud/share
  because no credentials were available.
- Job status `completed`, product status `packaged`.

## Two-volume / bundle SKU acceptance

Command:

```powershell
$env:PYTHONUTF8=1
.\.venv\Scripts\python.exe .\tests\two_volume_smoke.py
```

Observed on the synthetic two-volume fixture (no API calls):

- Book `LA001`, productions `LA001-U` / `LA001-L`, each 4 pages, 2 chapters,
  4 exercises, 4/4 `verified`, both jobs `completed`, both products `packaged`.
- Single SKUs `LA001-U` and `LA001-L` at ¥3.99 each; bundle `LA001-B` at ¥5.00
  against a ¥7.98 component baseline (saves ¥2.98).
- Seven SKU images generated for each of the three SKUs; the bundle manifest
  references the two existing PDFs plus seven images, nine files total, without
  copying production PDFs.
- Per-SKU `商品介绍.md`, `商品信息.json`, `文件清单.json`, `商品交付信息.txt`,
  `验收记录.json`, and book-level `book_metadata.json`.
- Bundle acceptance is `needs_review` only because the synthetic book has no
  verified publisher cover and no cloud upload/share was performed.
- Copy of the case: `samples/分册套装测试样张/`.

## Real OpenAI acceptance

Command:

```powershell
$env:PYTHONUTF8=1
.\.venv\Scripts\python.exe .\acceptance\real_ai_v1_run.py
```

Observed with a live `gpt-4.1` key on the synthetic 8-exercise book:

- 24 API calls; 24,477 input tokens; 11,616 output tokens.
- 7 exercises `VERIFIED`, 1 `NEEDS_REVIEW`, 0 failed, 0 pipeline errors.
- Full PDF 13 pages; quick PDF 3 pages; seven product images.
- Cost `0.0` with `cost_known=False`: no model prices were configured, so the
  system did not invent a cost.
- Q4 was sent to human review because the independent reviewer found an
  incorrect matrix/row-operation derivation even though the final answer was
  correct.  This is the review gate working, not a hidden failure.
- Copy of the case: `samples/真实AI测试样张/`.

## Chapter 26 product-library acceptance

Command:

```powershell
$env:PYTHONUTF8=1
.\.venv\Scripts\python.exe .\scripts\acceptance_library.py
```

Observed on a disposable `.tmp_acceptance/` data root:

- Seed: 100 books, 120 products, 110 versions, 300 SKUs (120 singles, 20
  bundles, 160 quick variants), 12 assets, 8 categories, 12 tags.
- Book retrieval: total 100, 5 pages at page size 20, page 1/2 disjoint,
  code search hit 1.
- SKU retrieval: total 300, all codes unique, all 9 lifecycle states present.
- Batch price change: 10 rows, 10 preview-valid, run `COMPLETED`, 10 prices
  updated to ¥8.88.
- Version switch: a two-version book switched from `v1.1` to `v1.0` and the
  binding was reflected on its SKU.
- Bundle reuse: 20 bundles, 2 parts each, 2 production records for the book,
  no extra AI/PDF production.
- Assets: 12 registered, 12 linked, maximum 3 references to one file, delete
  blocked while referenced.
- Backup: manifest and `PRAGMA integrity_check` OK; mutation to ¥77.77 was
  rolled back by restore to the pre-mutation ¥8.88 price; integrity OK.
- Export: 100 book rows (CSV) and 300 SKU rows (XLSX) read back successfully.
- Elapsed 4.46 s; script exit 0.



- Text-layer fixture: 5 pages, 32 lines, 6 TOC entries, page screenshots and JSONL
  written.
- Scanned real page (`统计学习方法（第2版）`, page 36): RapidOCR returned 32 lines
  in 6.74 s including model load; no guessed text on failure.  The smoke script
  `scripts/ocr_smoke.py` accepts a page path and renders it at 200 DPI.
- No scanned book was used to generate or distribute a product; the fixture is
  synthetic.

## GUI verification

- Offscreen Qt (`QT_QPA_PLATFORM=offscreen`, `QT_QPA_FONTDIR=C:\Windows\Fonts`):
  all seven pages instantiate, render, and load fixture data. The product-center
  page exposes 11 tabs and was exercised with 10 books / 30 SKUs (all 30 delivery
  rows, book and SKU selection).
- Visible Windows launch: native window, 1440×900 logical at 200% scaling,
  readable Chinese fonts, professional layout, clean close (exit 0).
- Settings save test: model `gpt-4.1-mini`, price `2/8` USD per million, DPAPI
  secret round-trip, plaintext key absent from `settings.json`, delete works.
- XeLaTeX/ctex: Chinese, Greek (`α`), `≠`, math, cover, TOC, headers, tables, and
  watermark compile without missing-glyph warnings.

## Clean release verification

`release/WenLianJia_v1.1.0.zip` was extracted to a clean folder. No `.venv`,
data directory, secret file, log, cache, or temporary test output was present.
Inside the extraction:

- `python -m pytest -q`: `42 passed, 1 warning` in 13.58 s.
- `scripts/init_db.py` against a fresh data root: all 28 tables created.
- `scripts/doctor.py`: all required local checks pass; OpenAI, DeepSeek and
  Baidu credentials reported as `[INFO] not configured` in the fresh data root,
  not as fake success.
- Zip: 156 entries; it contains the three sample packages, including the
  real-OpenAI sample.  The final hash is recorded in `release/SHA256SUMS.txt`
  beside the archive.

The installer was separately exercised from a source checkout with an existing
uv-created virtual environment: pip was bootstrapped, requirements installed,
the database initialized, and the doctor passed. Credentials were intentionally
absent.

## What this report does not claim

A live OpenAI request was performed for the synthetic acceptance book; no live
DeepSeek Search/Fetch, no real publisher cover download, no real Baidu
OAuth/upload/download/share, and no human review of a real textbook’s
mathematics were performed.  Those require owner credentials, a licensed
source, and a human reviewer.  See `docs/KNOWN_ISSUES.md`.
