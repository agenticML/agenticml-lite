# Package 33: ECS autoscaling

Nov 19, 2026 · supervisor plan · coder: Sonnet · release 3.1

## Goal

Let the hosted service add server tasks when CPU is high and remove them
when load drops, without ever opening more database connections than the
database allows. Today the task count is fixed (1, or 2 by hand), and each
server process may open up to 89 Postgres connections (24 transaction +
48 run/object lock + 16 quota/lineage lock + 1 server lock) against about
112 on `db.t4g.micro`. That is only safe because real use is far below the
ceiling and there are at most two tasks.

This package adds:

1. **A per-process connection budget** for Postgres:
   `--db-connections N` / `AGENTICML_SERVER_DB_CONNECTIONS`. When it is
   unset, today's limits apply unchanged.
2. **Autoscaling in `hosted.yaml`**: a new `MaxTaskCount` parameter (1–4,
   default 1). Above 1, an Application Auto Scaling target and a target
   tracking policy on average service CPU (60%) scale between
   `DesiredCount` (now the minimum) and `MaxTaskCount`. The service's
   `DesiredCount` is then left to the autoscaler, and each task gets a
   connection budget that fits the database even during a deploy.
3. **The deploy role's new permissions** in `bootstrap.yaml`.
4. **A `max_task_count` input** in the deploy workflow.

## Decisions (supervisor, recorded here)

- **Off until the owner turns it on.** With `MaxTaskCount` 1 (the default),
  `hosted.yaml` creates no scaling resources and needs no new permission. So
  merging and deploying this package changes nothing at runtime, and does
  not need a bootstrap re-apply first. The owner turns it on with the
  deploy input after:
  - re-applying the bootstrap stack (also needed for the package 32
    dashboard);
  - the two-task rolling deploy check in the Backlog.
- **Fit the database by budget, per database class.** `hosted.yaml` gets a
  `DbInstanceClass` parameter (`db.t4g.micro` default, `small`, `medium`;
  changing it modifies the database in place with a few minutes of downtime,
  single-AZ). RDS sets `max_connections` to
  `LEAST(DBInstanceClassMemory / 9531392, 5000)`, where
  `DBInstanceClassMemory` is the memory minus the OS and RDS reserve (about
  70% for 1 GiB), and 5 slots are reserved for superusers. So
  `db.t4g.micro` gives roughly 75 usable, not the nominal 112. Usable
  connections for the application per class (conservative: about 70% of the
  nominal formula, minus 5 reserved, minus about 10 for operators, backups
  and monitoring): micro 64, small 144, medium 300. Each task gets
  `floor(usable / (2 × MaxTaskCount))`; the 2 covers rolling deploys
  (`MaximumPercent: 200` runs up to twice the tasks briefly):

  | class | 2 tasks | 3 tasks | 4 tasks |
  | --- | --- | --- | --- |
  | `db.t4g.micro` | 16 | not allowed | not allowed |
  | `db.t4g.small` | 36 | 24 | 18 |
  | `db.t4g.medium` | 75 | 50 | 37 |

  - A template Rule allows only `MaxTaskCount` 1 or 2 on `db.t4g.micro`: more
    tasks need `db.t4g.small` or larger.
  - The `RdsConnectionsAlarm` threshold follows the class (about 80% of the
    estimated `max_connections`): micro 60, small 130, medium 270.
  - Run `SHOW max_connections;` after a class change and check the estimate.
  - Real connection use follows load, not the task count. More tasks share
    the same load, so a smaller budget per task is consistent with that.
  - When a budget is exhausted, the request waits, then gets a 503 with
    `Retry-After: 5`.
  - The sync client already retries 503 up to 3 times, honouring
    `Retry-After` (`client.py`), and the sync loop backs off.
- **Autoscaling needs tasks.** `HasScaling` is true only when `MaxTaskCount`
  is above 1 and `DesiredCount` is above 0. So `DesiredCount` 0 always puts
  `DesiredCount: 0` on the service and removes the scaling resources and the
  budget variable, whatever `MaxTaskCount` is. (Target tracking does not
  scale out from zero tasks anyway.)
