"""Environment capture: Python version, platform, hostname, installed packages.

Kept dependency-free. GPU/CUDA capture is best-effort and only attempted when
the relevant libraries are already importable; we never import them eagerly.
"""

from __future__ import annotations

import logging
import platform as _platform
import socket
import sys

logger = logging.getLogger("agenticml.env")


def _installed_packages() -> dict[str, str]:
    """Return {distribution_name: version}. Empty dict on failure."""
    try:
        from importlib.metadata import distributions
    except ImportError:  # pragma: no cover - py<3.8
        return {}

    packages: dict[str, str] = {}
    try:
        for dist in distributions():
            name = dist.metadata.get("Name") if dist.metadata else None
            if not name:
                continue
            packages[name.lower()] = dist.version or ""
    except Exception:
        logger.exception("failed to enumerate installed packages")
        return {}
    return packages


def _hostname() -> str | None:
    try:
        return socket.gethostname() or None
    except Exception:
        return None


def capture_environment() -> dict:
    """Snapshot of the runtime environment, suitable for the /init payload."""
    return {
        "python_version": sys.version.split()[0],
        "platform": _platform.platform(),
        "hostname": _hostname(),
        "packages": _installed_packages(),
    }
