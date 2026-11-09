"""Validated workspace plans and quota decisions (decimal GB, UTC periods)."""
import json
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re

from .keys import ClientError

KEYS = ('storage_bytes', 'upload_bytes_per_day', 'requests_per_minute', 'people')
UNLIMITED = dict.fromkeys(KEYS)


class LimitReached(ClientError):
    pass


def free_workspace_limit_error(limit):
    if limit == 0:
        return LimitReached('free workspaces are not available')
    return LimitReached('you already have a free workspace')


def _keys(value, expected, where):
    if not isinstance(value, dict):
        raise ValueError(f'{where} must be an object')
    unknown = value.keys() - set(expected)
    missing = set(expected) - value.keys()
    if unknown:
        raise ValueError(f'{where}: unknown keys: {", ".join(sorted(unknown))}')
    if missing:
        raise ValueError(f'{where}: missing keys: {", ".join(sorted(missing))}')


def _number(value, where):
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError(f'{where} must be a non-negative integer or null')
    return value


def validate_limits(value):
    _keys(value, KEYS, 'limits')
    return {key: _number(value[key], key) for key in KEYS}


def utc_now():
    return datetime.now(timezone.utc)


class Plans:
    def __init__(self, config, *, clock=None):
        _keys(config, ('default_plan', 'free_workspaces_per_account', 'plans'), 'limits file')
        if config['default_plan'] != 'free':
            raise ValueError('default_plan must be free')
        self.default_plan = 'free'
        self.free_workspaces_per_account = _number(config['free_workspaces_per_account'], 'free_workspaces_per_account')
        _keys(config['plans'], ('free',), 'plans')
        self.free = validate_limits(config['plans']['free'])
        # Period keys use the process UTC clock in 18c; package 20 moves them
        # to the database clock so workers cannot disagree across a boundary.
        self.clock = clock or utc_now

    @classmethod
    def load(cls, path):
        try:
            return cls(json.loads(Path(path).read_text()))
        except (OSError, ValueError) as exc:
            raise ValueError(f'invalid limits file {path}: {exc}') from exc

    def limits_for(self, workspace_row):
        if workspace_row is None:
            from .access import WorkspaceNotFound
            raise WorkspaceNotFound()
        if workspace_row['plan'] == 'free':
            return dict(self.free)
        if workspace_row['plan'] != 'custom':
            raise ValueError('unknown workspace plan')
        return validate_limits(json.loads(workspace_row['custom_limits']))

    def check_upload(self, limits, usage, length, growth=None):
        maximum = limits['storage_bytes']
        stored = usage['stored_bytes']
        if growth is not None and growth > 0 and maximum is not None and stored + growth > maximum:
            raise LimitReached(f'storage limit reached: {stored / 1e9:.1f} GB of {maximum / 1e9:.1f} GB used; delete runs or ask for more')
        now = self.clock()
        day = now.date().isoformat()
        uploaded = usage['upload_bytes'] if usage['upload_day'] and usage['upload_day'] >= day else 0
        maximum = limits['upload_bytes_per_day']
        if maximum is not None and uploaded + length > maximum:
            remaining = math.ceil(((now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0) - now).total_seconds() / 60)
            hours, minutes = divmod(remaining, 60)
            raise LimitReached(f'daily upload limit reached: {uploaded / 1e9:.1f} GB of {maximum / 1e9:.1f} GB today; resets at 00:00 UTC (in {hours} h {minutes} min)')

    def request(self, db, workspace_id):
        from .db.usage import UsageRepo
        from .db.workspaces import WorkspaceRepo
        limits = self.limits_for(WorkspaceRepo(db).by_id(workspace_id))
        now = self.clock().timestamp()
        count = UsageRepo(db, workspace_id).count_request(int(now // 60))
        maximum = limits['requests_per_minute']
        return max(1, math.ceil((int(now // 60) + 1) * 60 - now)) if maximum is not None and count > maximum else None

    def check_people(self, db, workspace_id, tx):
        from .db.usage import UsageRepo
        from .db.workspaces import WorkspaceRepo
        maximum = self.limits_for(WorkspaceRepo(db).by_id(workspace_id, tx))['people']
        people = UsageRepo(db, workspace_id).people(tx, now=self.clock())
        if maximum is not None and people >= maximum:
            raise LimitReached(f'people limit reached ({people} of {maximum})')


def parse_size(value):
    if value.lower() == 'unlimited':
        return None
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)(KB|MB|GB|TB)?', value, re.I)
    if not match:
        raise ValueError('size must be bytes or decimal KB/MB/GB/TB, or unlimited')
    try:
        result = Decimal(match[1]) * 1000 ** ({None: 0, 'KB': 1, 'MB': 2, 'GB': 3, 'TB': 4}[match[2].upper() if match[2] else None])
        if result != result.to_integral_value():
            raise ValueError('size must be a whole number of bytes')
        return int(result)
    except InvalidOperation as exc:
        raise ValueError('invalid size') from exc


def parse_count(value):
    if value.lower() == 'unlimited':
        return None
    if not re.fullmatch('[0-9]+', value):
        raise ValueError('limit must be a non-negative integer or unlimited')
    return int(value)