- **Scale on CPU.** `ECSServiceAverageCPUUtilization`:
  - The target is 60%.
  - Scale-out cooldown is 60 s, scale-in cooldown 300 s.
  - It is a free AWS metric; each task is one Python process, and CPU is
    its first limit.
  - Target tracking creates two CloudWatch alarms (about $0.20/month after
    the free ten).
- **No `RoleARN` on the scalable target.** Application Auto Scaling uses
  (and creates once) its ECS service-linked role
  `AWSServiceRoleForApplicationAutoScaling_ECSService`, so no `iam:PassRole`
  is needed.

## Files to touch

- `src/agenticml/server/db/postgres.py`: `connection_limits(budget)` and
  `configure_connection_budget(budget)`; idle cap tied to the transaction
  limit.
- `src/agenticml/server/app.py`: `make_server(..., db_connections=None)`
  applies the budget before opening the database; `serve(db_connections=None)`
  passes it on.
- `src/agenticml/cli.py`: `server serve --db-connections N` and
  `AGENTICML_SERVER_DB_CONNECTIONS`.
- `deploy/aws/cloudformation/hosted.yaml`:
  - parameters `MaxTaskCount` and `DbInstanceClass`, the micro rule,
    condition `HasScaling`, mappings `ConnectionBudget` and
    `ConnectionsAlarm`;
  - `ScalableTarget` and `CpuScalingPolicy`;
  - conditional `DesiredCount` on the service;
  - conditional env var;
  - comments.
- `deploy/aws/cloudformation/bootstrap.yaml`: CloudFormation role
  statements for Application Auto Scaling and the target-tracking alarms;
  the ECS autoscaling service-linked role.
- `.github/workflows/deploy.yml`: `max_task_count` input.
- Tests:
  - `tests/test_server_db_budget.py` (new);
  - `tests/test_server_cli.py`;
  - `tests/test_deploy_cloudformation.py`;
  - any test the changes break, for example
    `tests/test_server_postgres.py` and
    `tests/test_server_many_processes.py`, which touch the pool
    constants.
- Docs (after the code review):
  - `docs/SERVER.md`: "Database connections per process" and the
    configuration table;
  - `website/src/content/docs/server/install.md` and
    `website/src/content/docs/reference/configuration.md`;
  - `deploy/aws/README.md`: Operations and scaling, alarm 6, cost, GitHub
    variables text;
  - `docs/plans/on-prem-roadmap.md`.

## Approach

### 1. Connection budget (`db/postgres.py`)

```python
DEFAULT_LIMITS = (24, 48, 16)    # transaction, outer (run/object), inner (quota/lineage)
MIN_BUDGET = 8

def connection_limits(budget):
    """(transaction, outer, inner) for a per-process budget; None keeps the defaults.
    The budget includes the lifetime server-lock connection."""
```

- `None` gives `DEFAULT_LIMITS`.
- Otherwise `budget` must be an `int` in `MIN_BUDGET..1000`, else
  `ValueError`. Then:
  - `w = budget - 1`
  - `inner = max(round(w * 0.18), min(4, w - 3))`: the quota/lineage pool
    keeps at least 4 connections (3 at the smallest budget would starve
    it), so the two concurrent lineage receives (`lineage_receives`
    in `app.py`, which hold `lineage:*` session locks for up to 30 s) never
    block every `quota:*` lock;
  - `transaction = max(2, round(w * 0.27))`
  - `outer = w - inner - transaction`, which must be ≥ 1 (true for every
    budget ≥ 8)
  - so `transaction + outer + inner + 1 == budget`; the hosted budgets
    give 25 → (6, 14, 4), 16 → (4, 7, 4), 12 → (3, 4, 4).
- `configure_connection_budget(budget)`:
  - rebinds the module globals `TRANSACTION_CONNECTION_LIMIT`,
    `_outer_slots` and `_inner_slots` (new `BoundedSemaphore`s) to the
    computed limits;
  - `None` restores the defaults exactly;
  - a module lock makes it safe to call twice.
  - It is called once at startup, before any lock or transaction. Holders of
    the old semaphores release into the old ones, because `session_lock`
    keeps a local reference.
