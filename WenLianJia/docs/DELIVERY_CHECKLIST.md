# Delivery checklist

Use this before publishing anything.

## Local product

- [ ] Source PDF opened and inspected: pages, text layer, TOC, scanned pages.
- [ ] Chapter count and per-chapter exercise counts match a manual spot check.
- [ ] Every detected question has original text and, where applicable, a page
      image; no placeholder or guessed formula.
- [ ] Low-confidence or `UNKNOWN` type items reviewed in 习题复核.
- [ ] AI answers have analysis, knowledge points, approach, derivation, final
      answer, summary, and pitfalls.
- [ ] Independent review is `VERIFIED` or a human has approved; no `AI_DRAFT`
      remains in the final PDF.
- [ ] Full PDF opens, has cover/TOC/headers, correct Chinese, formulas, images,
      page numbers, and watermark `全网同名爱做题的大肥鱼` at 35°/18%.
- [ ] Quick-answer PDF opens and its answers match the full edition.
- [ ] Seven product images are 1080×1440, readable, and use real counts/pages.
- [ ] No fake textbook cover or invented chapter/question count appears in an
      image.
- [ ] `06_商品信息` and `07_交付记录` exist; all nine delivered files have
      SHA-256 and size in `文件清单.json`.

## Catalogue / SKU

- [ ] Book identity is checked against the actual edition: title, author,
      publisher, year, edition, ISBN-10/ISBN-13.
- [ ] Cover is either the verified original with source URL/license recorded, or
      the clearly labelled self-designed visual.  Never an unverified lookalike.
- [ ] Upper/lower volumes are separate Book productions with their own exercise
      counts and PDFs.
- [ ] A single-volume SKU exists for each part before the bundle; bundle price is
      not above the sum of part original prices unless explicitly allowed.
- [ ] Money displays two decimals and the stored value is integer cents; no
      floating-point rounding drift.
- [ ] Seven SKU images show the correct volume(s), price, discount, and real
      content.  A bundle image shows both volumes, not only one.
- [ ] SKU `文件清单.json` references existing production PDFs and does not copy
      or regenerate them; `商品介绍.md` matches the SKU.
- [ ] Unverified cover or ambiguous edition is `NEEDS_REVIEW` and not published
      as a verified original.

## Cloud

- [ ] Correct Baidu account observed (`uinfo`) before upload.
- [ ] Effective root is `/apps/{appname}/爱做题的大肥鱼_商品库/...`.
- [ ] Every delivery file uploaded; remote size checked.
- [ ] If content proof is required, download+SHA-256 verification is on and
      matches every file.
- [ ] Share link created by the paid API, or the share step is explicitly
      marked `needs_manual` with no link claimed.
- [ ] A human opens the link in a signed-out/incognito browser and confirms that
      the buyer can see the files.  Only then mark it verified.

## Acceptance / cleanup

- [ ] `验收记录.json` overall is `passed` (not `pending`/`failed`).
- [ ] Upload records are `verified`; share is `verified`.
- [ ] Cleanup `evaluate` has an empty blocker list.
- [ ] Staging manifest lists only intermediate files; final PDFs, images, info,
      delivery records, and the only source remain.
- [ ] Purge uses the exact confirmation `确认删除` and was reviewed by the owner.
- [ ] No real API key, token, textbook, or customer data is in Git/release.

## Release

- [ ] `python -m pytest -q` passes.
- [ ] `tests/pipeline_smoke.py` completes with 0 errors/warnings.
- [ ] `scripts/doctor.py` reports required tools.
- [ ] Sample files are labelled as fixture output, not real AI/product.
- [ ] `docs/TEST_REPORT.md` and `docs/KNOWN_ISSUES.md` match observed reality.
- [ ] Release zip contains source, docs, scripts, prompts, tests, and samples,
      but no `.venv`, data root, secrets, logs, or temporary screenshots.
