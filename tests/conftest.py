"""Fixtures applied to every test module."""

from pathlib import Path

import pytest

from gst_validator import TTLCache


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing real is on the other end, so do not sleep between requests.

    The clients pace themselves by default because the portal's firewall
    blocks an address that bursts. Against a mock transport that is only a
    slow test suite, so the default is zeroed here. `_Pacer` is tested on its
    own, and the default itself is asserted in test_client.
    """
    monkeypatch.setattr("gst_validator.client._DEFAULT_MIN_INTERVAL", 0.0)


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep tests off both shared caches: the process-wide one and the disk."""
    monkeypatch.setattr("gst_validator.client.DEFAULT_CACHE", TTLCache())
    monkeypatch.setattr(
        "gst_validator.cache.DiskCache.default_directory",
        staticmethod(lambda: tmp_path / "disk-cache"),
    )