- The idle-pool cap (the literal `16` in the release path) becomes
  `min(16, TRANSACTION_CONNECTION_LIMIT)`, read per instance at
  `__init__`. Keep `SESSION_LOCK_LIMIT` as the default constant, which
  tests use.
- Nothing is parsed at import time.
- SQLite ignores the budget. If it is set with SQLite, log one INFO line
  "database connection budget applies to Postgres only" and continue.

### 2. Wiring (`app.py`, `cli.py`)

- `make_server(..., db_connections=None)` calls
  `postgres.configure_connection_budget(db_connections)` before
  `for_server_dir(...)`, but only on Postgres; on SQLite it configures
  `None`. It calls it every time, so `None` restores the defaults and one
  test cannot leak a budget into the next. It imports `postgres` lazily so
  that SQLite-only installs work, as today.
- **Registry reuse**: `for_server_dir` can return a cached open
  `PostgresDatabase` created under other limits. Each instance records its
  full `limits` tuple (transaction, outer, inner) at `__init__`. Before
  configuring anything, `make_server` looks up an already-open cached
  instance for the resolved URL (`db.open_postgres(url)`, which opens
  nothing). If one is open and its `limits` equal `connection_limits(
  db_connections)`, it does not touch the process-wide limits (so the lock
  semaphores are not swapped); if they differ it raises
  `ValueError("the database is already open in this process with other
  connection limits")` before swapping anything and leaves the cached
  instance open (it belongs to its other owner). After `for_server_dir` it
  repeats the full-tuple comparison as a backstop, restoring the previous
  limits (`limits_state` / `restore_limits_state`) on a mismatch.
- `serve(..., db_connections=None)` passes it to `make_server` only when it
  is not `None`, which keeps the fakes in existing tests working.
- `cli.py`:
  - `server serve --db-connections N` (help: `Most Postgres connections one
    server process opens (default: AGENTICML_SERVER_DB_CONNECTIONS or
    89)`);
  - a helper modelled on `_metrics_from_args`: the flag wins; a blank env
    counts as unset; a non-integer or out-of-range value raises
    `ValueError("AGENTICML_SERVER_DB_CONNECTIONS must be a whole number
    from 8 to 1000")`, with the same message for the flag;
  - pass `db_connections=` to `serve()` only when set.

### 3. `hosted.yaml`

- **Parameters**:
  - `MaxTaskCount`, `Type: String`, `AllowedValues: ["1", "2", "3", "4"]`,
    `Default: "1"`. Description: "Most tasks autoscaling may run; 1 turns
    autoscaling off. DesiredCount is the minimum."
  - `DbInstanceClass`, `Type: String`, `AllowedValues: [db.t4g.micro,
    db.t4g.small, db.t4g.medium]`, `Default: db.t4g.micro`; the database's
    `DBInstanceClass` is `!Ref DbInstanceClass`.
- **Rule** `MoreTasksNeedABiggerDatabase`: when `DbInstanceClass` is
  `db.t4g.micro`, `MaxTaskCount` must be `"1"` or `"2"`.
- **Conditions**: `HasTasks: !Not [!Equals [!Ref DesiredCount, 0]]` and
  `HasScaling: !And [!Not [!Equals [!Ref MaxTaskCount, "1"]], !Condition
  HasTasks]`.
- **Mappings** (top-level keys are the full class name, which
  CloudFormation allows and cfn-lint accepts; `Fn::Select`/`Fn::Split` are
  not allowed in a `FindInMap` key):
  - `ConnectionBudget`: class, then task count, every class for every
    `MaxTaskCount` value: `db.t4g.micro: {"1": "32", "2": "16", "3": "10",
    "4": "8"}`, `db.t4g.small: {"1": "72", "2": "36", "3": "24", "4":
    "18"}`, `db.t4g.medium: {"1": "150", "2": "75", "3": "50", "4": "37"}`
    (floor(usable / (2 * tasks))). CloudFormation resolves a `FindInMap`
    even in an `Fn::If` branch that is not taken, so every pair needs an
    entry. The "1" entries are never set as the variable (only while
    `HasScaling`), and the micro "3" and "4" exist only so the lookup
    resolves: the rule still refuses them.
  - `ConnectionsAlarm`: class, then `Threshold`: micro `"60"`, small
    `"130"`, medium `"270"`.
