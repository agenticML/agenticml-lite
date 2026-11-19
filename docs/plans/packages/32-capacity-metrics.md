# Package 32: capacity metrics (CloudWatch metrics, dashboard, alarms)

Nov 18, 2026 · supervisor plan · coder: Sonnet · release 3.1

## Goal

Show what limits the hosted service as load grows, on one CloudWatch
dashboard, before package 33 adds autoscaling. Today only load-balancer,
RDS and two ECS numbers exist, with six alarms. Nothing shows requests per
minute, server-side latency percentiles, requests in progress, database
connections the server holds, or the package 31 usage totals.

This package adds:

1. In the server: per-minute request metrics and resource peaks, kept in
   memory, and an optional publisher (`--metrics cloudwatch`, env
   `AGENTICML_SERVER_METRICS`) that sends them to CloudWatch once a minute
   with `PutMetricData`. Default `off`: self-hosted servers send nothing.
2. In `hosted.yaml`: the publisher turned on, the task role allowed to
   publish to one namespace, and three alarms (RDS CPU credits, RDS
   connections, ECS memory).
3. In `bootstrap.yaml`: one CloudWatch dashboard, `agenticml-hosted`.

## Decisions (supervisor, recorded here)

- **`PutMetricData`, not log lines.** The hosted image already has boto3
  (the `s3` extra). The API supports true percentiles across tasks
  (`Values`/`Counts`), and IAM can limit it to one namespace. Metrics in log
  lines (Embedded Metric Format) would need no IAM, but AWS's docs do not
  confirm that the ECS `awslogs` driver path extracts them (AWS recommends
  its OTLP endpoint for ECS instead), and a wrong guess fails silently.
  Cost: about 43,000 calls a month per task, inside the 1 million free API
  calls.
- **12 metrics, no dimensions.** No task, workspace, user or route
  dimensions: each distinct value is a billed metric, and the first ten
  metrics in the account are free. Two tasks publish to the same metrics;
  CloudWatch adds counts and merges latency values, so the dashboard shows
  the whole service.
- **Dashboard in the bootstrap stack.** In `hosted.yaml` it would need
  `cloudwatch:PutDashboard` on the deploy role, which only a bootstrap
  re-apply grants; every hosted deploy would fail until the owner does
  that. In `bootstrap.yaml` the owner creates it with that same re-apply,
  whenever convenient, and hosted deploys never depend on it. The hosted
  names it refers to are fixed (`agenticml-hosted` cluster, service and
  database), and a test keeps them in step.
- **Three new alarms; nine in the hosted stack.** With the bootstrap's
  Lambda-errors alarm that is ten, the free tier.

## Metrics

Namespace `AgenticML/Server`, no dimensions, standard resolution, one
datum per metric per minute per task:

| Metric | Unit | Value | Dashboard statistic |
| --- | --- | --- | --- |
| `Requests` | Count | requests finished this minute (health checks excluded) | Sum |
| `Errors4xx` | Count | of those, status 400–499 | Sum |
| `Errors5xx` | Count | of those, status 500–599 | Sum |
| `Rejected` | Count | connections refused with the raw 503 at the 64-connection cap (`DashboardServer.process_request`) | Sum |
| `Latency` | Milliseconds | per-request times as `Values`/`Counts` (bucketed, see below) | p50, p95, p99 |
| `InFlightPeak` | Count | most requests in progress at once this minute | Maximum and Sum |
| `DbConnectionsPeak` | Count | most database connections this process held at once this minute | Maximum and Sum |
| `Accounts` | Count | package 31 total | Maximum |
| `ActiveUsers7d` | Count | package 31 `active_7d` | Maximum |
| `WorkspacesActive` | Count | package 31 `workspaces_active` | Maximum |
| `StoredBytes` | Bytes | package 31 `stored_bytes` | Maximum |
| `UploadBytesToday` | Bytes | package 31 `upload_bytes_today` | Maximum |

- Counts are published every minute, also when 0, so graphs and alarms
  have no gaps. `Latency` is published only when there were requests.
- The six usage totals come from `StatsRepo(db).totals()` every 15th flush
  (every 15 minutes), not every minute. Each task publishes the same
  values; the dashboard uses Maximum, so they are not double counted.
- Health checks (`/healthz`, the ingest route `health`) are not counted
  anywhere: the load balancer calls them every few seconds.

## Files to touch

- `src/agenticml/server/capacity.py` (new): `RequestMetrics` (in-memory
  counters) and `MetricsPublisher` (the loop).
