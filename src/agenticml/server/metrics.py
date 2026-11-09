"""Fold scalar ``.bin`` records into a per-series summary.

``last_value`` / ``last_step`` are the last record in append order (not the
largest step). ``min_value`` / ``max_value`` skip NaN and keep ±inf.
``count`` includes NaN. NaN is never stored; ±inf are stored as-is.
"""
from __future__ import annotations

import json
import math
import re

from agenticml.store.layout import RECORD_SIZE, RECORD_STRUCT, open_nofollow

CHUNK = 1 << 20
INDEX_READ_LIMIT = 16 * 1024 * 1024
_SERIES_BIN = re.compile(r'^scalars/(0|[1-9][0-9]{0,8})\.bin$')
_STATS = frozenset(('last', 'min', 'max'))
_OPS = frozenset(('lt', 'le', 'gt', 'ge', 'eq'))
_OP_SQL = {'lt': '<', 'le': '<=', 'gt': '>', 'ge': '>=', 'eq': '='}
_STAT_COL = {'last': 'last_value', 'min': 'min_value', 'max': 'max_value'}
_NUMBER = re.compile(r'[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?\Z')
MAX_METRIC_NAMES = 8
MAX_METRIC_NAME = 200


class RunsQueryError(ValueError):
    """Invalid ``/api/runs`` metric query; the dashboard answers 422."""


def empty_summary():
    return {
        'last_value': None,
        'min_value': None,
        'max_value': None,
        'count': 0,
        'last_step': None,
        'decoded_bytes': 0,
    }


def series_id_from_path(path):
    match = _SERIES_BIN.fullmatch(path)
    return int(match.group(1)) if match else None


def fold(summary, data):
    """Fold complete ``<If`` records in ``data`` into ``summary``."""
    usable = len(data) - (len(data) % RECORD_SIZE)
    if usable <= 0:
        return dict(summary)
    last_value = summary['last_value']
    min_value = summary['min_value']
    max_value = summary['max_value']
    count = summary['count']
    last_step = summary['last_step']
    for step, value in RECORD_STRUCT.iter_unpack(memoryview(data)[:usable]):
        count += 1
        last_step = step
        if math.isnan(value):
            last_value = None
            continue
        last_value = value
        if min_value is None or value < min_value:
            min_value = value
        if max_value is None or value > max_value:
            max_value = value
    return {
        'last_value': last_value,
        'min_value': min_value,
        'max_value': max_value,
        'count': count,
        'last_step': last_step,
        'decoded_bytes': summary['decoded_bytes'] + usable,
    }


def merge_summary(base, extra):
    """Combine an existing summary with a fold of later bytes."""
    if extra['count'] == 0:
        return {
            **base,
            'decoded_bytes': base['decoded_bytes'] + extra['decoded_bytes'],
        }
    return {
        'last_value': extra['last_value'],
        'min_value': _extreme(base['min_value'], extra['min_value'], min),
        'max_value': _extreme(base['max_value'], extra['max_value'], max),
        'count': base['count'] + extra['count'],
        'last_step': extra['last_step'],
        'decoded_bytes': base['decoded_bytes'] + extra['decoded_bytes'],
    }


def _extreme(left, right, pick):
    if left is None:
        return right
    if right is None:
        return left
    return pick(left, right)


def summarize_file(path, size):
    """Fold a committed prefix of ``path``, never reading past ``size``."""
    summary = empty_summary()
    if size <= 0:
        return summary
    remaining = size
    try:
        with open_nofollow(path, 'rb') as handle:
            while remaining:
                chunk = handle.read(min(CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                summary = fold(summary, chunk)
    except FileNotFoundError:
        return empty_summary()
    return summary


def read_range(path, start, end):
    if end <= start:
        return b''
    with open_nofollow(path, 'rb') as handle:
        handle.seek(start)
        return handle.read(end - start)


def parse_index_obj(obj):
    """``{series_id: name}`` from a scalars index object, or None if unparsable."""
    if not isinstance(obj, dict):
        return None
    series = obj.get('series')
    if not isinstance(series, dict):
        return None
    names = {}
    for key, info in series.items():
        if not isinstance(key, str) or not isinstance(info, dict):
            continue
        series_id = info.get('id')
        if type(series_id) is int and series_id >= 0:
            names[series_id] = key
    return names


def parse_index_bytes(body):
    try:
        obj = json.loads(body.decode('utf-8'))
    except (ValueError, RecursionError, UnicodeDecodeError):
        return None
    return parse_index_obj(obj)


def read_index_names(path, size=None):
    """Parse ``scalars/index.json`` from disk; empty dict when missing or invalid."""
    try:
        with open_nofollow(path, 'rb') as handle:
            limit = INDEX_READ_LIMIT if size is None else min(size, INDEX_READ_LIMIT)
            body = handle.read(limit)
    except (OSError, FileNotFoundError):
        return {}
    parsed = parse_index_bytes(body)
    return parsed if parsed is not None else {}


def parse_runs_query(query):
    """Parse ``metrics`` / ``sort`` / ``order`` / ``filter`` from ``parse_qs``.

    ``sort`` is ``(name, stat)`` or None. Each filter is
    ``(name, stat, op, number)``. Other ``sort`` values, bad syntax, a
    non-finite number, or more than eight names raise ``RunsQueryError``.
    """
    metrics = _parse_metrics(query.get('metrics', [None])[0])
    sort = _parse_sort(query.get('sort', [None])[0])
    order = query.get('order', ['asc'])[0]
    if order in (None, ''):
        order = 'asc'
    if order not in ('asc', 'desc'):
        raise RunsQueryError('invalid order')
    filters = [_parse_filter(item) for item in query.get('filter', [])]
    if len(filters) > MAX_METRIC_NAMES:
        raise RunsQueryError('too many filters')
    return metrics, sort, order, filters


def stat_column(stat):
    return _STAT_COL[stat]


def op_sql(op):
    return _OP_SQL[op]


def _parse_metrics(raw):
    if raw in (None, ''):
        return []
    names = raw.split(',')
    if len(names) > MAX_METRIC_NAMES:
        raise RunsQueryError('too many metrics')
    for name in names:
        if not name or len(name) > MAX_METRIC_NAME:
            raise RunsQueryError('invalid metrics')
    return names


def _parse_sort(raw):
    if raw in (None, ''):
        return None
    name, stat = _split_metric(raw, 1, 'invalid sort')
    if stat not in _STATS:
        raise RunsQueryError('invalid sort')
    return name, stat


def _parse_filter(raw):
    name, stat, op, number = _split_metric(raw, 3, 'invalid filter')
    if stat not in _STATS or op not in _OPS:
        raise RunsQueryError('invalid filter')
    if _NUMBER.fullmatch(number) is None:
        raise RunsQueryError('invalid filter')
    try:
        value = float(number)
    except (TypeError, ValueError):
        raise RunsQueryError('invalid filter') from None
    if not math.isfinite(value):
        raise RunsQueryError('invalid filter')
    return name, stat, op, value


def _split_metric(raw, trailing, message):
    if not isinstance(raw, str) or not raw.startswith('metric:'):
        raise RunsQueryError(message)
    rest = raw[len('metric:'):]
    parts = rest.rsplit(':', trailing)
    if len(parts) != trailing + 1:
        raise RunsQueryError(message)
    name = parts[0]
    if not name or len(name) > MAX_METRIC_NAME:
        raise RunsQueryError(message)
    return (name, *parts[1:])