- **Service**: `DesiredCount: !If [HasScaling, !Ref AWS::NoValue, !Ref
  DesiredCount]`. While autoscaling is on (`MaxTaskCount` above 1 and
  `DesiredCount` above 0), a deploy leaves the running count alone;
  `DesiredCount` 0 always sets 0. The comment explains it.
- **Container env**:
  `!If [HasScaling, {Name: AGENTICML_SERVER_DB_CONNECTIONS, Value:
  !FindInMap [ConnectionBudget, !Ref DbInstanceClass, !Ref MaxTaskCount]},
  !Ref AWS::NoValue]`.
- **`RdsConnectionsAlarm`**: `Threshold: !FindInMap [ConnectionsAlarm, !Ref
  DbInstanceClass, Threshold]`; the comment says "about 80% of the estimated
  `max_connections` of the class".
- **`ScalableTarget`** (`AWS::ApplicationAutoScaling::ScalableTarget`):
  - `Condition: HasScaling`, `DependsOn: Service`;
  - `ServiceNamespace: ecs`, `ScalableDimension: ecs:service:DesiredCount`;
  - `ResourceId: !Sub service/${Cluster}/${Service.Name}`;
  - `MinCapacity: !Ref DesiredCount`, `MaxCapacity: !Ref MaxTaskCount`;
  - no `RoleARN`.
- **`CpuScalingPolicy`** (`AWS::ApplicationAutoScaling::ScalingPolicy`):
  - `Condition: HasScaling`;
  - `PolicyName: agenticml-hosted-cpu`, `PolicyType:
    TargetTrackingScaling`, `ScalingTargetId: !Ref ScalableTarget`;
  - `TargetTrackingScalingPolicyConfiguration`: `TargetValue: 60`,
    `PredefinedMetricSpecification: {PredefinedMetricType:
    ECSServiceAverageCPUUtilization}`, `ScaleOutCooldown: 60`,
    `ScaleInCooldown: 300`.
- **`HealthyHostsAlarm`**: its threshold stays `!Ref DesiredCount`, which is
  the minimum now. Rewrite its comment: fewer healthy targets than the
  minimum for 5 minutes. With autoscaling, extra tasks above the minimum
  never alarm.
- Keep `DesiredCount` `MaxValue: 2`. Its description says: 0 stops the
  service and removes the autoscaling resources and the connection budget,
  whatever `MaxTaskCount` is (`HasScaling` needs `HasTasks`).
- Stay under the 51,200-byte limit.

### 4. `bootstrap.yaml` (CloudFormation role)

- **New Sid `Scaling`** (Allow), `Resource: "*"`, with the same
  `aws:RequestedRegion` condition as the other `*` statements. Actions:
  - `application-autoscaling:RegisterScalableTarget`,
    `DeregisterScalableTarget`;
  - `DescribeScalableTargets`, `DescribeScalingPolicies`,
    `DescribeScalingActivities`, `DescribeScheduledActions`;
  - `PutScalingPolicy`, `DeleteScalingPolicy`;
  - `TagResource`, `UntagResource`, `ListTagsForResource`.
  
  List them explicitly; no wildcard.
- **New Sid `ScalingMetrics`** (Allow): `cloudwatch:GetMetricData` on
  `Resource: "*"` (the call has no resource ARN), with the same
  `aws:RequestedRegion` condition. The scaling policy handler reads the
  metric when it creates the policy.
- **New Sid `ScalingAlarms`** (Allow): `cloudwatch:PutMetricAlarm` and
  `cloudwatch:DeleteAlarms` on
  `arn:${AWS::Partition}:cloudwatch:${AWS::Region}:${AWS::AccountId}:alarm:TargetTracking-service/agenticml-hosted/agenticml-hosted-*`.
  - These are the alarms target tracking manages. Whether the caller needs
    them depends on the API path; granting them for these names only is
    harmless.
  - Keep the existing `Alarms` statement unchanged (tests read its single
    Resource).