- `src/agenticml/server/ingest.py`: `IngestHandler` records each request.
- `src/agenticml/ui/server.py`: `DashboardServer.process_request` counts
  the raw 503 rejects (through a hook that is a no-op for the local
  dashboard).
- `src/agenticml/server/db/postgres.py`, `src/agenticml/server/db/sqlite.py`:
  `pool_stats(reset=False)` with in-use and peak connection counts.
- `src/agenticml/server/app.py`: `IngestServer` owns a `RequestMetrics`;
  `serve(metrics=None)` starts and stops the publisher.
- `src/agenticml/cli.py`: `server serve --metrics MODE` and
  `AGENTICML_SERVER_METRICS`.
- `pyproject.toml`: a `cloudwatch = ["boto3>=1.34"]` extra; `hosted`
  includes it. `deploy/aws/requirements-hosted.txt` already pins boto3.
- `deploy/aws/cloudformation/hosted.yaml`: env var, task policy
  statement, three alarms.
- `deploy/aws/cloudformation/bootstrap.yaml`: the dashboard.
- `scripts/restore_drill/` (only if it copies the service's environment
  into the drill task): force `AGENTICML_SERVER_METRICS=off` there.
- Tests: `tests/test_server_metrics_publish.py` (new; `test_server_metrics.py`
  already exists for run metrics), `tests/test_server_cli.py`,
  `tests/test_deploy_cloudformation.py`, and any test the changes break.
- Docs (after the code review): `docs/SERVER.md`,
  `website/src/content/docs/server/install.md`,
  `website/src/content/docs/reference/configuration.md`,
  `deploy/aws/README.md`, `docs/plans/on-prem-roadmap.md`.

## Approach

### 1. `RequestMetrics` (`server/capacity.py`)

Thread-safe (one `threading.Lock`), cheap per request:

```python
class RequestMetrics:
    def request_started(self): ...          # in_flight += 1; peak = max(peak, in_flight)
    def request_finished(self, status, seconds): ...   # in_flight -= 1; counts; latency bucket
    def rejected(self): ...                 # raw 503 at the connection cap
    def take(self) -> dict: ...             # snapshot and reset: requests, errors_4xx, errors_5xx,
                                            # rejected, latency {bucket_ms: count}, in_flight_peak
```

- `status` may be `None` (no response sent: client went away, read
  timeout): count it in `Requests` but in neither error count.
- Latency buckets: round each duration in milliseconds to a log scale with
  about 10 steps per decade (for example `round(10 ** (round(log10(ms) * 10) / 10), 1)`
  for `ms >= 1`, and `1` below that), so a minute has well under 150
  distinct values. `take()` returns `{value: count}`.
- After `take()`, the in-flight peak restarts from the current in-flight
  count (requests still running count toward the next minute's peak).

### 2. Recording requests (`server/ingest.py`, `ui/server.py`)

- Override `IngestHandler.handle_one_request`: reset per-request fields,
  call `super()`, and in `finally` record the request only if it started.
- A request starts when its request line is read: override
  `IngestHandler.parse_request` to note the start time and call
  `request_started()` before the parent's parsing (so 400/414/431/505
  replies from `send_error` and header-deadline timeouts count). This
  leaves the idle keep-alive wait out.
- Latency is server time. The handler adds up the time spent reading the
  request (the headers in `parse_request`, each blocking body read in
  `IngestHandler._read_body` and `DashboardHandler._read_put_body`) and
  writing the response (`_ResponseWriter.write`, which every
  response goes through) in `_m_transfer`, and records
  `total - transfer` (never below 0). Counts and `InFlightPeak` are
  unchanged: a slow client still holds a request in flight.
- Capture the final status by overriding `send_response` (every response,
  including `send_error`, goes through it). `send_response_only` (the
  100-continue reply) is not a final status and is not captured.
- Health checks: `parse_request` looks at the raw request line first and,
  for a `GET` of `/healthz` (the ingest `health` route), neither counts it
  nor raises the in-flight count or its peak, so an idle task reads 0.
  Errors sent while parsing such a line still count as before. Never use the
  request path or query in any metric.
- `DashboardServer.process_request`: where it sends the raw 503 and
  closes, call `self.on_rejected()`. The base class defines it as a no-op;
  `IngestServer` calls `self.request_metrics.rejected()`.
- The local `agenticml ui` dashboard (which uses `DashboardHandler` but
  not `IngestHandler`) is unchanged.

### 3. Database connection peaks (`db/postgres.py`, `db/sqlite.py`)

Add to both database classes `pool_stats(reset=False) -> dict` with
`in_use` and `peak`, computed under the existing `self._lock`:

