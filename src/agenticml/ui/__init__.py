"""Local, dependency-free experiment dashboard.

CLI integration can import :func:`serve` without starting a server.
"""
from .server import make_server, serve

__all__ = ["make_server", "serve"]
