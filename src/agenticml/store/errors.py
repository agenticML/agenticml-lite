"""Shared storage errors without server or optional dependencies."""


class ObjectStoreUnavailable(Exception):
    """The object backend cannot currently serve a request."""
