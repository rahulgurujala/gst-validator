"""Guards on the release machinery."""

import json
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import gst_validator

from .support import (
    VALID_GSTIN,
)

ROOT = Path(__file__).resolve().parent.parent


class TestPackaging:
    """Guards that the release machinery cannot silently drift."""

    def test_dunder_version_matches_the_installed_distribution(self) -> None:
        """release-please bumps both; an edit to the marker would desync them."""
        assert gst_validator.__version__ == metadata.version("gst-validator")

    def test_everything_in_dunder_all_is_importable(self) -> None:
        missing = [name for name in gst_validator.__all__ if not hasattr(gst_validator, name)]
        assert missing == []

    def test_module_entry_point_runs(self) -> None:
        """`python -m gst_validator` is documented, so it must work."""
        result = subprocess.run(
            [sys.executable, "-m", "gst_validator", VALID_GSTIN, "--offline", "--json"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert json.loads(result.stdout)["valid"] is True


class TestProseStyle:
    """Guards a house rule that two hand sweeps have already failed to hold.

    Em-dashes were removed from the repository once; documents written
    afterwards reintroduced five of them. A check is cheaper than remembering.
    """

    @staticmethod
    def _tracked_text_files() -> list[str]:
        # --others --exclude-standard adds files not yet committed. Without
        # them a brand new document escapes every check here until someone
        # remembers to re-run the suite after `git add`, which is exactly
        # when a new document is least likely to be clean.
        listing = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            check=True,
            cwd=ROOT,
        )
        suffixes = (".md", ".py", ".toml", ".yml", ".yaml", ".json", ".txt")
        names = {name for name in listing.stdout.split("\0") if name.endswith(suffixes)}
        return sorted(name for name in names if (ROOT / name).is_file())

    def test_no_smart_punctuation(self) -> None:
        # Built with chr() so this file does not trip its own check: the
        # formatter rewrites a "\u2014" escape into the character itself.
        # Em-dash, en-dash, ellipsis, and the four curly quotes.
        dashes = tuple(
            chr(point) for point in (0x2014, 0x2013, 0x2026, 0x201C, 0x201D, 0x2018, 0x2019)
        )
        offenders: list[str] = []
        for name in self._tracked_text_files():
            if name == "uv.lock":
                continue
            text = (ROOT / name).read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), start=1):
                if any(dash in line for dash in dashes):
                    offenders.append(f"{name}:{number}")
        assert offenders == [], f"use plain ASCII punctuation: {offenders}"


class TestDocumentationLinks:
    """Relative links rot silently: nothing renders a 404 until a reader clicks.

    Three of the five navigation links at the top of the README pointed at
    sections that had moved into docs/ when it was cut down to a landing page.
    """

    @staticmethod
    def _anchors(text: str) -> set[str]:
        """GitHub's heading slug: lowercased, punctuation dropped, spaces to -."""
        found: set[str] = set()
        for line in text.splitlines():
            heading = re.match(r"^#{1,6}\s+(.*)", line)
            if heading:
                slug = re.sub(r"[^\w\s-]", "", heading.group(1).lower()).strip()
                found.add(slug.replace(" ", "-"))
        return found

    def test_every_relative_link_and_anchor_resolves(self) -> None:
        broken: list[str] = []
        for path in sorted(ROOT.glob("*.md")) + sorted((ROOT / "docs").glob("*.md")):
            text = path.read_text(encoding="utf-8")
            own = self._anchors(text)
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                file_part, _, fragment = target.partition("#")
                name = path.relative_to(ROOT)
                if not file_part:
                    if fragment and fragment not in own:
                        broken.append(f"{name} -> #{fragment}")
                    continue
                destination = (path.parent / file_part).resolve()
                if not destination.exists():
                    broken.append(f"{name} -> {target} (no such file)")
                elif fragment and fragment not in self._anchors(
                    destination.read_text(encoding="utf-8")
                ):
                    broken.append(f"{name} -> {target} (no such anchor)")
        assert broken == [], f"broken documentation links: {broken}"


class TestDocumentedEnumerations:
    """Lists in the docs that mirror something in the code, checked against it.

    Each of these has gone stale at least once: the exception tree missed
    `PortalBlockedError` for a whole release, the field tables lagged the
    dataclasses, and a new module never reached the file map. Catching that
    by reading is unreliable, so it is a test.
    """

    @staticmethod
    def _doc(name: str) -> str:
        return (ROOT / name).read_text(encoding="utf-8")

    def test_every_exception_is_in_the_hierarchy_diagram(self) -> None:
        import inspect

        import gst_validator as package

        tree = self._doc("docs/library.md")
        start = tree.index("GSTValidatorError\n")
        diagram = tree[start : start + 500]
        missing = [
            name
            for name in package.__all__
            if inspect.isclass(getattr(package, name))
            and issubclass(getattr(package, name), package.GSTValidatorError)
            and getattr(package, name) is not package.GSTValidatorError
            and name not in diagram
        ]
        assert missing == [], f"exceptions absent from the diagram: {missing}"

    def test_every_search_result_field_is_documented(self) -> None:
        import dataclasses

        import gst_validator as package

        doc = self._doc("docs/library.md")
        missing: list[str] = []
        for name in (
            "HSNCode",
            "CompositionTaxpayer",
            "ApplicationStatus",
            "ReferenceNumber",
            "GSTPractitioner",
            "TemporaryRegistration",
        ):
            cls = getattr(package, name)
            row = next(
                (line for line in doc.splitlines() if f"`{name}`" in line and line.startswith("|")),
                "",
            )
            assert row, f"{name} has no row in the field table"
            missing += [
                f"{name}.{f.name}"
                for f in dataclasses.fields(cls)
                if f.name != "unmapped" and f.name not in row
            ]
        assert missing == [], f"fields absent from the table: {missing}"

    def test_every_endpoint_is_in_the_portal_table(self) -> None:
        import inspect

        from gst_validator import GSTClient

        portal = self._doc("docs/portal.md")
        table = portal[: portal.index("## The GSTIN layouts")]
        missing = [
            name
            for name, _ in inspect.getmembers(GSTClient, inspect.isfunction)
            if name.startswith(("fetch", "search", "track", "verify")) and name not in table
        ]
        assert missing == [], f"endpoints absent from the table: {missing}"

    def test_every_module_is_in_the_file_map(self) -> None:
        contributing = self._doc("CONTRIBUTING.md")
        missing = [
            path.name
            for path in sorted((ROOT / "src" / "gst_validator").glob("*.py"))
            if path.name not in contributing
        ]
        assert missing == [], f"modules absent from the file map: {missing}"

    def test_every_test_module_is_in_the_file_map(self) -> None:
        contributing = self._doc("CONTRIBUTING.md")
        missing = [
            path.name
            for path in sorted((ROOT / "tests").glob("test_*.py"))
            if path.name not in contributing
        ]
        assert missing == [], f"test modules absent from the file map: {missing}"

    def test_the_documented_defaults_match_the_code(self) -> None:
        """The docs say "one per second" and "five minutes"; check both.

        Read from the source rather than imported: conftest zeroes the
        interval for every other test, so an import here would see that
        instead of the declared default.
        """
        source = (ROOT / "src" / "gst_validator" / "limits.py").read_text(encoding="utf-8")
        assert "DEFAULT_MIN_INTERVAL: Final = 1.0" in source
        assert "DEFAULT_COOL_OFF: Final = 300.0" in source
        for name in ("docs/library.md", "docs/deployment.md"):
            text = self._doc(name)
            assert "one per second" in text or "one request per second" in text
            assert "five minutes" in text
