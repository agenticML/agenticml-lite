# Fix: the hosted dashboard breaks after the weekly database password rotation

Maintenance fix, found on Nov 27, 2026 when the owner reported that the
dashboard at app.agenticml.xyz "loads slowly" after signing in.

## What happened (root cause)

1. The RDS instance uses `ManageMasterUserPassword: true`. RDS rotates that
   managed secret automatically **every 7 days** (last rotation Nov 26, 2026
   12:12 UTC; next Dec 3). Nothing in the stack turned this on explicitly, and
   the README's note that rotation "requires forcing a new ECS task deployment"
   assumed rotation was manual.
2. ECS injects `PGPASSWORD` from the secret **only when a task starts**. The
   running task had started on Nov 22, so it still held the old password.
3. Connections that are already open survive a password change. The task's pool
   kept its 2 old connections (`DbConnectionsPeak` stayed at 2), so light
   traffic kept working.
4. The dashboard loads `app.js`, which statically imports `charts.js`,
   `code.js` and `codediff.js`. The browser fetches those three in parallel, and
   each request checks the session in Postgres. The third concurrent request
   needs a **new** connection, and opening it failed:
   `FATAL: password authentication failed for user "agenticml"` (RDS log, at
   exactly 06:07:17, 06:07:28, 06:08:10 and 06:08:35 UTC, the times of the four
   503s in the ALB access log).
5. `PostgresDatabase._new_connection` maps the failure to
   `DatabaseError('could not connect to Postgres')` with the driver message
   dropped. `WebAuth` catches it (`webauth.py`, the
   `except (KeyStoreUnavailable, DatabaseError)` branch in the dashboard
   dispatch) and answers `503 {"error": "key store unavailable"}` **without
   logging anything**.
6. One failed module import makes the whole `app.js` module fail, so the
   dashboard never makes an API call and never renders. To the owner it looked
   like a page that would not finish loading.

Why it was invisible: the server logs nothing for this 503. RDS Postgres logs
are not exported to CloudWatch (`EnabledCloudwatchLogsExports` is null), so the
`FATAL` lines exist only in the RDS console log files.

