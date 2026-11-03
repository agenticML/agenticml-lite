"""Standard-library on-prem ingest server over a plain v1 store."""
from __future__ import annotations

from .keys import KeyStore
from .app import make_server, serve

__all__ = ["KeyStore", "make_server", "serve"]
