"""Guards on the release machinery."""

import json
import subprocess
import sys
from importlib import metadata

import gst_validator

from .support import (
    VALID_GSTIN,
)


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
