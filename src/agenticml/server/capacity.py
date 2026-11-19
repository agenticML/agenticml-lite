"""Capacity metrics: per-minute request counters and an optional CloudWatch publisher.

No path, query, slug, login or key ever enters a metric or a log line.
"""
from datetime import datetime, timezone
import logging
import math
import os
import threading
import time

from .db.stats import StatsRepo

logger = logging.getLogger('agenticml.server.capacity')
NAMESPACE = 'AgenticML/Server'
FLUSH = 60
USAGE_EVERY = 15
WARN_EVERY = 600
MAX_VALUES = 150
EXTRA_MESSAGE = "--metrics cloudwatch needs the [cloudwatch] extra: pip install 'agenticml-lite[cloudwatch]'"
MODE_MESSAGE = 'AGENTICML_SERVER_METRICS must be off or cloudwatch'
REGION_MESSAGE = '--metrics cloudwatch needs an AWS region: set AWS_REGION'
FINAL_JOIN = 10
METRIC_NAMES = ('Requests', 'Errors4xx', 'Errors5xx', 'Rejected', 'Latency', 'InFlightPeak',
                'DbConnectionsPeak', 'Accounts', 'ActiveUsers7d', 'WorkspacesActive',
                'StoredBytes', 'UploadBytesToday')


def _bucket(seconds):
    ms = seconds * 1000
    if ms < 1:
        return 1
    return round(10 ** (round(math.log10(ms) * 10) / 10), 1)


class RequestMetrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._in_flight = 0
        self._reset()

    def _reset(self):
        self._requests = self._errors_4xx = self._errors_5xx = self._rejected = 0
        self._latency = {}
        self._peak = self._in_flight

    def request_started(self):
        with self._lock:
            self._in_flight += 1
            if self._in_flight > self._peak:
                self._peak = self._in_flight

    def request_finished(self, status, seconds, count=True):
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
            if not count:
                return
            self._requests += 1
            if status is not None:
                if 400 <= status < 500:
                    self._errors_4xx += 1
                elif 500 <= status < 600:
                    self._errors_5xx += 1
            bucket = _bucket(seconds)
            self._latency[bucket] = self._latency.get(bucket, 0) + 1

    def rejected(self):
        with self._lock:
            self._rejected += 1

    def take(self):
        with self._lock:
            taken = {'requests': self._requests, 'errors_4xx': self._errors_4xx,
                     'errors_5xx': self._errors_5xx, 'rejected': self._rejected,
                     'latency': self._latency, 'in_flight_peak': self._peak}
            self._reset()
        return taken


def cloudwatch_client(final=False):
    """A CloudWatch client; ``final`` is the quick one for the last flush at shutdown."""
    try:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, NoRegionError
    except ImportError as exc:
        raise ValueError(EXTRA_MESSAGE) from exc
    if final:
        config = Config(connect_timeout=2, read_timeout=3, retries={'total_max_attempts': 1})
    else:
        config = Config(connect_timeout=5, read_timeout=10, retries={'max_attempts': 2})
    # botocore reads AWS_DEFAULT_REGION, not AWS_REGION, so pass the region on.
    region = os.environ.get('AWS_REGION') or os.environ.get('AWS_DEFAULT_REGION') or None
    try:
        return boto3.client('cloudwatch', region_name=region, config=config)
    except NoRegionError as exc:
        raise ValueError(REGION_MESSAGE) from exc
    except BotoCoreError as exc:
        raise ValueError(f'--metrics cloudwatch: {exc}') from exc


class MetricsPublisher:
    def __init__(self, server, client=None, interval=FLUSH, final_client=None):
        self.server = server
        self.client = client
        self.final_client = final_client
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self._flushes = 0
        self._warned = {}

    def start(self):
        if self._thread is None:
            if self.client is None:
                self.client = cloudwatch_client()
                self.final_client = cloudwatch_client(final=True)
            self._thread = threading.Thread(target=self._run, name='agenticml-metrics', daemon=True)
            self._thread.start()

    def stop(self, timeout=None):
        """Flush the partial minute once (quick client), then join; True when the thread ended."""
        self._stop.set()
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self):
        # A fixed cadence: each window is about one interval however long a
        # flush takes; a flush that overran skips to the next future slot.
        start = time.monotonic()
        slot = 1
        while not self._stop.wait(max(0.0, start + slot * self.interval - time.monotonic())):
            self._flush_safely()
            slot = max(slot + 1, int((time.monotonic() - start) // self.interval) + 1)
        self._flush_safely(self.final_client)

    def _warn(self, what, exc):
        message = f'{what}: {str(exc) or type(exc).__name__}'
        now = time.monotonic()
        last = self._warned.get(message)
        if last is None or now - last >= WARN_EVERY:
            self._warned[message] = now
            if len(self._warned) > 20:
                self._warned = {message: now}
            logger.warning(message)

    def _flush_safely(self, client=None):
        try:
            self.flush(client)
        except Exception as exc:
            self._warn('could not publish capacity metrics', exc)

    def _usage(self):
        try:
            totals = StatsRepo(self.server.db).totals()
        except Exception as exc:
            self._warn('could not read usage totals', exc)
            return []
        usage = {'Accounts': (totals['accounts'], 'Count'),
                 'ActiveUsers7d': (totals['active_7d'], 'Count'),
                 'WorkspacesActive': (totals['workspaces_active'], 'Count'),
                 'StoredBytes': (totals['stored_bytes'], 'Bytes'),
                 'UploadBytesToday': (totals['upload_bytes_today'], 'Bytes')}
        return [(name, *usage[name]) for name in METRIC_NAMES if name in usage]

    def flush(self, client=None):
        stamp = datetime.now(timezone.utc)
        taken = self.server.request_metrics.take()
        pool = self.server.db.pool_stats(reset=True)
        values = {'Requests': taken['requests'], 'Errors4xx': taken['errors_4xx'],
                  'Errors5xx': taken['errors_5xx'], 'Rejected': taken['rejected'],
                  'InFlightPeak': taken['in_flight_peak'], 'DbConnectionsPeak': pool['peak']}
        rows = [(name, values[name], 'Count') for name in METRIC_NAMES if name in values]
        if self._flushes % USAGE_EVERY == 0:
            rows += self._usage()
        self._flushes += 1
        data = [{'MetricName': name, 'Timestamp': stamp, 'Value': value, 'Unit': unit}
                for name, value, unit in rows]
        buckets = sorted(taken['latency'].items())
        for start in range(0, len(buckets), MAX_VALUES):
            part = buckets[start:start + MAX_VALUES]
            data.append({'MetricName': 'Latency', 'Timestamp': stamp, 'Unit': 'Milliseconds',
                         'Values': [value for value, _ in part],
                         'Counts': [float(count) for _, count in part]})
        (client or self.client).put_metric_data(Namespace=NAMESPACE, MetricData=data)
