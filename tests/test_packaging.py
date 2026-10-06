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
        listing = subprocess.run(
            ["git", "ls-files", "-z"],
            capture_output=True,
            text=True,
            check=True,
            cwd=ROOT,
        )
        suffixes = (".md", ".py", ".toml", ".yml", ".yaml", ".json", ".txt")
        return [name for name in listing.stdout.split("\0") if name.endswith(suffixes)]

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
