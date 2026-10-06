"""The in-memory and on-disk caches."""

import argparse
import time
from pathlib import Path

import httpx
import pytest

from gst_validator import (
    DiskCache,
    GSTClient,
    NullCache,
    TaxpayerCache,
    TaxpayerDetails,
    TTLCache,
)
from gst_validator.cli import main

from .support import (
    PAYLOAD,
    VALID_GSTIN,
    answer,
    client_factory,
)


class TestCache:
    def test_hit_skips_the_network(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            if request.url.path.endswith("taxpayerDetails"):
                calls += 1
            return httpx.Response(200, json=PAYLOAD)

        cache = TTLCache()
        transport = httpx.MockTransport(handler)
        with GSTClient(transport=transport, cache=cache) as client:
            first = client.fetch_details(VALID_GSTIN, "1a2b3")
            second = client.fetch_details(VALID_GSTIN, "ignored")
            assert client.cached(VALID_GSTIN) == first
            assert client.fetch_details(VALID_GSTIN, "1a2b3", refresh=True) is not None
        assert first == second
        assert calls == 2  # one cold lookup, one forced refresh

    def test_entries_expire(self) -> None:
        now = 1000.0
        cache = TTLCache(ttl=60, clock=lambda: now)
        cache.set(VALID_GSTIN, TaxpayerDetails(gstin=VALID_GSTIN))
        assert VALID_GSTIN in cache
        now += 61
        assert cache.get(VALID_GSTIN) is None
        assert len(cache) == 0

    def test_lru_eviction(self) -> None:
        cache = TTLCache(maxsize=2)
        for suffix in "abc":
            cache.set(suffix, TaxpayerDetails(gstin=suffix))
        assert len(cache) == 2
        assert cache.get("a") is None

    def test_rejects_bad_configuration(self) -> None:
        with pytest.raises(ValueError):
            TTLCache(ttl=0)
        with pytest.raises(ValueError):
            TTLCache(maxsize=0)

    def test_null_cache_disables_caching(self) -> None:
        cache = NullCache()
        cache.set(VALID_GSTIN, TaxpayerDetails(gstin=VALID_GSTIN))
        assert cache.get(VALID_GSTIN) is None


class TestDiskCache:
    """The CLI is short-lived, so its cache has to outlive the process."""

    def test_round_trips_through_a_new_instance(self, tmp_path: Path) -> None:
        details = TaxpayerDetails.from_payload(dict(PAYLOAD))
        DiskCache(directory=tmp_path).set(VALID_GSTIN, details)
        # A different instance stands in for the next CLI invocation.
        restored = DiskCache(directory=tmp_path).get(VALID_GSTIN)
        assert restored is not None
        assert restored.legal_name == "ACME TRADERS"
        assert restored.raw == PAYLOAD  # the portal body is what is stored

    def test_entries_expire(self, tmp_path: Path) -> None:
        cache = DiskCache(directory=tmp_path, ttl=0.01)
        cache.set(VALID_GSTIN, TaxpayerDetails.from_payload(dict(PAYLOAD)))
        time.sleep(0.02)
        assert cache.get(VALID_GSTIN) is None

    def test_corrupt_and_missing_entries_are_a_miss_not_a_crash(self, tmp_path: Path) -> None:
        cache = DiskCache(directory=tmp_path)
        assert cache.get(VALID_GSTIN) is None  # nothing written yet
        tmp_path.mkdir(exist_ok=True)
        (tmp_path / f"{VALID_GSTIN}.json").write_text("{not json")
        assert cache.get(VALID_GSTIN) is None

    def test_unwritable_directory_does_not_fail_the_lookup(self, tmp_path: Path) -> None:
        blocked = tmp_path / "file-not-a-dir"
        blocked.write_text("")
        cache = DiskCache(directory=blocked / "sub")
        cache.set(VALID_GSTIN, TaxpayerDetails.from_payload(dict(PAYLOAD)))  # must not raise
        assert cache.get(VALID_GSTIN) is None

    def test_clear_removes_entries(self, tmp_path: Path) -> None:
        cache = DiskCache(directory=tmp_path)
        cache.set(VALID_GSTIN, TaxpayerDetails.from_payload(dict(PAYLOAD)))
        assert cache.clear() == 1
        assert cache.get(VALID_GSTIN) is None

    def test_a_second_cli_run_spends_no_captcha(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The point of the whole thing: look twice, solve one captcha."""
        captchas = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal captchas
            match request.url.path:
                case "/services/searchtp":
                    return httpx.Response(200, text="<html></html>")
                case "/services/captcha":
                    captchas += 1
                    return httpx.Response(
                        200, content=b"\x89PNG", headers={"content-type": "image/png"}
                    )
                case "/services/api/search/taxpayerDetails":
                    return httpx.Response(200, json=PAYLOAD)
                case _:
                    return httpx.Response(200, json={"status": 1, "data": []})

        shared = DiskCache(directory=tmp_path)
        monkeypatch.setattr(
            "gst_validator.cli.GSTClient",
            client_factory(httpx.MockTransport(handler), cache=shared),
        )

        def pinned_cache(_args: argparse.Namespace) -> TaxpayerCache:
            return shared

        monkeypatch.setattr("gst_validator.cli._cache_for", pinned_cache)
        monkeypatch.setattr("builtins.input", answer("1a2b3"))

        assert main([VALID_GSTIN, "--json"]) == 0
        assert main([VALID_GSTIN, "--json"]) == 0
        capsys.readouterr()
        assert captchas == 1


class TestDiskCacheKeySafety:
    """A cache key becomes a file name, so it must not be able to escape."""

    def test_traversal_key_is_refused(self, tmp_path: Path) -> None:
        cache = DiskCache(directory=tmp_path / "cache")
        details = TaxpayerDetails.from_payload({"gstin": VALID_GSTIN})
        for key in ("../../escaped", "a/b", "..", "", "x" * 40, "lower-case"):
            with pytest.raises(ValueError, match="unsafe cache key"):
                cache.set(key, details)
        assert [p for p in tmp_path.iterdir() if p.is_file()] == []

    def test_a_gstin_is_a_valid_key(self, tmp_path: Path) -> None:
        cache = DiskCache(directory=tmp_path)
        details = TaxpayerDetails.from_payload({"gstin": VALID_GSTIN})
        cache.set(VALID_GSTIN, details)
        assert cache.get(VALID_GSTIN) is not None

    def test_every_live_layout_is_a_valid_key(self, tmp_path: Path) -> None:
        """The non-PAN layouts must not trip the guard."""
        cache = DiskCache(directory=tmp_path)
        details = TaxpayerDetails.from_payload({"gstin": VALID_GSTIN})
        for key in ("9917USA29016OS6", "2317UNO00001UND", "27AAICA3918J1CT"):
            cache.set(key, details)
            assert cache.get(key) is not None


class TestDiskCacheResilience:
    """A cache is an optimisation: it must never be the thing that fails."""

    def test_an_unserialisable_payload_does_not_break_the_lookup(self, tmp_path: Path) -> None:
        cache = DiskCache(directory=tmp_path)
        details = TaxpayerDetails.from_payload({"gstin": VALID_GSTIN, "odd": {1, 2}})
        cache.set(VALID_GSTIN, details)  # must not raise
        assert cache.get(VALID_GSTIN) is None

    def test_an_unsafe_key_is_still_heard(self, tmp_path: Path) -> None:
        """Widening the write guard must not swallow a caller's mistake."""
        cache = DiskCache(directory=tmp_path)
        with pytest.raises(ValueError, match="unsafe cache key"):
            cache.set("../escape", TaxpayerDetails.from_payload({"gstin": VALID_GSTIN}))

    def test_clear_sweeps_temporary_files_from_a_crashed_run(self, tmp_path: Path) -> None:
        (tmp_path / f"{VALID_GSTIN}.4242.tmp").write_text("{}")
        cache = DiskCache(directory=tmp_path)
        cache.set(VALID_GSTIN, TaxpayerDetails.from_payload({"gstin": VALID_GSTIN}))
        assert cache.clear() == 2
        assert list(tmp_path.iterdir()) == []


class TestDefaultCacheDirectory:
    """The platform branches, which CI never reaches.

    The autouse fixture replaces this method for every other test and CI runs
    on Linux only, so without this the Windows and macOS paths ship unexecuted
    while the metadata claims the package is OS independent.
    """

    @staticmethod
    def _directory(monkeypatch: pytest.MonkeyPatch, platform: str, **environ: str) -> Path:
        monkeypatch.undo()  # drop the autouse stub so the real method runs
        monkeypatch.setattr("gst_validator.cache.sys.platform", platform)
        for name in ("LOCALAPPDATA", "XDG_CACHE_HOME"):
            monkeypatch.delenv(name, raising=False)
        for name, value in environ.items():
            monkeypatch.setenv(name, value)
        return DiskCache.default_directory()

    def test_macos_uses_library_caches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        path = self._directory(monkeypatch, "darwin")
        assert path.parts[-3:] == ("Library", "Caches", "gst-validator")

    def test_windows_honours_localappdata(self, monkeypatch: pytest.MonkeyPatch) -> None:
        path = self._directory(monkeypatch, "win32", LOCALAPPDATA=r"C:\Users\x\AppData\Local")
        assert path.name == "gst-validator"
        assert "Local" in str(path)

    def test_windows_falls_back_when_localappdata_is_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = self._directory(monkeypatch, "win32")
        assert path.parts[-3:] == ("AppData", "Local", "gst-validator")

    def test_linux_honours_xdg_cache_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        path = self._directory(monkeypatch, "linux", XDG_CACHE_HOME="/custom/cache")
        assert path == Path("/custom/cache/gst-validator")

    def test_linux_falls_back_to_dot_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        path = self._directory(monkeypatch, "linux")
        assert path.parts[-2:] == (".cache", "gst-validator")
