"""Fixtures applied to every test module."""

from pathlib import Path

import pytest

from gst_validator import TTLCache


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep tests off both shared caches: the process-wide one and the disk."""
    monkeypatch.setattr("gst_validator.client.DEFAULT_CACHE", TTLCache())
    monkeypatch.setattr(
        "gst_validator.cache.DiskCache.default_directory",
        staticmethod(lambda: tmp_path / "disk-cache"),
    )