- **`ServiceLinkedRoles`**: add `ecs.application-autoscaling.amazonaws.com`
  to the `iam:AWSServiceName` list.
- Do not touch `NotTheBootstrapRoles`. The deploy role's `HostedStack`
  statement already has `cloudformation:DescribeStacks` on the hosted stack,
  which the final check needs; it gets nothing for scaling.

### 5. `deploy.yml`

- **New input** `max_task_count`: `type: choice`,
  `options: ['keep', '1', '2', '3', '4']`, `default: 'keep'`. Description:
  "Most tasks autoscaling may run (1 turns it off; keep leaves the stack's
  value)".
- **Env** `MAX_TASK_COUNT: ${{ inputs.max_task_count }}`.
- **Run block**: add `MaxTaskCount="$MAX_TASK_COUNT"` to `overrides` only
  when it is non-empty and not `keep`. Push runs have no inputs, so they
  keep the stack's value, as `DesiredCount` does.
- **The final "check running count" step** reads the stack's `MaxTaskCount`
  and `DesiredCount` parameters (`aws cloudformation describe-stacks`
  `Parameters`, the resolved values) and decides:
  - **Autoscaling on** (`MaxTaskCount` above 1 and `DesiredCount` above 0):
    wait up to 30 × 10 s and pass when `runningCount == desiredCount` and
    `desiredCount >= DesiredCount` (the minimum). A scale-out during the
    check may take longer than a plain deploy.
  - **Otherwise** (`MaxTaskCount` 1, or `DesiredCount` 0): the old 12 × 10 s
    loop, and it passes only when `runningCount == desiredCount ==
    DesiredCount`. If the service `desiredCount` differs from the
    `DesiredCount` parameter (a scale-out left over from turning
    autoscaling off), it fails with `::error::` telling the owner to run
    `aws ecs update-service ... --desired-count <DesiredCount>` or redeploy
    with `desired_count`.
- **Rules**: no `${{` in run blocks; no new step containing
  `describe-stack-events`.

### 6. Tests

- **`tests/test_server_db_budget.py`**:
  - `connection_limits`:
    - `None` gives the defaults;
    - every budget from 8 to 200 sums to the budget with each pool ≥ 1 and
      transaction ≥ 2;
    - 7, 1001 and non-integers are refused.
  - `configure_connection_budget`:
    - sets and then restores the limits;
    - an old semaphore holder releases safely after a reconfigure.
  - `make_server` with a budget on Postgres (skip on SQLite):
    - the database's transaction limit and the lock semaphores match;
    - with a small budget, concurrent work beyond it times out with
      `DatabaseBusy`, never hangs (short timeouts via monkeypatch). These
      tests run their body (including `make_server`) in a worker thread and
      fail on `future.result(timeout=60)`; there is no pytest-timeout
      dependency;
    - `pool_stats` never exceeds the budget, and under load the real
      backends do too: the test counts `pg_stat_activity` rows from an admin
      connection and checks `len(db._connections) + db._lock_connections`
      and the transaction limit at the peak;
    - two held `lineage:*` locks (the two concurrent lineage receives) leave
      a `quota:*` lock acquirable promptly at budget 12; every budget from 8
      to 200 gives an inner pool of at least 4;
    - a second `make_server` with other limits on the same open database
      raises the `ValueError` above, keeps the first server's instance and
      limits, and the same limits return the same instance.
  - `serve(..., db_connections=25)` passes `db_connections` to
    `make_server` and omits it for `None` (`make_server` is monkeypatched
    with a recorder that raises, so nothing binds).
  - On SQLite, a budget logs the INFO line and changes nothing: the test
    first configures budget 25, then `make_server` on SQLite, with and
    without `db_connections=25`, must leave the defaults.
  - An autouse fixture restores the defaults after each test.
