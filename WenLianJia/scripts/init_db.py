"""Initialize the WenLianJia database and print the local layout."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from core.config import SettingsManager  # noqa: E402
from core.db import Database  # noqa: E402
from core.library.migrations import create_library_engine, ensure_schema, schema_version  # noqa: E402
from core.paths import get_paths  # noqa: E402


def main() -> int:
    paths = get_paths()
    settings = SettingsManager(paths)
    db = Database(paths.database)
    engine = create_library_engine(paths.database)
    ensure_schema(engine)
    tables = db.query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    names = [row["name"] for row in tables]
    print(f"data root : {paths.root}")
    print(f"database  : {paths.database}")
    print(f"settings  : {paths.settings}")
    print(f"table list: {', '.join(names)}")
    print(f"tables    : {len(names)}")
    print(f"library   : schema version {schema_version(engine)}")
    print(f"products  : {db.count_products() if hasattr(db, 'count_products') else len(db.list_products())}")
    print("database initialized")
    # Touch the settings file so the first GUI run has an explicit file to edit.
    settings.save()
    engine.dispose()
    db.close_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