- Postgres counts every connection the process holds: transaction
  connections in use (`len(self._connections) - len(self._idle)`), session
  lock connections in use, and dedicated connections (the lifetime server
  lock). Track the session-lock and dedicated ones with a plain counter
  incremented when such a connection is opened and decremented when it is
  closed (do not read `BoundedSemaphore._value`).
- SQLite: transaction connections in use; no session locks.
- `peak` is the highest `in_use` since the last `reset=True` call; the
  reset sets it to the current `in_use`. Update the peak wherever
  `in_use` goes up, under the lock.

### 4. `MetricsPublisher` (`server/capacity.py`)

```python
NAMESPACE = 'AgenticML/Server'
FLUSH = 60          # seconds
USAGE_EVERY = 15    # flushes

class MetricsPublisher:
    def __init__(self, server, client=None, interval=FLUSH, final_client=None): ...
    def start(self): ...       # daemon thread "agenticml-metrics"
    def stop(self, timeout=None): ...   # set Event, flush once more, join; True when ended
METRIC_NAMES = (...)   # the 12 names; flush() and the deploy tests use them
```

- Like `StatsRefresher`, but on a fixed monotonic cadence: flush `k` is
  due at `start + k * interval`, so each window is about one interval
  however long a flush takes; a flush that overran skips to the next future
  slot. Every exception caught and logged with `logger.warning` (no traceback, at most
  once per 10 minutes for the same message), never stops the server.
- Each flush: `server.request_metrics.take()`,
  `server.db.pool_stats(reset=True)`, the usage totals every
  `USAGE_EVERY`th flush, then one `put_metric_data(Namespace=NAMESPACE,
  MetricData=[...])` call with `Timestamp` = the flush time (UTC).
  `Latency` uses `Values`/`Counts` lists (split into several data of the
  same metric if a minute ever has more than 150 distinct values).
- The clients: `boto3.client('cloudwatch', region_name=..., config=
  botocore.config.Config(connect_timeout=5, read_timeout=10,
  retries={'max_attempts': 2}))` for the regular flushes, and a second one
  (`connect_timeout=2, read_timeout=3, retries={'total_max_attempts': 1}`)
  for the final flush at shutdown, so `stop()` takes a few seconds even
  when CloudWatch is down. `serve()` creates both before binding anything
  (lazy import). botocore ignores `AWS_REGION`, so the region is passed
  from `AWS_REGION`, then `AWS_DEFAULT_REGION`; a missing region (or any
  other botocore error) becomes `ValueError("--metrics cloudwatch needs an
  AWS region: set AWS_REGION")`, so the CLI prints one line. Without boto3, `serve()` fails
  at startup with `ValueError("--metrics cloudwatch needs the [cloudwatch]
  extra: pip install 'agenticml-lite[cloudwatch]'")` (the pattern of
  `objectstore.py`). The region comes from the environment
  (`AWS_REGION`, set in the hosted task).
- Tests pass a fake `client` object that records the calls.

### 5. Wiring (`app.py`, `cli.py`)

- `IngestServer.__init__` creates `self.request_metrics = RequestMetrics()`.
- `serve(..., metrics=None)`: `None` or `'off'` publishes nothing;
  `'cloudwatch'` creates a `MetricsPublisher(server, ...)` and starts it after
  the startup lines are printed (`AgenticML server: ...` must stay the
  first stdout line: `tests/test_server_lifecycle.py` checks it), next to
  the `StatsRefresher`. Both are stopped by a `before_db_close` hook that
  `IngestServer.server_close(drain=..., before_db_close=...)` runs after the
  drain and the handler joins and before the database closes (in a
  `try/finally`, so the database and the server lock are always released):
  requests that finish during the drain are in the final flush, and the
  join of the publisher is bounded (10 s). Any other value: `ValueError`.
- `cli.py`: `server serve --metrics MODE` (help: `Publish capacity metrics:
  off or cloudwatch (default: AGENTICML_SERVER_METRICS or off)`), helper
  `_metrics_from_args(args)` modelled on `_legal_base_url_from_args`: the
  flag wins, a blank env counts as unset, values are stripped and
  lower-cased, an invalid value raises `ValueError("AGENTICML_SERVER_METRICS
  must be off or cloudwatch")` (the same message for the flag, so do not
  use argparse `choices`). Pass `metrics=` to `serve()` only when it is
  not `off`. `server up` does not get the flag.

### 6. `hosted.yaml`

