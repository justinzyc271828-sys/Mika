"""Shared pytest fixtures for WenLianJia."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import SettingsManager  # noqa: E402
from core.db import Database  # noqa: E402
from core.paths import AppPaths  # noqa: E402
from core.secrets import SecretStore  # noqa: E402


@pytest.fixture
def app(tmp_path: Path) -> SimpleNamespace:
    paths = AppPaths(tmp_path / "data")
    paths.ensure()
    settings = SettingsManager(paths)
    db = Database(paths.database)
    secrets = SecretStore(paths.secrets)
    namespace = SimpleNamespace(paths=paths, settings=settings, db=db, secrets=secrets)
    try:
        yield namespace
    finally:
        db.close()
