"""Polymarket BTC trading bot."""

from hashlib import sha256
from pathlib import Path


_BASE_VERSION = "0.10.12"


def _source_fingerprint() -> str:
    """Return a stable fingerprint for the Python source actually being run."""
    digest = sha256()
    source_root = Path(__file__).resolve().parent
    for path in sorted(source_root.glob("*.py")):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:8]


__version__ = f"{_BASE_VERSION}+{_source_fingerprint()}"
