"""Chapter 26 acceptance run: 100 textbooks / 300 SKUs.

Run from the repository root::

    .\\.venv\\Scripts\\python.exe .\\scripts\\acceptance_library.py

The script uses a disposable data root (``.tmp_acceptance`` by default) and
checks retrieval, pagination, batch price changes, SKU uniqueness, bundle file
reuse, version switching, asset references, backup/restore and export.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from core.config import SettingsManager  # noqa: E402
from core.db import Database  # noqa: E402
from core.library.backup import BackupManager  # noqa: E402
from core.library.batch import BatchEngine, read_table  # noqa: E402
from core.library.seed import seed_library  # noqa: E402
from core.library.service import LibraryService  # noqa: E402
from core.paths import AppPaths  # noqa: E402


def main() -> int:
    data_root = Path(os.environ.get("WLJ_ACCEPTANCE_DIR", ROOT / ".tmp_acceptance"))
    if data_root.exists():
        shutil.rmtree(data_root)
    paths = AppPaths(data_root)
    paths.ensure()
    db = Database(paths.database)
    service = LibraryService(paths, SettingsManager(paths), db)
    batch = BatchEngine(service)
    backup = BackupManager(service)
    checks: dict[str, object] = {}
    started = time.perf_counter()
    try:
        report = seed_library(service, books=100, skus=300)
        checks["seed"] = report.as_dict()

        page1 = service.list_books(page=1, page_size=20)
        page2 = service.list_books(page=2, page_size=20)
        checks["books"] = {
            "total": page1.total,
            "pages": page1.pages,
            "page_size": len(page1.items),
            "disjoint": not ({b.id for b in page1.items} & {b.id for b in page2.items}),
            "search": service.list_books(q="SEED-001").total,
        }
        skus = service.list_skus(page=1, page_size=100)
        all_codes = [sku.sku_code for sku in service.list_skus(page_size=500).items]
        checks["skus"] = {
            "total": skus.total,
            "unique_codes": len(all_codes) == len(set(all_codes)),
            "page_size": len(skus.items),
            "lifecycle_states": len(service.overview()["lifecycle"]),
        }

        target = [sku for sku in service.list_skus(page_size=500).items if sku.kind == "single"][:10]
        rows = [{"SKU编号": sku.sku_code, "售价": "8.88"} for sku in target]
        job = batch.preview("update_price", rows, options={}, actor="acceptance")
        run = batch.run(job.id)
        changed = sum(1 for sku in target if service.get_sku(sku.id).price_cents == 888)
        checks["batch_price"] = {
            "rows": len(rows),
            "status": run.status.value,
            "changed": changed,
            "preview_valid": sum(1 for item in job.preview if item["valid"]),
        }

        book = next(
            book
            for book in service.list_books(page_size=500).items
            if len(service.list_versions(book.id)) > 1
        )
        versions = service.list_versions(book.id)
        sku = next(s for s in service.list_skus(book_id=book.id, page_size=500).items if s.kind == "single")
        service.set_sku_version(sku.id, versions[1].id)
        checks["version_switch"] = {
            "book": book.code,
            "versions": [version.version_label for version in versions],
            "sku_version": service.get_sku(sku.id).version_id == versions[1].id,
        }

        bundles = [sku for sku in service.list_skus(page_size=500).items if sku.kind == "bundle"]
        bundle = bundles[0]
        checks["bundle_reuse"] = {
            "bundles": len(bundles),
            "parts": len(bundle.items),
            "products_for_book": len(service.db.list_productions(bundle.book_id)),
            "single_products": len(
                [
                    s
                    for s in service.list_skus(book_id=bundle.book_id, page_size=500).items
                    if s.kind == "single"
                ]
            ),
        }

        linked = [asset for asset in service.assets.list_assets(page_size=500).items if asset["refs"]]
        shared = max((asset["refs"] for asset in linked), default=0)
        protected = False
        if linked:
            try:
                service.assets.delete_asset(linked[0]["id"], confirm=True)
            except Exception:
                protected = True
        checks["assets"] = {
            "total": service.assets.usage_summary()["assets"],
            "linked": len(linked),
            "max_refs": shared,
            "shared_delete_blocked": protected,
        }

        manifest = backup.create_backup(label="acceptance")
        verification = backup.verify_backup(manifest["database"])
        mutated = target[0]
        service.set_price(mutated.id, "77.77")
        restored = backup.restore_backup(manifest["database"], confirm=True)
        checks["backup"] = {
            "file": manifest["database"],
            "verify_ok": verification["ok"],
            "restore_price": service.get_sku(mutated.id).price_cents,
            "restored_from": restored["restored_from"],
            "integrity": backup.integrity_check()["ok"],
        }

        books_csv = data_root / "export_books.csv"
        skus_xlsx = data_root / "export_skus.xlsx"
        batch.export_books(books_csv, actor="acceptance")
        batch.export_skus(skus_xlsx, actor="acceptance")
        checks["export"] = {
            "books_rows": len(read_table(books_csv)),
            "skus_rows": len(read_table(skus_xlsx)),
            "books_file": str(books_csv),
            "skus_file": str(skus_xlsx),
        }
    finally:
        service.engine.dispose()
        db.close_all()

    checks["elapsed_seconds"] = round(time.perf_counter() - started, 2)
    checks["data_root"] = str(data_root)
    ok = (
        checks["books"]["total"] == 100
        and checks["books"]["disjoint"]
        and checks["skus"]["total"] == 300
        and checks["skus"]["unique_codes"]
        and checks["batch_price"]["changed"] == checks["batch_price"]["rows"]
        and checks["version_switch"]["sku_version"]
        and checks["assets"]["shared_delete_blocked"]
        and checks["assets"]["max_refs"] >= 2
        and checks["backup"]["verify_ok"]
        and checks["backup"]["integrity"]
        and checks["export"]["books_rows"] >= 100
        and checks["export"]["skus_rows"] >= 300
    )
    checks["ok"] = bool(ok)
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
