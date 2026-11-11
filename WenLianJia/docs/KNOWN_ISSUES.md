# Known issues and boundaries

## External services not verified

1. **OpenAI is verified only on the synthetic acceptance book.** A live
   `gpt-4.1` run completed 24 calls and produced 7 `VERIFIED` + 1
   `NEEDS_REVIEW` answer.  Real textbook OCR, long proofs, schema edge cases,
   rate-limit behavior, and the owner’s actual price table are still unverified.
   Configure model prices before relying on cost figures; the acceptance run
   correctly recorded `cost_known=False`.  The API key was pasted into chat and
   should be rotated after acceptance.
2. **Baidu OAuth/upload/share are unverified.** No AppKey/Secret, app name, or
   paid share permission exists here.  No file was uploaded and no link was
   created.  The client is implemented against the official endpoints but must
   be exercised against the owner’s application.
3. **Paid share service.** If the account has not purchased “文件分享服务”, the
   share API will fail.  The app reports `needs_manual`; it cannot create a link
   legally or reliably in that state.
4. **Cloud hash limitation.** The listing `md5` is a cloud hash, not the source
   file MD5.  Without **下载后校验 SHA-256**, the app can prove size/existence
   only.  Cleanup is blocked until the stricter proof and a verified share exist.
5. **Download hash bandwidth.** Verifying by downloading every deliverable may
   be slow or costly.  It is off by default and can be enabled per settings.
6. **DeepSeek Search/Fetch is unverified.** No `DEEPSEEK_API_KEY` exists here.
   Request paths, retry classification, cache, billing records, and candidate
   parsing are tested only with labelled fake responses.  Real metadata quality,
   QPS limits, and publisher pages must be checked with the owner’s key.
7. **Real cover sourcing is unverified.** The cover downloader validates MIME,
   size, and dimensions and records source/license, but it cannot establish
   commercial-use permission.  Ambiguous editions are left `NEEDS_REVIEW`; a
   self-designed cover is clearly labelled when no verified cover is available.
   Google Books/Open Library are optional and not implemented as automatic
   providers in this build.
8. **Book lookup runs synchronously in the dialog.** A slow or unreachable
   DeepSeek endpoint can freeze the modal until the HTTP timeout.  Keep the
   timeout low or run lookup without other UI work; a worker-based dialog is a
   planned improvement.
9. **Per-SKU cloud upload is implemented but not live-tested.** The catalogue
   dialog now has an “上传 SKU 到网盘” action that creates the book/SKU folders,
   uploads PDFs, seven images and 商品信息, then verifies size and optionally
   downloads for SHA-256. No owner credentials were available to exercise it
   against the real account, and share creation for a SKU still depends on the
   paid share service. Do not report a SKU as cloud-delivered until that flow is
   exercised with the owner’s account.

## Parsing and mathematical quality

10. **Scanned formulas are not reconstructed as LaTeX.** The parser keeps a page
    screenshot and OCR text.  The PDF builder can embed the page image, but it
    does not claim to infer a formula that OCR could not read.  Handwriting,
    two-column layouts, and low-resolution scans can degrade detection.
11. **Exercise boundaries are heuristic.** Numbered lists, examples, and review
    sections are classified by headings and layout.  Ambiguous items are marked
    low-confidence and should be checked in 习题复核.
12. **“VERIFIED” is not a formal proof.** It means an independent model review
    returned `pass` and structural checks passed.  It is not a proof assistant,
    SymPy certification of every step, or a human mathematician’s sign-off.
    Proof-heavy books require human review.
13. **Some questions split across PDF pages.** `needspace` keeps the final answer
    with its surrounding content, but a long derivation may still start on one
    page and finish on the next.  This is a layout polish item, not data loss.

## GUI / operations

14. **Window is large at 200% Windows scaling.** Logical size is 1440×900; a
    200% display makes the physical window 2880×1800.  It fits a 4K screen but
    may exceed a small laptop’s visible area.  Resizing works; the minimum size
    is enforced.
15. **Per-chapter/per-PDF retry is not a dedicated button.** Resume/retry is
    available at the job level and per question in 习题复核; a chapter/failed-PDF
    retry currently means restarting the job with completed stages reused.
    The database/stage checkpoints support adding a narrower retry action.
16. **No signed installer.** The release is a source + virtual-environment
    package with PowerShell install/start scripts, not an MSI/EXE.  It requires
    Python and TeX Live on the target machine.
17. **No automatic model-price updates.** Prices must be entered manually; this
    is deliberate, because guessing vendor pricing would corrupt cost records.
18. **Log tail is append-only text.** It is read-only in the UI and does not
    provide structured filtering beyond the job/stage views.
19. **China network access to OpenAI** may require a compliant gateway/network.
    Configure `openai.base_url`; do not embed proxy credentials in source.

## Copyright / marketplace

20. The deliverable is an original exercise-solution work, not a copy of the
    textbook.  Do not use the publisher cover, scanned pages as a substitute
    product, or answers copied from a pirate solution manual.  Use the original
    cover only to identify the edition; without verified commercial permission,
    use the clearly labelled self-designed visual.
21. Marketplace rules and textbook-edition recognition must be checked by the
    owner before listing.  A hash match does not establish legal resale rights
    or mathematical correctness.

## Next engineering actions

- Run one real OpenAI solve+verify on a licensed, owner-supplied textbook.
- Rotate the API key that was pasted into chat, then store the replacement only
  through Settings/DPAPI.
- Add the owner’s DeepSeek key, run real ISBN/title searches, and verify a
  publisher cover with recorded source/license.
- Connect the owner’s Baidu app and verify upload, download hash, and share for
  a single-volume SKU and a bundle SKU.
- Live-test the new per-SKU upload action against the owner’s Baidu account,
  including folder creation, size verification, optional download hash, and a
  single-volume plus bundle delivery.
- Add chapter/PDF-scoped retry controls.
- Move book lookup to a worker thread.
- Consider a compact 1280×800 layout for 1080p/150% displays.
- Add a structured log viewer if support volume warrants it.
