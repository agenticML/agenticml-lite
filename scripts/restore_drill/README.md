# Restore drill and real-EFS benchmark (operator runbook)

Package 24.13, AWS roadmap Phase 6. Restores the hosted service's three stores
into isolated drill resources, proves the restored service works (6.1), measures
real EFS with `scripts/bench_server.py` (6.2), then removes every drill
resource. The owner runs it with an admin profile. It is not shipped in the
wheel and lives outside `deploy/aws/`, so merging it queues no hosted deploy.
The short version of the policy is in the "Restore drill and Real EFS
benchmark" section of `deploy/aws/README.md`.

## What the tooling does

| File | Purpose |
|---|---|
| `common.sh` | Source it from **bash**. `drill_init` loads the state file, `drill_discover` reads every production fact the drill needs, `drill_set` saves a value. |
| `mk_td.py` | Builds the `inspect`, `dbcheck`, `server` and `bench` task definitions from the live production one and refuses unsafe output. |
| `drill-run.sh` | The drill server's entrypoint: guards, then `check` or `sync` mode. |
| `check_db.py` | Read-only SQL counts (the `dbcheck` task). |
| `copy_versions_at.py` | Copies the S3 object versions current at time T into the drill bucket, with an integrity check. |
| `run_task.sh` | Runs one task in the drill cluster, waits, prints timings, saves the log. |

**No account-specific identifier is stored in the repository.** Every ID is read
at run time from the `agenticml-hosted` stack outputs and resources, the live
task definition and describe calls. Production facts and the drill's own IDs
are kept in `$DRILL_STATE_DIR/state.env` (default `./restore-drill-state`,
git-ignored). Do not paste its contents into the repo, a PR or the public
status page.

## Safety model

- **No production writes.** The only production access is read-only: describe
  and list calls, `list-object-versions` and version reads on the production
  bucket, a read-only SQL transaction (optional), and the production secret read
  that ECS does at every task start. `copy_versions_at.py` refuses any write
  whose `--bucket` is not the drill bucket.