- **`tests/test_server_cli.py`**:
  - flag and env matrix (unset, blank, valid, invalid);
  - the same error for flag and env;
  - `--help` names `AGENTICML_SERVER_DB_CONNECTIONS`.
- **`tests/test_deploy_cloudformation.py`**:
  - `MaxTaskCount` parameter;
  - `HasScaling` condition;
  - the `ConnectionBudget` mapping, checked against the formula
    `floor(usable / (2 × n))` for every allowed (class, n), with usable 64 /
    144 / 300; each value is a valid budget (`connection_limits` from
    `agenticml.server.db.postgres`) and `2 × n × budget <= usable`;
  - the `DbInstanceClass` parameter, the micro rule (`MaxTaskCount` 1 or 2
    only) and the `ConnectionsAlarm` mapping (60 / 130 / 270) behind the
    `RdsConnectionsAlarm` threshold;
  - `HasScaling` condition structure (`And` with `HasTasks`) and a small
    evaluator: `DesiredCount` 0 with `MaxTaskCount` 3 gives no scaling and
    `DesiredCount` stays on the service;
  - the two scaling resources:
    - condition, `DependsOn`, `ResourceId` `!Sub`, min/max refs, policy
      config, no `RoleARN`;
    - the `needed` service map gains `AWS::ApplicationAutoScaling` →
      `application-autoscaling`;
  - conditional `DesiredCount`;
  - conditional env entry;
  - the bootstrap `Scaling`, `ScalingMetrics` and `ScalingAlarms`
    statements (explicit actions including `DescribeScheduledActions` and
    `GetMetricData`, the alarm ARN pattern matches the hosted cluster and
    service names);
  - the deploy role's `HostedStack` has `DescribeStacks`;
  - the service-linked role list;
  - the deploy workflow input and override, and the final check's logic
    (string checks in the run block: the stack parameters, 30 × 10 s only
    with autoscaling, the left-over scale-out error, values from env only,
    one `describe-stack-events` step).
  - Update every exact-set assertion the changes touch.
- **cfn-lint**: run as CI does: `cfn-lint==1.60.0`, from the scratchpad
  venv used in package 32.

## Constraints and conventions

- Default behaviour is unchanged everywhere:
  - no budget means 24/48/16;
  - `MaxTaskCount` 1 means no new AWS resources.
- Nothing new is sent anywhere. Nothing reads the request path.
- The deploy role keeps only `ecs:DescribeServices`; all new permissions
  go to the CloudFormation role.
- Alarm and policy names start with `agenticml-hosted`.

## Edge cases

- **Turning autoscaling off again** (`max_task_count=1`): the scaling
  resources are deleted and `DesiredCount` comes back on the service.
  **Two steps, because CloudFormation updates the service before it removes
  the scalable target**: the service would be set to `DesiredCount` while the
  target and policy still exist, and the autoscaler could scale it out
  again. So first lower the live target, either
  `aws application-autoscaling register-scalable-target ... --max-capacity
  <DesiredCount>` or suspend dynamic scale-out, wait for the scale-in, and
  only then deploy with `max_task_count=1`. The final deploy check fails
  with the `update-service` fix if a scale-out is left over.
- **`DesiredCount` 0**: `HasScaling` is false, so the service gets
  `DesiredCount: 0` and the scaling resources are removed, whatever
  `MaxTaskCount` is. Turning it back on is a deploy with `desired_count`
  above 0.
- **Deploy while scaled out**:
  - Application Auto Scaling does not scale in during a deployment;
  - the surge doubles the tasks briefly;
  - the budget covers that.
- **Deploy that toggles `MaxTaskCount`** (exception to the formula): while
  it runs, old tasks have the old budget and new ones the new budget (or
  none), so the surge is not covered by `floor(usable / (2n))`. Toggle at
  low load.
- **Budget is sized for the maximum**: with few tasks running, each still
  has the smaller budget. Watch `DbConnectionsPeak` and 503s on the package
  32 dashboard and the ALB `TargetResponseTime`, not only CPU.
- **A bigger database class**: `DbInstanceClass` is a change in place with a
  few minutes of downtime; run `SHOW max_connections;` afterwards and
  compare with the usable estimate.
