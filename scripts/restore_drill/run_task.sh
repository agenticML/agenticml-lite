#!/usr/bin/env bash
# Run one restore-drill task, wait for it, print timings and the exit code, save the log.
# usage: run_task.sh FAMILY [OVERRIDES_JSON]
#   FAMILY          a task definition family named agenticml-restore-drill-* (register it first)
#   OVERRIDES_JSON  optional ECS run-task --overrides JSON (for example DRILL_MODE=sync variables)
# Always runs in the cluster agenticml-restore-drill, in the stack's task security group and the
# task subnet of the drill's availability zone, with a public IPv4 for egress only.
# Needs the state file written by drill_discover (DRILL_STATE_DIR, default ./restore-drill-state).
set -euo pipefail

if [ "${DRILL_CLUSTER:-agenticml-restore-drill}" != agenticml-restore-drill ]; then
  echo "refusing: DRILL_CLUSTER=${DRILL_CLUSTER} (drill tasks run only in agenticml-restore-drill)" >&2
  exit 2
fi
if [ $# -lt 1 ] || [ $# -gt 2 ] || [[ "$1" != agenticml-restore-drill-* ]]; then
  echo "usage: run_task.sh agenticml-restore-drill-<kind> [OVERRIDES_JSON]" >&2
  exit 2
fi
family=$1
overrides=${2:-}
cluster=agenticml-restore-drill

# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
drill_init
drill_need AWS_REGION TASK_SG DRILL_TASK_SUBNET LOG_GROUP

read -r container prefix < <(aws ecs describe-task-definition --task-definition "$family" \
  --query 'taskDefinition.containerDefinitions[0].[name, logConfiguration.options."awslogs-stream-prefix"]' --output text)

args=(ecs run-task --cluster "$cluster" --launch-type FARGATE --platform-version 1.4.0
  --task-definition "$family" --started-by restore-drill --count 1
  --tags "key=Project,value=agenticml-restore-drill"
  --network-configuration "awsvpcConfiguration={subnets=[$DRILL_TASK_SUBNET],securityGroups=[$TASK_SG],assignPublicIp=ENABLED}"
  --query 'tasks[0].taskArn' --output text)
[ -z "$overrides" ] || args+=(--overrides "$overrides")
arn=$(aws "${args[@]}")
if [ -z "$arn" ] || [ "$arn" = None ]; then
  echo "run-task started no task" >&2
  exit 1
fi
tid=${arn##*/}
echo "task $tid ($family) in $cluster"

# The waiter gives up after 10 minutes: allow up to 2 hours.
for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
  if aws ecs wait tasks-stopped --cluster "$cluster" --tasks "$arn" 2>/dev/null; then break; fi
done

out=$DRILL_STATE_DIR/out
aws ecs describe-tasks --cluster "$cluster" --tasks "$arn" \
  --query 'tasks[0].{createdAt:createdAt,pullStartedAt:pullStartedAt,pullStoppedAt:pullStoppedAt,startedAt:startedAt,stoppedAt:stoppedAt,lastStatus:lastStatus,exitCode:containers[0].exitCode,stoppedReason:stoppedReason,containerReason:containers[0].reason,az:availabilityZone,cpu:cpu,memory:memory}' \
  --output json | tee "$out/$family-$tid.task.json"

sleep 10  # awslogs delivers a few seconds after the container stops
aws logs filter-log-events --log-group-name "$LOG_GROUP" --log-stream-names "$prefix/$container/$tid" \
  --query 'events[].[message]' --output text | tee "$out/$family-$tid.log"
echo "log saved: $out/$family-$tid.log"

code=$(aws ecs describe-tasks --cluster "$cluster" --tasks "$arn" --query 'tasks[0].containers[0].exitCode' --output text)
echo "exit code: $code"
[ "$code" = 0 ]