- Container `Environment`: `AGENTICML_SERVER_METRICS` = `cloudwatch`.
- `TaskPolicy`: a statement allowing only `cloudwatch:PutMetricData` on
  `Resource: "*"` with `Condition: {StringEquals: {cloudwatch:namespace:
  AgenticML/Server}}`.
- Alarms (to the same alerts topic, `AlarmActions` and `OKActions`):
  - `agenticml-hosted-rds_cpu_credits`: `AWS/RDS` `CPUCreditBalance`,
    `DBInstanceIdentifier: !Ref DbInstance`, Minimum < 20, period 300,
    3 periods, `notBreaching`. (A t4g.micro earns 12 credits an hour and
    holds at most 288. RDS t4g runs in Unlimited mode, so below 20 credits
    the instance is not throttled but soon bursts on billed surplus
    credits. A new or restored instance starts at 0 credits, so this alarm
    stays in ALARM until it has earned 20, about two hours.)
  - `agenticml-hosted-rds_connections`: `AWS/RDS` `DatabaseConnections`,
    Maximum > 90, period 300, 1 period, `notBreaching` (about 80% of the
    roughly 112 connections `db.t4g.micro` allows; `docs/SERVER.md`).
  - `agenticml-hosted-ecs_memory`: `AWS/ECS` `MemoryUtilization`,
    `ClusterName: !Ref Cluster`, `ServiceName: !GetAtt Service.Name`,
    Maximum > 85, period 300, 2 periods, `notBreaching` (no data while
    the service is scaled to 0). Not `ECS/ContainerInsights`, which stays
    off.
- Stay under the 51,200-byte template limit (now about 33.5 KB).

### 7. `bootstrap.yaml`: dashboard

- `AWS::CloudWatch::Dashboard` named `agenticml-hosted`, body built with
  `!Sub` (region), widgets in a 24-column grid:
  1. Requests: `Requests`, `Errors4xx`, `Errors5xx`, `Rejected` (Sum, 1 min).
  2. Latency: `Latency` p50, p95, p99 (1 min).
  3. Load: `InFlightPeak` and `DbConnectionsPeak` (each Maximum and Sum), and
     `AWS/RDS` `DatabaseConnections` (Maximum) for `agenticml-hosted`.
  4. CPU: `AWS/ECS` `CPUUtilization` and `MemoryUtilization` (Average and
     Maximum; `ClusterName`/`ServiceName` `agenticml-hosted`), `AWS/RDS`
     `CPUUtilization`.
  5. RDS credits: `CPUCreditBalance` (Minimum, 5 min).
  6. Usage: `Accounts`, `ActiveUsers7d`, `WorkspacesActive` (Maximum,
     1 hour); `StoredBytes`, `UploadBytesToday` (Maximum, 1 hour).
- Add a dashboard URL to the bootstrap Outputs only if the bootstrap tests
  allow it without churn; otherwise the README gives the console path.
- Nothing in `hosted.yaml` imports or depends on the dashboard.

## Constraints and conventions

- Core stays stdlib; boto3 is imported only for `--metrics cloudwatch`.
- No raw paths, queries, slugs, logins or keys in any metric or log line.
- Recording must not slow requests: one lock, a few integer updates, no I/O
  on the request path. The publisher does all I/O on its own thread.
- Self-hosted default is `off`: nothing is sent anywhere unless asked.
- Container Insights stays disabled; `test_deploy_cloudformation.py`
  forbids `ECS/ContainerInsights` in the hosted template.
- Alarm names are literal strings `agenticml-hosted-*` (the tests key by
  name and the bootstrap role allows `alarm:agenticml-hosted-*`).

## Edge cases

- CloudWatch unreachable or throttling: the flush fails, a warning is
  logged at most every 10 minutes, the minute's numbers are dropped (not
  queued), the server keeps serving.
- Shutdown: after the drain and handler joins, `stop()` flushes the
  partial minute once with the quick client (a few seconds at most) and
  joins before the server closes the database.
- Two tasks: counts add up, latency values merge, peaks shown as Maximum
  (largest task) and Sum (both together); usage totals as Maximum.
- A minute with no requests: counts are 0, no `Latency` datum.
- A request that never got a response (client gone, read timeout) counts in
  `Requests` and `InFlightPeak` but not in an error count.
- The local `agenticml ui` server and test servers built with
  `make_server` keep working; recording is on, publishing is not.
- `serve()` with a fake server in tests (`SimpleNamespace`): only touch
  `request_metrics` or `db` when the publisher is enabled.

## Tests and checks

New `tests/test_server_metrics_publish.py`:

- `RequestMetrics`: counts and error classes, `None` status, latency
  bucketing (few distinct values for many durations; percentile within one
  bucket of the true value), in-flight peak and its carry-over after
  `take()`, thread safety (many threads, totals exact).
- Through a running server (`make_server` in a thread, as other tests do):
  a normal request, a 404, a 413 or other 4xx, a forced 5xx if one is easy
  to trigger, and `/healthz` → `take()` shows the right counts and no
  health request; a raw 503 at the connection cap counts `Rejected` (use
  the existing cap tests' technique, or call `process_request`'s reject
  path directly if simpler).
- `pool_stats`: on SQLite and on Postgres (`AGENTICML_TEST_DATABASE_URL`),
  in-use and peak move with open transactions and, on Postgres, session
  locks; `reset=True` restarts the peak.
- `MetricsPublisher` with a fake client: one flush produces the 7 request
  and resource data with the right names, units and values; `Latency` uses
  `Values`/`Counts` and is absent when there were no requests; usage totals
  appear on the first and every 15th flush only; a client exception is
  logged once as a warning and the next flush still runs; `stop()` flushes
  and returns promptly.
- `serve(metrics='cloudwatch')` without boto3 (monkeypatch the import to
  fail) raises the `[cloudwatch]` extra message; `serve(metrics='bogus')`
  raises `ValueError`.

`tests/test_server_cli.py`: flag and env matrix (unset, blank, `off`,
`cloudwatch`, upper case), invalid value from env and from the flag give
the same message and exit 1, `--help` names `AGENTICML_SERVER_METRICS`.

`tests/test_deploy_cloudformation.py`: the env var; the task policy
statement (only `cloudwatch:PutMetricData`, the namespace condition, and
the service set now including `cloudwatch`); the nine alarm names and
their summary tuples; the dashboard exists in `bootstrap.yaml`, its name
matches `agenticml-hosted`, its body parses as JSON (after `!Sub`
substitution of the region) and refers to the hosted cluster, service and
database names and to `AgenticML/Server`; hosted template still under
the size limit.

Commands:

- `testbed/.venv/bin/python -m pytest -q tests` (SQLite) and the same with
  `AGENTICML_TEST_DATABASE_URL` (Postgres; CI runs both).
- `cfn-lint deploy/aws/cloudformation/*.yaml` with the pinned
  `cfn-lint==1.60.0` in a throwaway venv in the scratchpad (CI's
  `deploy-check` job runs it).
- `cd testbed && .venv/bin/python -m pytest -q -m ui` (no dashboard change
  is planned; run it because `ui/server.py` changes).

## Docs

- `docs/SERVER.md`: a `--metrics` row in the configuration table, and a
  "Capacity metrics" section: what is measured, the metric list, that
  `off` is the default and nothing is sent, the IAM permission needed
  (`cloudwatch:PutMetricData` with the namespace condition) and the
  `[cloudwatch]` extra.
- `website/src/content/docs/server/install.md` and
  `website/src/content/docs/reference/configuration.md`: one row each.
- `deploy/aws/README.md`: the alarm list (nine), the dashboard (created by
  the bootstrap stack; re-apply the bootstrap stack to get it), the
  metrics, and the cost section (alarms count, about $1–4 a month for
  the custom metrics after the free ten).
- Roadmap: row 32 status; a Backlog owner step: re-apply the bootstrap
  stack to create the dashboard, then check after the next hosted deploy
  that the custom metrics arrive.

## Review notes (Nov 18, 2026)

Coded by Sonnet in two parallel parts (server, templates). Five review
lenses with two skeptics per finding: 19 confirmed (all low or medium),
none refuted, all fixed: the region and botocore errors give a clean CLI
message (botocore ignores `AWS_REGION`, so the region is passed
explicitly); the final flush runs after the drain and before the database
closes, with a quick single-attempt client and a bounded join; flushes keep
a fixed one-minute cadence; health checks never touch in-flight or the
peak; latency excludes request-header, request-body and response transfer
time; `capacity.METRIC_NAMES` ties the dashboard and IAM tests to the code;
the dashboard shows both Maximum and Sum for the peaks; the CPU credits
alarm comment explains RDS Unlimited mode and the post-restore ALARM; the
privacy policy mentions the metrics. A second review checked the shutdown
and request-accounting fixes.

## Done when

With `--metrics cloudwatch` the server sends the 12 metrics every minute
(proved with a fake client), the hosted template enables it with a
namespace-scoped permission and three new alarms, the bootstrap template
holds the dashboard, both suites (SQLite and Postgres), the UI tests and
cfn-lint pass, and the docs, roadmap and Backlog are updated.