- **Budget too small for a burst** (the inner pool is at least 4, so the two
  concurrent lineage receives cannot starve quota locks): requests wait for a slot (10 s for a
  transaction, 30 s for a lock), then answer 503 with `Retry-After`. Clients
  retry.
- **Re-applying `bootstrap.yaml` is required before the first deploy with
  `MaxTaskCount` above 1**; otherwise that deploy fails on AccessDenied and
  rolls back. The README and Backlog say so.

## Docs

- `docs/SERVER.md` "Database connections per process":
  - the budget and its split, the default;
  - why hosted sets it (formula);
  - Postgres only.
  
  Fix the "at most 8 uploads hold locks" wording: the body limit does not
  bound lock holding. Add a row to the configuration table.
- Website `install.md` and `configuration.md`: one row each.
- `deploy/aws/README.md`:
  - "Operations and scaling" rewritten:
    - min = `desired_count`, max = `max_task_count`;
    - how to turn autoscaling on, in order: re-apply bootstrap, two-task
      check, deploy with `max_task_count`;
    - how to watch it on the package 32 dashboard;
    - turning it off;
    - `update-service` caveats.
  - Alarm 6 wording (the minimum).
  - Cost: about $0.025 per extra task-hour while it runs; the two
    target-tracking alarms about $0.20/month.
  - The GitHub variables paragraph mentioning `desired_count`.
- **Roadmap**:
  - row 33 status;
  - merge the bootstrap re-apply into the package 32 Backlog owner step;
  - add an owner step: enable autoscaling after the two-task check, run a
    short load test (several sync clients, for example
    `scripts/hosted_check.py make-run` in parallel, or `bench_server.py`
    on Fargate as in the README), watch it scale out and back in, and run
    `SHOW max_connections;` once.

## Review notes (Nov 19, 2026)

Coded by Sonnet in two parallel parts (server budget; templates and
workflow). Four review lenses with two skeptics per finding: 16 confirmed,
2 refuted. The design changed on three points:

- RDS sets `max_connections` from memory after the OS reserve, so
  `db.t4g.micro` gives roughly 75 usable connections, not 112. Budgets now
  come from a usable estimate per instance class (new `DbInstanceClass`
  parameter: micro 64, small 144, medium 300), micro allows at most 2
  tasks (a template rule), and the RDS connections alarm threshold follows
  the class. The owner checks `SHOW max_connections;` before turning
  autoscaling on (Backlog).
- Small budgets starved upload quota locks behind lineage pushes: the
  quota/lineage pool keeps at least 4 slots.
- `desired_count=0` did not stop an autoscaled service: autoscaling now
  needs a minimum above 0.

Also fixed: two deploy-role permissions (`DescribeScheduledActions`,
`cloudwatch:GetMetricData`); the deploy workflow's final check waits up to
5 minutes while autoscaling is on and fails on a scale-out left over after
turning it off; the two-step turn-off and the toggle-deploy exception are
documented; a database already open in the process under other limits is
refused; stronger tests (real backend counts, serve wiring, time-bounded
"never hangs" tests). A second review of the fixes found two more low
issues (the refusal compared only the transaction limit; the time bound
could still hang pytest at exit), both fixed.

Deploy fix (Nov 19, 2026): the first deploy of dev 470a8e7 failed at
CreateChangeSet with `Template error: Unable to get mapping for
ConnectionBudget::db.t4g.micro::1`, because the mapping had no entry for
micro with `MaxTaskCount` 1 and `FindInMap` is resolved in the untaken
`Fn::If` branch too. Nothing in the stack changed. The fix fills every
class x task count pair and adds a generic test that every `FindInMap` in
both templates resolves for every allowed parameter value.

## Done when

The budget works and is tested on Postgres; `hosted.yaml` scales between
the minimum and `MaxTaskCount` when it is above 1 and is unchanged at 1;
the deploy role can create the scaling resources after a bootstrap
re-apply; the workflow has the input; both suites, the UI tests and
cfn-lint pass; docs, roadmap and Backlog are updated.
