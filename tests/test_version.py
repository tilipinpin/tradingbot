from __future__ import annotations

import re

from src import __version__


def test_version_includes_runtime_source_fingerprint() -> None:
    assert re.fullmatch(r"0\.10\.12\+[0-9a-f]{8}", __version__)
