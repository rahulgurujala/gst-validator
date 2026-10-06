"""The captcha image the portal binds to a search session."""

import base64
import os
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Captcha", "StrPath"]

type StrPath = str | os.PathLike[str]
"""Anything :func:`open` accepts: a ``str`` or a :class:`pathlib.Path`."""


@dataclass(frozen=True, slots=True)
class Captcha:
    """Captcha image bound to the client session that fetched it."""

    content: bytes
    media_type: str = "image/png"

    @property
    def base64(self) -> str:
        """Bare base64 payload - hand this to a human or a solver service."""
        return base64.b64encode(self.content).decode("ascii")

    @property
    def data_uri(self) -> str:
        """``data:`` URI, drop-in for an ``<img src=...>`` in a web UI."""
        return f"data:{self.media_type};base64,{self.base64}"

    def save(self, path: StrPath) -> None:
        """Write the raw image bytes to ``path``."""
        Path(path).write_bytes(self.content)