Immediate mitigation (done Nov 27, 2026, owner's go): `aws ecs update-service
--force-new-deployment` gave the service a task with the current password. It
would break again after the Dec 3 rotation without this fix.

## Goal

- A server whose database password was rotated keeps working without a
  restart. When opening a new connection fails with a password error, the
  server fetches the current password from Secrets Manager and retries once.
- Database connection failures and the dashboard's "unavailable" 503s are
  logged, with enough detail to find the cause and no secrets.
- RDS Postgres logs reach CloudWatch Logs with 30-day retention.

## Files to touch

- `src/agenticml/server/db/postgres.py`: password refresh, connect-failure log.
- `src/agenticml/server/webauth.py`: log the 503 branch.
- `src/agenticml/server/ingest.py`: log the sync-side "key store unavailable" /
  "object store unavailable" 503 branches the same way (only where an exception
  is caught; do not change status codes or bodies).
- `deploy/aws/cloudformation/hosted.yaml`: new env var, task-role permission,
  RDS log export, RDS log group.
- `deploy/aws/cloudformation/bootstrap.yaml`: let the CloudFormation role
  manage the RDS log group.
- `deploy/aws/README.md`: replace the manual-rotation note; document the env
  var, the log export, and the bootstrap re-apply order.
- `docs/SERVER.md` (or wherever `AGENTICML_SERVER_DATABASE_URL` is documented;
  grep first): document `AGENTICML_SERVER_DATABASE_PASSWORD_SECRET`.
- Tests: `tests/test_server_postgres.py` (or a new
  `tests/test_server_db_password_refresh.py`), `tests/test_deploy_cloudformation.py`,
  and a webauth logging test next to the existing webauth/keys tests.
- `docs/plans/on-prem-roadmap.md` (Maintenance fixes row and two Backlog rows):
  supervisor.

## Approach

### 1. Password refresh in `PostgresDatabase` (`db/postgres.py`)

1. New optional environment variable
   `AGENTICML_SERVER_DATABASE_PASSWORD_SECRET`: a Secrets Manager secret ARN or
   name whose value is JSON with a `password` key (the RDS-managed secret
   format). Read it once in `PostgresDatabase.__init__` (store on the instance;
   empty string means unset).
2. Keep a per-instance `self._password = None` (None: let libpq use the URL /
   `PGPASSWORD` as today) guarded by a lock.
3. In `_new_connection`, pass `password=self._password` to `psycopg.connect`
   only when `self._password` is not None (a keyword argument, never put into
   the URL or conninfo string that might be logged).
4. If `psycopg.connect` raises and the error is a password failure **and** the
   secret is configured:
   - Detect the failure by `exc.sqlstate == '28P01'` (invalid_password); if
     psycopg leaves `sqlstate` None on connect errors, fall back to the server
     message containing `password authentication failed`. Check the real
     behaviour against a Postgres container (see Tests) and report which one
     works.
   - Fetch the secret with `boto3.client('secretsmanager').get_secret_value`
     (region from the default chain / `AWS_REGION`), parse JSON, take
     `password`. Import boto3 lazily inside the fetch; if boto3 is missing or the
     fetch/parse fails, log a warning (no secret value, no ARN beyond what is
     already in the task definition) and fall through to today's error.
   - Refetch at most once per 30 seconds per instance (store the time of the
     last fetch) so a wrong secret or a real outage cannot hammer Secrets
     Manager. Concurrent threads that fail together share one fetch (hold the
     lock while fetching, recheck the timestamp after acquiring it).
   - If the fetched password differs from the one just tried, store it and retry
     the connect **once**; otherwise give up.
   - On success after a refresh, log at INFO: `database password refreshed from
     the secret`.
5. Applies to both pool connections and lock connections (`lock=True`), since
   both go through `_new_connection`. Leave `exists()` alone (a one-off probe
   used by the CLI).
6. Keep `_raise_plain(DatabaseError('could not connect to Postgres'))` as the
   raised error: callers must not see driver text.

### 2. Logging

1. `_new_connection`: when a connect fails (after any refresh retry), log a
   WARNING `could not connect to Postgres: <sqlstate or class name>: <message>`
   where the message is `exc.diag.message_primary` if available, else the class
   name. Never log the URL, conninfo, or password. (The primary message for
   this incident is `password authentication failed for user "agenticml"`,
   which is safe.) To stop a flood during an outage, log at most once per 10
   seconds per instance and count the suppressed ones into the next line
   (`(N more since last log)`).
2. `webauth.py` dashboard dispatch branch
   `except (KeyStoreUnavailable, DatabaseError)`: log a WARNING
   `dashboard request failed: database unavailable (<ExceptionClass>) <METHOD> <path without query>`.
   Same 10-second rate limit (module-level helper is fine; share it with
   ingest.py if natural). Do not change the response.
3. `ingest.py`: the same for the sync branches that answer
   `key store unavailable` / `object store unavailable` after catching an
   exception.

### 3. CloudFormation (`hosted.yaml`, `bootstrap.yaml`)

1. Container environment: add
   `AGENTICML_SERVER_DATABASE_PASSWORD_SECRET: !GetAtt DbInstance.MasterUserSecret.SecretArn`.
   Keep `PGPASSWORD` from the secret for startup.
2. `TaskPolicy`: allow `secretsmanager:GetSecretValue` on
   `!GetAtt DbInstance.MasterUserSecret.SecretArn` for the task role (the
   execution role already has it; the running app uses the task role). The
   secret uses the AWS-managed key, so no KMS statement is needed; say so in a
   comment.
3. RDS log export: on `DbInstance`, add
   `EnableCloudwatchLogsExports: [postgresql]` and
   `DependsOn: RdsLogGroup`. Add `RdsLogGroup` (`AWS::Logs::LogGroup`,
   `LogGroupName: /aws/rds/instance/agenticml-hosted/postgresql`,
   `RetentionInDays: 30`). Enabling the export does not restart the instance.
4. `bootstrap.yaml` `LogGroups` statement: add the resource
   `arn:...:log-group:/aws/rds/instance/agenticml-hosted/*` next to the existing
   `/ecs/agenticml-hosted*` one. Check whether the CloudFormation role also
   needs `rds:ModifyDBInstance` for the export (it already updates the
   instance; confirm the existing statement covers it) and report.
5. cfn-lint both templates clean.

### 4. Docs (`deploy/aws/README.md`, server docs)

- Replace "Rotating the RDS-managed database password ... requires forcing a
  new ECS task deployment" with: RDS rotates the managed password every 7 days;
  the server re-reads it from the secret when a new connection is refused, so no
  restart is needed; if `AGENTICML_SERVER_DATABASE_PASSWORD_SECRET` is unset
  (older stacks), force a new deployment after each rotation.
- Document the env var (format: secret ARN or name whose JSON has `password`;
  needs `secretsmanager:GetSecretValue` and boto3, which `[hosted]` includes).
- Note the RDS log group and that the bootstrap stack must be re-applied
  **before** the hosted deploy that adds it.

## Constraints and conventions

- No secret values, URLs with credentials, or conninfo in logs or exceptions.
- Do not change HTTP status codes or response bodies.
- boto3 stays optional: the server must start and work without it when the new
  variable is unset; tests that need boto3 skip when it is missing.
- Follow existing patterns: lazy boto3 import like `capacity.py` /
  `objectstore.py`; module `logger`.
- Code is MIT open source (agenticml-lite).

## Edge cases

- Variable unset: behaviour identical to today (plus the new log lines).
- Secret fetch fails (no permission, network, bad JSON, no `password` key):
  warning logged, original connect error raised.
- Fetched password equals the one that failed: no retry (real outage or wrong
  user).
- Many threads fail at once: one fetch, all retry with the new password.
- A refresh during startup (`__init__` initial connection) works the same way.
- Lock connections (`lock=True`) use the refreshed password too.

## Tests and checks

- Unit tests with a fake `psycopg.connect` (monkeypatch `self._psycopg` or the
  module) and a fake secret fetcher (inject a seam, e.g. a
  `_fetch_secret_password` method tests can replace; no moto needed):
  first connect raises a 28P01 error, refresh returns a new password, second
  connect succeeds with `password=` passed; fetch failure → `DatabaseError`;
  same password → no retry; rate limit → second failure within 30 s does not
  refetch; variable unset → no fetch.
- An integration test against real Postgres when
  `AGENTICML_TEST_DATABASE_URL` is set: create a throwaway role, open a
  `PostgresDatabase` with that role's password, `ALTER ROLE ... PASSWORD` to a
  new one, make the fake fetcher return the new password, and check a new
  transaction that needs a fresh connection succeeds (drain the idle pool or use
  more concurrent transactions than idle connections). This also confirms the
  28P01 detection.
- Logging tests (caplog): the webauth 503 branch logs one WARNING without the
  cookie or query string; connect-failure log has no password/URL; rate limit
  suppresses and counts.
- `tests/test_deploy_cloudformation.py`: the env var, task-policy statement,
  `EnableCloudwatchLogsExports`, the RDS log group with 30-day retention, and the
  bootstrap log-group resource.
- Run: `testbed/.venv/bin/python -m pytest -q tests` from the repo root (SQLite),
  and the Postgres run with `AGENTICML_TEST_DATABASE_URL` pointing at a
  throwaway `postgres:16` container on a free port; cfn-lint both templates.
  No dashboard UI change, so the UI suite is not required.

## Done when

- The password-refresh, logging and CloudFormation tests pass on SQLite and
  Postgres; the full suites pass; cfn-lint is clean.
- After merge and deploy (owner: bootstrap re-apply first, then approve the
  hosted deploy), `/aws/rds/instance/agenticml-hosted/postgresql` exists with
  30-day retention and the next rotation (Dec 3, 2026) causes no 503s; check
  the ALB 5xx count and the server log for `database password refreshed`.

## Follow-ups (Backlog, not in this fix)

- Static assets are served uncompressed with `no-cache`/`no-store`, so every
  dashboard load re-downloads or revalidates about 450 KB at one round trip
  each: add gzip and long-lived caching with versioned URLs.
- One failed module import blanks the whole dashboard: show an error with a
  retry instead of a page that never finishes.