- **No new IAM.** A point-in-time restore keeps the master password as of the
  restore time and creates no managed secret, so drill tasks reuse the existing
  execution role. The drill server reuses the existing task role through a
  bucket policy on the drill bucket (that role can also write to the production
  `hosted/*` prefix, which is why the guards below exist; a dedicated role is
  the alternative and is an IAM create that needs the owner's approval).
- **Guards.** `mk_td.py` refuses to write a definition that names the production
  bucket, filesystem, access point, database host (except `dbcheck --prod`),
  hostname, target group or load balancer DNS name, an ECS Exec flag, or any
  port mapping; the bench definition gets no task role and no environment
  variables or secrets. `drill-run.sh` is the entrypoint, so a run-task command
  override cannot skip its checks: it exits 3 before starting the server unless
  the database host is `agenticml-restore-drill.*`, the objects URL names a
  bucket starting with `agenticml-restore-drill-`, and the public URL is
  loopback.
- **Nothing is reachable.** Tasks get a public IPv4 only for egress (the stack
  has no NAT), behind the production task security group, which has no new
  inbound rule. The drill is never in the target group or in DNS.
- **Everything is named `agenticml-restore-drill*` and tagged
  `Project=agenticml-restore-drill`**, so the cleanup and the final listing can
  prove nothing remains.
- **Abort on any failed gate**, clean up, and record the failure in the roadmap
  Backlog.
- **Deadline.** Drill tasks must start before the next rotation of the database
  secret (ECS reads it at task start); check it in step 0.

## Before you start

The owner approves: the billed drill resources (an RDS `db.t4g.micro`, an EFS
filesystem with a mount target, an S3 bucket, Fargate tasks up to 4 vCPU / 8 GB
for the benchmark; well under $1, resources living about 3 to 5 hours), option
A or B in step 1, and the use of the existing task role.

Run every block in `bash`. Each block starts by loading the state file:

```bash
export AWS_PROFILE=<owner admin profile> AWS_REGION=<region of the stack>
export DRILL_STATE_DIR="$PWD/restore-drill-state"   # git-ignored
D=scripts/restore_drill
. $D/common.sh && drill_init
```

## 0. Preflight (read-only)

```bash
. $D/common.sh && drill_init && drill_discover       # writes $DRILL_STATE_FILE and prod-td.json
aws ecs describe-services --cluster "$PROD_CLUSTER" --services "$PROD_SERVICE" \
  --query 'services[0].[taskDefinition,runningCount,desiredCount]' --output text
aws rds describe-db-instances --db-instance-identifier "$PROD_DB" \
  --query 'DBInstances[0].[DBInstanceStatus,DeletionProtection,LatestRestorableTime]' --output text
aws secretsmanager describe-secret --secret-id "$SECRET_ARN" --query '[LastRotatedDate,NextRotationDate]' --output text
aws resourcegroupstaggingapi get-resources --tag-filters Key=Project,Values=agenticml-restore-drill \
  --query 'ResourceTagMappingList[].ResourceARN'                          # expect []
aws rds describe-db-instances --query 'DBInstances[].DBInstanceIdentifier' --output text   # only production
aws efs describe-file-systems --query 'FileSystems[].FileSystemId' --output text            # only production
aws s3api list-object-versions --bucket "$PROD_B" --prefix hosted/ --output json > "$DRILL_STATE_DIR/out/s3-versions-before.json"
python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); print(len(d.get("Versions",[])), len(d.get("DeleteMarkers",[])))' \
  "$DRILL_STATE_DIR/out/s3-versions-before.json"
git show "$(python3 -I -c 'import json,sys; i=json.load(open(sys.argv[1]))["containerDefinitions"][0]["image"]; print(i.rsplit(":",1)[1].split("-")[0])' "$DRILL_STATE_DIR/prod-td.json")":scripts/bench_server.py | sha256sum
```

Check: the service runs its task definition with 1 of 1 tasks; the production
database is `available` with deletion protection on; the tag query and the
database and filesystem lists show no drill resources; note the secret's next
rotation (every drill task must start before it) and the S3 counts; the last line is the
sha256 of the benchmark script at the deployed commit (`mk_td.py bench`
embeds the same script and checks it again inside the container).

## 1. Recovery point

Record the EFS recovery point, T_S3 (the S3 cut time) and T_DB (the database
restore time). Set **both T_DB and T_S3 to the recovery point's CompletionDate**,
not its CreationDate.

Why: an EFS backup is not a point in time. It copies files while it runs (the
Nov 16 daily point ran from 05:00 to 08:03), so files synced during that window
are in the restored files and in S3 but not in a database restored to the
start. With T_DB at the completion time the database is at least as new as the
files: attempt 1 (T_DB at 05:00) found 60 `object_missing` problems and 60
`object_orphan` warnings for a run synced at 05:33, and attempt 2 (T_DB at
08:03) found 0 and 0.

Limit: a sync made during the backup window can still leave the database ahead
of the files for that run. Look at the sync times and, when you need a clean
result, pick a point whose window had no syncs (or quiesce writers).

### Option A: an on-demand EFS point now (reads production EFS, writes nothing to it)

On Nov 16, 2026 this was refused: `start-backup-job` returned AccessDenied
("Insufficient privileges") for the operator's `aws login` session, although
that session is an admin with no SCP and no vault policy in the way (restore
jobs worked). If it is refused, use option B (the latest scheduled point). The
reason is open in the roadmap Backlog.

```bash
. $D/common.sh && drill_init
RP_JOB=$(aws backup start-backup-job --backup-vault-name "$VAULT" --resource-arn "$PROD_FS_ARN" \
  --iam-role-arn "$BACKUP_ROLE" --start-window-minutes 60 --complete-window-minutes 720 \
  --lifecycle DeleteAfterDays=7 --recovery-point-tags Project=agenticml-restore-drill,Step=6.1 \
  --idempotency-token "drill-$(date -u +%Y%m%d)" --query BackupJobId --output text)
drill_set RP_JOB "$RP_JOB"
aws backup describe-backup-job --backup-job-id "$RP_JOB" \
  --query '[State,PercentDone,CreationDate,CompletionDate,RecoveryPointArn]' --output text   # repeat until COMPLETED
```

The RDS restore (step 3) waits for the job: T_DB is its completion time. When the
job is `COMPLETED`:

```bash
. $D/common.sh && drill_init
drill_set RP "$(aws backup describe-backup-job --backup-job-id "$RP_JOB" --query RecoveryPointArn --output text)"
drill_set T_S3 "$(date -u -d "$(aws backup describe-recovery-point --backup-vault-name "$VAULT" --recovery-point-arn "$RP" --query CompletionDate --output text)" +%Y-%m-%dT%H:%M:%S+00:00)"
drill_set T_DB "$(date -u -d "$T_S3" +%Y-%m-%dT%H:%M:%SZ)"
aws backup describe-recovery-point --backup-vault-name "$VAULT" --recovery-point-arn "$RP" \
  --query '[Status,CreationDate,CompletionDate,BackupSizeInBytes,CreatedBy]'
```

Check: `COMPLETED`, and `CreatedBy` has no `BackupPlanId` (the drill's own point).

### Option B: the next scheduled point (no vault writes)

Wait for the scheduled EFS point to complete, then:

```bash
. $D/common.sh && drill_init
aws backup list-recovery-points-by-backup-vault --backup-vault-name "$VAULT" --by-resource-type EFS \
  --by-created-after <date at 00:00:00Z> --query 'RecoveryPoints[].[RecoveryPointArn,CreationDate,CompletionDate,Status]' --output text
drill_set RP "<recovery point ARN>"
drill_set T_S3 "<its CompletionDate, UTC, as 2026-01-01T08:03:00+00:00>"
drill_set T_DB "<the same CompletionDate, as 2026-01-01T08:03:00Z>"
```

Do not take an on-demand point in option B.

Recovery point age per store is the drill start minus the data time: RDS is
T_RDS_START − T_DB, EFS is the restore job's CreationDate − the point's
CompletionDate, S3 is T_S3_START − T_S3. For context also record each mechanism's
RPO: RDS point-in-time lags about 5 minutes (`LatestRestorableTime`), EFS
backups run on the plan's schedule (look it up), S3 versioning is continuous
but noncurrent versions expire after 30 days.

## 2. Drill cluster (free)

```bash
. $D/common.sh && drill_init
aws ecs create-cluster --cluster-name "$DRILL_CLUSTER" --tags key=Project,value=agenticml-restore-drill \
  --query 'cluster.[clusterName,status]' --output text      # agenticml-restore-drill ACTIVE
```

## 3. RDS point-in-time restore to a new instance

Start when `LatestRestorableTime` is at or after T_DB (5 to 10 minutes after it).

```bash
. $D/common.sh && drill_init
until [ "$(date -u -d "$(aws rds describe-db-instances --db-instance-identifier "$PROD_DB" --query 'DBInstances[0].LatestRestorableTime' --output text)" +%s)" -ge "$(date -u -d "$T_DB" +%s)" ]; do sleep 30; done
drill_set DRILL_DB agenticml-restore-drill
drill_set T_RDS_START "$(date -u +%FT%TZ)"
aws rds restore-db-instance-to-point-in-time \
  --source-db-instance-identifier "$PROD_DB" --target-db-instance-identifier "$DRILL_DB" \
  --restore-time "$T_DB" --db-instance-class db.t4g.micro \
  --db-subnet-group-name "$DB_SUBNET_GROUP" --vpc-security-group-ids "$RDS_SG" \
  --db-parameter-group-name "$DB_PARAM_GROUP" --option-group-name "$DB_OPTION_GROUP" \
  --ca-certificate-identifier "$DB_CA" --no-publicly-accessible --no-multi-az --no-deletion-protection \
  --no-auto-minor-version-upgrade --backup-retention-period 0 \
  --tags Key=Project,Value=agenticml-restore-drill Key=Step,Value=6.1 \
  --query 'DBInstance.[DBInstanceIdentifier,DBInstanceStatus]' --output text
# If the API rejects --backup-retention-period 0, repeat with 1 and add --delete-automated-backups at cleanup.
until aws rds wait db-instance-available --db-instance-identifier "$DRILL_DB" 2>/dev/null; do :; done
drill_set T_RDS_DONE "$(date -u +%FT%TZ)"
drill_set DRILL_DB_HOST "$(aws rds describe-db-instances --db-instance-identifier "$DRILL_DB" --query 'DBInstances[0].Endpoint.Address' --output text)"
aws rds describe-db-instances --db-instance-identifier "$DRILL_DB" --query 'DBInstances[0].{st:DBInstanceStatus,enc:StorageEncrypted,kms:KmsKeyId,sg:VpcSecurityGroups[].VpcSecurityGroupId,sng:DBSubnetGroup.DBSubnetGroupName,pg:DBParameterGroups[].DBParameterGroupName,dp:DeletionProtection,pub:PubliclyAccessible,msu:MasterUserSecret,ca:CACertificateIdentifier,ret:BackupRetentionPeriod}'
aws rds describe-events --source-type db-instance --source-identifier "$DRILL_DB" --duration 240 \
  --query 'Events[].[Date,Message]' --output text | tee "$DRILL_STATE_DIR/out/rds-events.txt"
```

Expected: `available`, encrypted with the source key, the production security
group, subnet group and parameter group (so `rds.force_ssl` applies), not
public, no deletion protection, **`MasterUserSecret` null** (no new secret was
created). If `msu` is not null, stop. The RDS recovery duration is
T_RDS_DONE − T_RDS_START, cross-checked against the events.

## 4. EFS restore to a new filesystem

Needs the recovery point to be `COMPLETED`.

```bash
. $D/common.sh && drill_init
TOKEN=drill-$(date -u +%Y%m%d%H%M%S)
printf '{"file-system-id":"%s","Encrypted":"true","KmsKeyId":"%s","PerformanceMode":"generalPurpose","CreationToken":"%s","newFileSystem":"true"}\n' \
  "$PROD_FS" "$EFS_KMS" "$TOKEN" > "$DRILL_STATE_DIR/restore-meta.json"
aws backup get-recovery-point-restore-metadata --backup-vault-name "$VAULT" --recovery-point-arn "$RP"   # compare with the file
drill_set T_EFS_START "$(date -u +%FT%TZ)"
JOB=$(aws backup start-restore-job --recovery-point-arn "$RP" --iam-role-arn "$BACKUP_ROLE" --resource-type EFS \
  --metadata "file://$DRILL_STATE_DIR/restore-meta.json" --idempotency-token "$TOKEN" \
  --no-copy-source-tags-to-restored-resource --query RestoreJobId --output text)
drill_set RESTORE_JOB "$JOB"
until s=$(aws backup describe-restore-job --restore-job-id "$JOB" --query Status --output text); [ "$s" = COMPLETED ] || [ "$s" = FAILED ] || [ "$s" = ABORTED ]; do sleep 30; done; echo "$s"
aws backup describe-restore-job --restore-job-id "$JOB" \
  --query '[Status,CreationDate,CompletionDate,RecoveryPointCreationDate,CreatedResourceArn,StatusMessage]' --output text | tee "$DRILL_STATE_DIR/out/efs-restore-job.txt"
drill_set DFS "$(aws backup describe-restore-job --restore-job-id "$JOB" --query CreatedResourceArn --output text | sed 's#.*/##')"
```

Check: `COMPLETED` and a new filesystem ID. If the metadata keys are rejected, the
job fails at once and creates nothing: fix the keys and retry with a new
`TOKEN`. If `--no-copy-source-tags-to-restored-resource` is rejected for EFS,
drop it. The EFS recovery duration is CompletionDate − CreationDate, plus the
mount target and access points until `T_EFS_READY`.

### Prepare and inspect the drill filesystem

```bash
. $D/common.sh && drill_init
aws efs tag-resource --resource-id "$DFS" --tags Key=Project,Value=agenticml-restore-drill Key=Step,Value=6.1 Key=Name,Value=agenticml-restore-drill
aws efs describe-file-systems --file-system-id "$DFS" --query 'FileSystems[0].[Encrypted,KmsKeyId,PerformanceMode,ThroughputMode,LifeCycleState]' --output text
# Expect encrypted, the production key, generalPurpose, elastic. If the throughput mode is not elastic:
#   aws efs update-file-system --file-system-id "$DFS" --throughput-mode elastic
aws efs describe-file-system-policy --file-system-id "$DFS" || echo "no policy (expected)"
# If a policy exists, delete it on the drill filesystem only: aws efs delete-file-system-policy --file-system-id "$DFS"
aws efs create-mount-target --file-system-id "$DFS" --subnet-id "$DATA_SUBNET" --security-groups "$EFS_SG" --query MountTargetId --output text
until [ "$(aws efs describe-mount-targets --file-system-id "$DFS" --query 'MountTargets[0].LifeCycleState' --output text)" = available ]; do sleep 10; done
drill_set AP_INSPECT "$(aws efs create-access-point --file-system-id "$DFS" --posix-user Uid=0,Gid=0 --root-directory Path=/ \
  --tags Key=Project,Value=agenticml-restore-drill Key=Name,Value=drill-inspect --query AccessPointId --output text)"
until [ "$(aws efs describe-access-points --access-point-id "$AP_INSPECT" --query 'AccessPoints[0].LifeCycleState' --output text)" = available ]; do sleep 5; done
python3 -I $D/mk_td.py inspect "$DRILL_STATE_DIR/prod-td.json" "$DRILL_STATE_DIR/td-inspect.json"
aws ecs register-task-definition --cli-input-json "file://$DRILL_STATE_DIR/td-inspect.json" --query 'taskDefinition.taskDefinitionArn' --output text
bash $D/run_task.sh agenticml-restore-drill-inspect
```

Expected: the inspect task exits 0, the log shows `top level: [...]` and ends with
`TOTAL files=… bytes=…`. Find the `agenticml` directory (mode 700, owner
`10001:10001`, containing `server` and `workspaces`): either `/agenticml` or, if
AWS Backup restored into a subfolder, `/aws-backup-restore_<timestamp>/agenticml`.
Then replace the root access point with the data access point:

```bash
. $D/common.sh && drill_init
DATA_PATH=/agenticml        # or /aws-backup-restore_<timestamp>/agenticml, from the inspect log
aws efs delete-access-point --access-point-id "$AP_INSPECT"
drill_set AP_DATA "$(aws efs create-access-point --file-system-id "$DFS" --posix-user Uid=10001,Gid=10001 \
  --root-directory "Path=$DATA_PATH" --tags Key=Project,Value=agenticml-restore-drill Key=Name,Value=drill-data \
  --query AccessPointId --output text)"
drill_set T_EFS_READY "$(date -u +%FT%TZ)"
```

## 5. S3: drill bucket and the versions current at T_S3

```bash
. $D/common.sh && drill_init
drill_set TEST_B "agenticml-restore-drill-$(date -u +%Y%m%d)-$RANDOM"
aws s3api create-bucket --bucket "$TEST_B" --create-bucket-configuration "LocationConstraint=$AWS_REGION"
aws s3api put-public-access-block --bucket "$TEST_B" --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-encryption --bucket "$TEST_B" --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-bucket-tagging --bucket "$TEST_B" --tagging 'TagSet=[{Key=Project,Value=agenticml-restore-drill},{Key=Step,Value=6.1}]'
cat > "$DRILL_STATE_DIR/drill-bucket-policy.json" <<EOF
{"Version":"2012-10-17","Statement":[
 {"Sid":"DrillTaskObjects","Effect":"Allow","Principal":{"AWS":"$TASK_ROLE"},"Action":["s3:GetObject","s3:PutObject","s3:DeleteObject"],"Resource":"arn:aws:s3:::$TEST_B/hosted/*"},
 {"Sid":"DrillTaskList","Effect":"Allow","Principal":{"AWS":"$TASK_ROLE"},"Action":"s3:ListBucket","Resource":"arn:aws:s3:::$TEST_B"},
 {"Sid":"DenyInsecureTransport","Effect":"Deny","Principal":"*","Action":"s3:*","Resource":["arn:aws:s3:::$TEST_B","arn:aws:s3:::$TEST_B/*"],"Condition":{"Bool":{"aws:SecureTransport":"false"}}}]}
EOF
aws s3api put-bucket-policy --bucket "$TEST_B" --policy "file://$DRILL_STATE_DIR/drill-bucket-policy.json"
aws s3api get-bucket-policy-status --bucket "$TEST_B" --query PolicyStatus.IsPublic          # false
python3 -I $D/copy_versions_at.py "$T_S3" "$PROD_B" "$TEST_B" --dry-run | tail -3            # review: "would copy N, hidden H"
drill_set T_S3_START "$(date -u +%FT%TZ)"
python3 -I $D/copy_versions_at.py "$T_S3" "$PROD_B" "$TEST_B" | tee "$DRILL_STATE_DIR/out/s3-copy.txt" | tail -2
drill_set T_S3_DONE "$(date -u +%FT%TZ)"
aws s3api list-objects-v2 --bucket "$TEST_B" --prefix hosted/ --query '[length(Contents), sum(Contents[].Size)]' --output text
```

Expected: the copied count and bytes equal the dry run's and the preflight
snapshot (apart from activity since). Any integrity mismatch makes the script exit
non-zero. The task role needs `ListBucket`: without it a missing key returns 403 and the
server reads that as the store being unavailable.

## 6. Gate 1: read-only SQL check of the restored database

```bash
. $D/common.sh && drill_init
DB_HOST=$DRILL_DB_HOST python3 -I $D/mk_td.py dbcheck "$DRILL_STATE_DIR/prod-td.json" "$DRILL_STATE_DIR/td-dbcheck.json"
aws ecs register-task-definition --cli-input-json "file://$DRILL_STATE_DIR/td-dbcheck.json" --query 'taskDefinition.taskDefinitionArn' --output text
bash $D/run_task.sh agenticml-restore-drill-dbcheck
```

Expected: exit 0 and the log ends with `DRILL_DB_OK`; `who` shows the
`agenticml` user and database; `in_recovery False`; `ssl` true with TLSv1.2 or
TLSv1.3; `rds.force_ssl` is `on`/`1` or "not visible to this role" (the app role
may not read `rds.*`; the parameter group check in step 3 is the proof, and the
`ssl` line shows TLS in use); the highest migration version is 9; counts for
accounts, workspaces, memberships, keys, runs, run_files and objects recorded (an
absent table or a failed required query makes the task exit 1 without `DRILL_DB_OK`).
This proves the production secret's password works on the restored instance,
`verify-full` works against the new endpoint, and the parameter group is in
force.

Optional comparison with production (read-only transaction, no writes):

```bash
python3 -I $D/mk_td.py dbcheck --prod "$DRILL_STATE_DIR/prod-td.json" "$DRILL_STATE_DIR/td-dbcheck-prod.json"
aws ecs register-task-definition --cli-input-json "file://$DRILL_STATE_DIR/td-dbcheck-prod.json" --query 'taskDefinition.taskDefinitionArn' --output text
bash $D/run_task.sh agenticml-restore-drill-dbcheck-prod
```

The counts must match the drill's, minus activity after T_DB (compare
`latest_run_update`).

## 7. Gate 2: drill server, check mode

```bash
. $D/common.sh && drill_init
DB_HOST=$DRILL_DB_HOST python3 -I $D/mk_td.py server "$DRILL_STATE_DIR/prod-td.json" "$DRILL_STATE_DIR/td-server.json"
aws ecs register-task-definition --cli-input-json "file://$DRILL_STATE_DIR/td-server.json" --query 'taskDefinition.taskDefinitionArn' --output text
bash $D/run_task.sh agenticml-restore-drill-server
```

(`mk_td.py` reads `TEST_B`, `DFS` and `AP_DATA` from the state file environment too.)

Expected: exit 0; `## mount` shows the data mount; `## healthy <time>`; no "S3
object bucket is unreachable" error; `## status` lists every workspace with its
members, keys, runs and objects (the object total matches the S3 copy), and the
users with roles; `## usage` is printed; `verify --deep` reports 0 problems
(warnings such as orphans are recorded as cross-store inconsistency, not
failures); `## done rc=0`. The time from the task's `createdAt` to `## healthy` is
the app recovery step.

## 8. Gate 3: a new sync and a login per workspace

From the gate-2 `users`, pick `WS` (a workspace slug) and `DRILL_USER` (one of
its members), plus one owner or admin user for each workspace.

```bash
. $D/common.sh && drill_init
OV='{"containerOverrides":[{"name":"server","environment":[{"name":"DRILL_MODE","value":"sync"},{"name":"WS","value":"<slug1>"},{"name":"DRILL_USER","value":"<member of slug1>"},{"name":"LOGIN_PAIRS","value":"<slug1>:<admin1> <slug2>:<admin2>"}]}]}'
bash $D/run_task.sh agenticml-restore-drill-server "$OV"
aws s3api list-objects-v2 --bucket "$TEST_B" --prefix hosted/ --query 'length(Contents)'       # the gate-2 count plus at least 1
aws s3api list-object-versions --bucket "$PROD_B" --prefix hosted/ --query '[length(Versions), length(DeleteMarkers||`[]`)]'   # as in the preflight apart from real user activity
```

Expected: `new sync verified: <run> [files]` and `drill sync: OK`; `login ok:
<slug> runs visible: N` for each workspace (N is that workspace's run count, plus
1 for the synced one); `verify --deep (after new sync)` with 0 problems; `##
done rc=0`. The temporary keys are revoked in the restored database only.
Firebase sign-in cannot be tested without public DNS: record that login was
checked with an API key.

## 9. 6.2 benchmark on the restored filesystem

Runs on the drill filesystem, not on production EFS, with production's settings
(same KMS key, generalPurpose, elastic, TLS mount through an access point, same
availability zone and image). Only IAM mount authorization is off, which affects
authorization, not I/O. 4 vCPU / 8 GB, 3 repetitions each of 1 and 8 clients.

```bash
. $D/common.sh && drill_init
drill_set AP_BENCH "$(aws efs create-access-point --file-system-id "$DFS" --posix-user Uid=10001,Gid=10001 \
  --root-directory 'Path=/bench,CreationInfo={OwnerUid=10001,OwnerGid=10001,Permissions=0700}' \
  --tags Key=Project,Value=agenticml-restore-drill Key=Name,Value=drill-bench --query AccessPointId --output text)"
until [ "$(aws efs describe-access-points --access-point-id "$AP_BENCH" --query 'AccessPoints[0].LifeCycleState' --output text)" = available ]; do sleep 5; done
aws efs describe-file-systems --file-system-id "$DFS" --query 'FileSystems[0].[PerformanceMode,ThroughputMode]' --output text   # generalPurpose elastic
# BENCH_COMMIT defaults to the commit in the deployed image tag. Pin it explicitly if you prefer.
python3 -I $D/mk_td.py bench "$DRILL_STATE_DIR/prod-td.json" "$DRILL_STATE_DIR/td-bench.json"
aws ecs register-task-definition --cli-input-json "file://$DRILL_STATE_DIR/td-bench.json" --query 'taskDefinition.taskDefinitionArn' --output text
bash $D/run_task.sh agenticml-restore-drill-bench
```

The task definition embeds `scripts/bench_server.py` as committed at the pinned
commit (`git show <commit>:scripts/bench_server.py`, gzipped and base64-encoded
at `mk_td.py` time) with its sha256, and the container checks the checksum before
running it. The bench task has no task role and no database, S3 or Firebase
environment variables.

**What the numbers mean.** SQLite refuses network filesystems, and in hosted
mode the database is Postgres, not EFS. So the benchmark task puts the
server's SQLite database on the container's local disk (`/tmp/bench-db`, one
file per run, removed after it, via a per-command
`AGENTICML_SERVER_DATABASE_URL` that the task's own environment guard never
sees) while the run files, appends and objects go to the EFS directory
`/data/benchmark-<epoch>`. The numbers measure EFS file I/O plus a local-disk
SQLite, not SQLite on EFS and not the Postgres path.

Expected: exit 0; `## host`, `## mount` (the NFS options from `/proc/mounts`),
`df`, `## db sqlite on local disk (/tmp), run files and objects on EFS`; six `## RUN clients={1,8} rep={1..3}` blocks each ending `## exit=0`; `##
done fail=0`; `## cleaned /data/benchmark-<epoch>`. Exit 99 means a production
variable was present (the definition has none); 98 means a checksum mismatch;
a run hitting the 900 s timeout means EFS latency is far worse than modelled:
record it. Copy the log, `td-bench.json` and the sha256 into the PR notes before
cleanup. Put the numbers in `docs/SERVER.md` ("Network filesystems (EFS)").

## 10. Cleanup (dependency order; drill resources only)

Save everything in `$DRILL_STATE_DIR/out` (logs, timings, events) into the
record first.

```bash
. $D/common.sh && drill_init
# 1. no drill task running
aws ecs list-tasks --cluster "$DRILL_CLUSTER" --desired-status RUNNING --query taskArns       # [] (else: aws ecs stop-task --cluster "$DRILL_CLUSTER" --task <arn>)
# 2. RDS (start first; it is the slowest). Guarded.
[ "$DRILL_DB" = agenticml-restore-drill ] && aws rds describe-db-instances --db-instance-identifier "$DRILL_DB" --query 'DBInstances[0].DeletionProtection' --output text   # False
[ "$DRILL_DB" = agenticml-restore-drill ] && aws rds delete-db-instance --db-instance-identifier "$DRILL_DB" --skip-final-snapshot --delete-automated-backups --query 'DBInstance.DBInstanceStatus' --output text
# 3. task definitions: deregister every revision, then delete
for kind in inspect dbcheck dbcheck-prod server bench; do
  for r in $(aws ecs list-task-definitions --family-prefix "agenticml-restore-drill-$kind" --status ACTIVE --query 'taskDefinitionArns[]' --output text); do
    aws ecs deregister-task-definition --task-definition "$r" --query taskDefinition.status --output text
  done
done
INACT=$(aws ecs list-task-definitions --family-prefix agenticml-restore-drill --status INACTIVE --query 'taskDefinitionArns[]' --output text)
[ -z "$INACT" ] || aws ecs delete-task-definitions --task-definitions $INACT --query failures
# 4. cluster
aws ecs delete-cluster --cluster "$DRILL_CLUSTER" --query 'cluster.status' --output text      # INACTIVE
# 5. EFS: access points, then mount targets, then the filesystem (never the production one)
if [ -n "${DFS:-}" ] && [ "$DFS" != "$PROD_FS" ]; then
  for ap in $(aws efs describe-access-points --file-system-id "$DFS" --query 'AccessPoints[].AccessPointId' --output text); do aws efs delete-access-point --access-point-id "$ap"; done
  for mt in $(aws efs describe-mount-targets --file-system-id "$DFS" --query 'MountTargets[].MountTargetId' --output text); do aws efs delete-mount-target --mount-target-id "$mt"; done
  until [ -z "$(aws efs describe-mount-targets --file-system-id "$DFS" --query 'MountTargets[].MountTargetId' --output text)" ]; do sleep 10; done
  until [ -z "$(aws efs describe-access-points --file-system-id "$DFS" --query 'AccessPoints[?LifeCycleState!=`deleted`].AccessPointId' --output text)" ]; do sleep 5; done
  aws efs delete-file-system --file-system-id "$DFS"
fi
# 6. S3 drill bucket (never versioned, so rm is a real delete)
case "$TEST_B" in agenticml-restore-drill-*) aws s3 rm "s3://$TEST_B" --recursive --quiet && aws s3api delete-bucket --bucket "$TEST_B";; esac
# 7. option A only: the drill's own recovery point (tag-checked; never a plan point)
if [ -n "${RP_JOB:-}" ] && [ "$(aws backup list-tags --resource-arn "$RP" --query 'Tags.Project' --output text)" = agenticml-restore-drill ]; then
  aws backup delete-recovery-point --backup-vault-name "$VAULT" --recovery-point-arn "$RP"
fi
# 8. log streams written by the drill tasks (prefixes restore-drill*)
for s in $(aws logs describe-log-streams --log-group-name "$LOG_GROUP" --log-stream-name-prefix restore-drill --query 'logStreams[].logStreamName' --output text); do
  aws logs delete-log-stream --log-group-name "$LOG_GROUP" --log-stream-name "$s"
done
# 9. wait for RDS
until aws rds wait db-instance-deleted --db-instance-identifier "$DRILL_DB" 2>/dev/null; do :; done
```

Local files to remove once the record is written: `$DRILL_STATE_DIR` (it holds the
logs, user names and production identifiers; the logs hold no keys or
passwords). Backup restore-job and backup-job history entries cannot be deleted
and cost nothing.

## 11. Final listing (proof that nothing remains and production is unchanged)

```bash
. $D/common.sh && drill_init
aws resourcegroupstaggingapi get-resources --tag-filters Key=Project,Values=agenticml-restore-drill --query 'ResourceTagMappingList[].ResourceARN'   # [] (may lag a few minutes)
aws rds describe-db-instances --query 'DBInstances[].DBInstanceIdentifier' --output text        # only production
aws rds describe-db-instance-automated-backups --query "DBInstanceAutomatedBackups[?DBInstanceIdentifier=='agenticml-restore-drill']"   # []
aws rds describe-db-snapshots --query "DBSnapshots[?DBInstanceIdentifier=='agenticml-restore-drill']"   # []
aws efs describe-file-systems --query 'FileSystems[].FileSystemId' --output text                # only production
aws efs describe-access-points --file-system-id "$PROD_FS" --query 'AccessPoints[].AccessPointId' --output text   # only production's one
aws ecs list-clusters --query clusterArns --output text                                         # only the production cluster
aws ecs list-task-definitions --family-prefix agenticml-restore-drill --query taskDefinitionArns   # []
aws s3api head-bucket --bucket "$TEST_B" 2>&1 | tail -1                                         # 404 Not Found
aws backup list-recovery-points-by-backup-vault --backup-vault-name "$VAULT" --query 'RecoveryPoints[?CreatedBy.BackupPlanId==`null`].RecoveryPointArn'   # []
aws logs describe-log-streams --log-group-name "$LOG_GROUP" --log-stream-name-prefix restore-drill --query 'logStreams[].logStreamName'   # []
aws ecs describe-services --cluster "$PROD_CLUSTER" --services "$PROD_SERVICE" --query 'services[0].[taskDefinition,runningCount]' --output text   # same revision as the preflight, 1 running
aws rds describe-db-instances --db-instance-identifier "$PROD_DB" --query 'DBInstances[0].[DBInstanceStatus,DeletionProtection,BackupRetentionPeriod]' --output text   # available True 7
aws s3api list-object-versions --bucket "$PROD_B" --prefix hosted/ --query '[length(Versions), length(DeleteMarkers||`[]`)]'   # as in the preflight apart from real user activity; no new delete markers
```

The tagging API can lag a few minutes behind deletions: the per-service
listings are the real proof.

## Record

Write the drill record (no AWS identifiers; the one below is the Nov 16 record) into the roadmap and the status
page: recovery point age and recovery duration per store, total time (first
restore call to the gate-3 `## done`), integrity results, any cross-store
orphans, the cost, and the benchmark tables in `docs/SERVER.md`. Timings come
from `state.env`, the `*.task.json` files, the restore job and the RDS events.

| Store | Recovery point age | Recovery duration |
|---|---|---|
| RDS | `T_RDS_START` − `T_DB` | `T_RDS_DONE` − `T_RDS_START` |
| EFS | restore CreationDate − point CreationDate | restore CompletionDate − CreationDate, plus mount target and access points until `T_EFS_READY` |
| S3 | `T_S3_START` − `T_S3` | `T_S3_DONE` − `T_S3_START` |
| App | none | gate-2 task `createdAt` to `## healthy` |

### Drill record (Nov 16, 2026)

All times UTC. Recovery point: the scheduled daily EFS point of Nov 16 (backup
job 05:00 to 08:03, about 3 hours). An on-demand point was refused (see option
A), so this was option B. Restored files landed under
`/aws-backup-restore_<timestamp>/agenticml`, and the data access point root
points there. The restored filesystem came back in Bursting mode and was
switched to Elastic on the drill filesystem.

| Store | Recovery point age | Recovery duration |
|---|---|---|
| RDS | about 5 minutes (point-in-time lag) | attempt 2, 12 min 36 s (15:42:34 to 15:55:10); attempt 1, 13 min 41 s |
| EFS | daily point: up to 24 h plus the 3 h backup run | restore job 1 min 32 s (15:19:02 to 15:20:34) for about 60 KB of run files; mount target and access points ready at 15:36:30 |
| S3 | continuous (noncurrent versions kept 30 days) | 64 versions, 1.57 GB, copied and SHA-256 checked in 3 min 11 s (15:19:49 to 15:23:00) |
| App | none | task created to healthy in 22 s (healthy 2 s after the container started) |

The stores restored in parallel, so the time to a verified service is about the
RDS restore plus about 1 minute.

Attempt 1 restored the database to the backup start (05:00): the server was
healthy, but `verify --deep` found 60 `object_missing` problems and 60
`object_orphan` warnings, all for one run synced at 05:33 (in the restored
files and in S3, not in the database). Attempt 2 restored the database to the
completion time (08:03:34), the rule in step 1.

Gates (attempt 2):

- Gate 1 (read-only SQL): connected with the production secret (a point-in-time
  restore keeps the master password, no new secret), TLS 1.3 with
  `verify-full`, migrations up to 9. Counts: accounts 8, workspaces 11,
  memberships 15, keys 2, runs 3, run_files 21, objects 64. Production had the
  same counts except runs 4 and run_files 28 (one 2.2-client run synced at
  14:11, after the restore point). `rds.force_ssl` is not visible to the app
  role; the check tolerates that.
- Gate 2 (server, check mode): healthy; `status` listed all 11 workspaces with
  their status (one archived workspace still suspended), and customer-one and
  customer-two with owner, member and viewer; `verify --deep` 0 problems, 0
  warnings (objects 64, runs 3).
- Gate 3 (new sync, logins): a new sync into customer-one (6 files, 1 new
  object) verified; admin-key logins for customer-one and customer-two listed
  their runs (2 each); `verify --deep` afterwards 0 and 0 (objects 65, runs 4).
  Firebase sign-in was not tested (the drill has no public DNS); logins were
  checked with an API key.
- Production was unchanged during the drill: objects bucket 64 versions and 0
  delete markers, hosted service on the same task definition with 1 task.
- 6.2 benchmark on the restored filesystem: see `docs/SERVER.md` (1 client:
  commit p50 about 11 ms, object PUT p50 about 35 ms; 8 clients: commit p50
  about 53 ms, object PUT p50 about 76 ms). The whole benchmark task took about
  3 minutes (15:43:53 to 15:46:36).
- Cleanup: every drill resource was deleted (database, filesystem, mount target,
  access points, bucket, task definitions, cluster, log streams). The scheduled
  recovery point was kept; it is not a drill resource. Cost: under $1.
