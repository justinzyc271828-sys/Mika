# 商品中心与大型商品库

Status: implemented and locally verified on 2026-11-10. This document covers
chapter 26 of the WenLianJia specification: the Xianyu teaching-aid product
family, the four-level catalogue, the 10,000-SKU-oriented data model, the
product-center UI, batch operations, lifecycle, shared assets, versions,
template inheritance and backup/restore.

## 1. Product family template

`product_families` stores reusable series templates. The default row is
`fam_* / xianyu-jiaofu / 闲鱼教辅` and is created lazily by
`LibraryService.ensure_default_family()`. A template contains:

- brand name (`全网同名爱做题的大肥鱼`);
- visual palette, image template (1200×1600, 7 gallery images) and detail-page
  sections;
- product introduction template, PDF template;
- watermark settings (text, 42 pt, `#9AA0A6`, 18% opacity, 35°, centered);
- default pricing (minimum 100 cents, round to 100 cents, bundle discount 0.9);
- default category, delivery rules and Baidu Netdisk directory rules.

`resolve_template(family, book.template_overrides)` deep-merges the family
payload with per-book overrides and records the source of each leaf
(`family` or `override`). Overrides are validated before saving: opacity must be
0–1, watermark size 8–200, rotation −90–90, gallery count 1–20, bundle discount
0–1, and prices non-negative. Template edits affect only future books unless the
operator explicitly runs **应用到现有教材** (`apply_family_template`), which
supports a dry run and never silently rewrites existing rows.

## 2. Four-level catalogue

| Level | Table / model | Meaning |
| --- | --- | --- |
| 1 | `product_families` / `ProductFamily` | Reusable series template |
| 2 | `books` / `Book` | Textbook master product (bibliographic identity) |
| 3 | `products` / `Product` + `book_versions` | Produced volume / content sub-product and its version |
| 4 | `skus` / `SalesSKU` | Actual sale SKU (single volume or bundle) |

`Book` carries `family_id`, `category_id`, `template_overrides` and `status`
(`ACTIVE` / `ARCHIVED`). `SalesSKU` carries `family_id`, `category_id`,
`version_id`, `lifecycle`, `delivery_status`, `primary_product_id`,
`production_status` and the published/paused/archived timestamps. A bundle
stores `items` that reference existing `products`; it never creates a second
production record or calls the AI/PDF pipeline again.

## 3. Database architecture

- SQLite is the first-version store. The legacy pipeline keeps its stdlib
  `sqlite3` access; the product-library domain uses SQLAlchemy 2.0 ORM in the
  same database file (`03_系统配置/wenlianjia.db`).
- `core/library/migrations.py` owns a `library_migrations` ledger
  (`SCHEMA_VERSION = 1`). `ensure_schema(engine)` adds missing legacy columns,
  creates the library tables/indexes, and records the applied version. The
  legacy `Database._migrate()` performs the equivalent additive migration and a
  one-time lifecycle backfill guarded by the `library_lifecycle_backfill`
  setting.
- Indexes cover the common 10,000-SKU queries: `idx_books_title`,
  `idx_books_isbn`, `idx_books_family`, `idx_books_category`,
  `idx_skus_book`, `idx_skus_lifecycle`, `idx_skus_production`,
  `idx_skus_delivery`, `idx_skus_price`, plus the SHA-256 and status indexes on
  the legacy `products` table.
- Every list API uses `LIMIT/OFFSET` with a hard page-size cap of 500 and a
  total count, so the UI never scans a directory or loads the full catalogue.
- The database URL is built in one place (`create_library_engine`). Moving to
  PostgreSQL means changing that URL/driver and reviewing SQLite-specific SQL;
  no catalogue logic reads the filesystem to determine product state.

## 4. Product center

`gui/pages/product_center.py` is a separate page in the main navigation
(**商品中心**, seventh page). It has 11 tabs:

1. 总览 — counts, lifecycle/delivery distribution, price totals, seed action.
2. 商品目录 — cross-book search by title/code/ISBN/author/publisher/subject.
3. 教材与子商品 — books and their production volumes.
4. SKU 与价格 — single/bundle SKU table, lifecycle and price actions.
5. 系列模板 — family list, default selection, explicit apply.
6. 分类与标签 — category tree and tags.
7. 资产与文件 — SHA-256 asset registry, add/verify/delete.
8. 版本管理 — per-book version history and activation.
9. 网盘交付 — delivery build/upload/share state per SKU.
10. 批量操作 — preview/run/retry/report and CSV/XLSX export.
11. 统计与审计 — category statistics and the audit ledger.

All writes go through `LibraryService`; the page does not manipulate SQL
directly. Every create/update/delete/lifecycle/batch/backup action appends an
`audit_log` row.

## 5. SKU lifecycle

Selling state is independent of production state (`Product.status`). The nine
states and allowed transitions are:

```
DRAFT              -> PENDING_PRODUCTION, ARCHIVED
PENDING_PRODUCTION -> PRODUCING, FAILED, ARCHIVED
PRODUCING          -> REVIEW_REQUIRED, FAILED, ARCHIVED
REVIEW_REQUIRED    -> READY, PRODUCING, FAILED, ARCHIVED
READY              -> PUBLISHED, PAUSED, ARCHIVED
PUBLISHED          -> PAUSED, ARCHIVED
PAUSED             -> PUBLISHED, ARCHIVED
ARCHIVED           -> DRAFT
FAILED             -> PENDING_PRODUCTION, ARCHIVED
```

