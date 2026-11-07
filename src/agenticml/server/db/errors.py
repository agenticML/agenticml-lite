"""Client-safe database errors, independent of a backend."""


class DatabaseError(RuntimeError):
    pass


class DatabaseBusy(DatabaseError):
    pass


class SchemaTooNew(DatabaseError):
    pass


class DatabaseUrlError(DatabaseError):
    """Invalid configuration, distinct from failure to read an existing store."""
