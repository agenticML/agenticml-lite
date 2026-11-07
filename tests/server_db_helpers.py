"""Repository-based inspection and legacy fixtures for server storage tests."""
from dataclasses import asdict
import json

from agenticml.server.db import for_server_dir
from agenticml.server.db.repos import AuditRepo


def key_object(store):
    return {'version': 2, 'keys': [asdict(row) for row in store.repo.list()]}


def user_object(store):
    return {'version': 1, 'users': [asdict(row) for row in store.repo.list()]}


def audit_rows(directory):
    db = for_server_dir(directory, create=False)
    return list(reversed(AuditRepo(db).tail(1000))) if db is not None else []


def audit_text(directory):
    return json.dumps(audit_rows(directory))