`transition_lifecycle` validates the edge, stamps `published_at`, `paused_at`
or `archived_at`, and writes an audit entry. Cloud delivery is tracked
separately with `DeliveryStatus` (`NOT_DELIVERED`, `PENDING`, `UPLOADED`,
`SHARE_CREATED`, `VERIFIED`, `NEEDS_MANUAL`, `FAILED`).

## 6. Batch operations

`core/library/batch.py` supports CSV (UTF-8 with BOM) and XLSX input/output via
openpyxl. Kinds:

- `import_books`, `import_skus`
- `update_price`, `update_category`, `add_tags`, `enable_skus`,
  `disable_skus`, `update_version`
- `generate_intro`, `generate_images`, `upload_netdisk`, `check_share`
- exports `export_books`, `export_skus`

The flow is always **preview → run → report**, with a per-row result stored in
`batch_job_items`. Preview validates required fields and entity existence and
returns `valid`/`message` per row; invalid rows are never executed. A run marks
the job `COMPLETED`, `PARTIAL` or `FAILED`; `retry(job_id)` resets failed rows
and re-runs them. Reports can be exported as CSV/XLSX, and every job is
auditable.

## 7. Assets

`core/library/assets.py` registers files by streaming SHA-256. The first
registration copies the file into `02_已完成/02_资产库/<first-two-hex>/`; later
registrations of the same bytes return the existing row and do not duplicate
storage. `asset_links` connects one asset to any number of owners
(`book`, `sku`, ...) with a role (`cover`, `gallery`, `attachment`).

- `delete_asset` requires confirmation and refuses while references exist.
- Deleting a SKU unlinks its references but never deletes a file that another
  owner still uses.
- `verify` reports missing files and hash mismatches; `usage_summary` reports
  total bytes, linked and orphan counts.

## 8. Versions

`book_versions` stores `version_label` (for example `v1.0`, `v1.1`, `v2.0`),
status (`DRAFT/ACTIVE/ARCHIVED/FAILED`), notes, the related production id and
timestamps. `activate_version` archives the previous active version;
`set_sku_version` changes which version a SKU sells. Deleting a version that a
SKU still references is refused. Existing delivered files are never overwritten
by a version switch.

## 9. Backup and recovery

`core/library/backup.py`:

- `create_backup(label)` checkpoints WAL, closes the tracked connections, copies
  the database with SQLite's backup API, writes a manifest with SHA-256, size,
  schema version and row counts, and exports metadata JSON.
- `verify_backup(path)` runs `PRAGMA integrity_check`, checks the expected
  tables/counts and the manifest hash.
- `restore_backup(path, confirm=True)` first writes a
  `wenlianjia-pre-restore-*.db` safety copy, restores via the backup API, then
  re-runs both migration layers.
- `integrity_check()` checks database integrity, asset files/hashes, orphan
  SKUs, books without SKUs and duplicate asset hashes.
- `create_auto_backup()` runs at startup and creates at most one automatic
  backup per 24 hours; a recent manual backup suppresses it.
- Backups live in `04_日志与记录/备份/`; batch reports in
  `04_日志与记录/报告/批量操作/`.

## 10. Acceptance evidence

`scripts/acceptance_library.py` creates a disposable data root and runs the
chapter-26 acceptance case. Latest observed run (2026-11-10):

```
seed: 100 books, 120 products, 110 versions, 300 SKUs
      (120 singles, 20 bundles, 160 quick variants), 12 assets, 8 categories, 12 tags
books: total 100, pages 5 at page_size 20, page 1/2 disjoint, search hits 1
skus: total 300, unique codes true, 9 lifecycle states present
batch_price: 10 rows, preview 10 valid, run COMPLETED, 10 prices changed
version_switch: two-version book, v1.1/v1.0, SKU version changed
bundle_reuse: 20 bundles, 2 parts each, 2 products for the book, no new production
assets: 12 total, 12 linked, max refs 3, shared delete blocked
backup: verify ok, integrity ok, restore returned the pre-mutation price
export: books 100 rows (CSV), skus 300 rows (XLSX)
elapsed: 4.46 s
ok: true
```

The data root is `.tmp_acceptance/`; the generated evidence includes
`export_books.csv`, `export_skus.xlsx` and the backup manifest. The automated
suite additionally covers 100/300 pagination in `tests/test_library.py` and the
offscreen product-center page in `tests/test_product_center_gui.py`.

## 11. Boundaries

- Live OpenAI, DeepSeek and Baidu Netdisk calls remain unverified without owner
  credentials. Batch `upload_netdisk` / `check_share` surface the unconfigured
  state instead of inventing a link.
- The GUI acceptance was offscreen; a visible-window pass of the new page is
  still a manual check.
- PostgreSQL has not been exercised; only the SQLite path is verified.
- The acceptance data is synthetic and is not a marketplace-ready product.
