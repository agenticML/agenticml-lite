#!/usr/bin/env bash
# Shared helpers for the restore drill. Source it from bash (not zsh): `. scripts/restore_drill/common.sh`.
#
# State lives in $DRILL_STATE_DIR (default ./restore-drill-state, git-ignored):
#   state.env   `export NAME=value` lines: discovered production facts and the drill's own IDs
#   prod-td.json  the live production task definition (read only; input to mk_td.py)
#   out/        task timings and logs
# Nothing account-specific is kept in the repository: drill_discover reads it from the
# agenticml-hosted CloudFormation stack, the live task definition and describe calls.
# Needs AWS_REGION (and AWS_PROFILE or other credentials) in the environment.

DRILL_STATE_DIR=${DRILL_STATE_DIR:-./restore-drill-state}
DRILL_CLUSTER=agenticml-restore-drill
DRILL_PROJECT=agenticml-restore-drill
export AWS_PAGER=""

drill_init() {
  umask 077
  mkdir -p "$DRILL_STATE_DIR/out"
  DRILL_STATE_DIR=$(cd "$DRILL_STATE_DIR" && pwd)
  DRILL_STATE_FILE=$DRILL_STATE_DIR/state.env
  touch "$DRILL_STATE_FILE"
  # shellcheck disable=SC1090
  . "$DRILL_STATE_FILE"
}

# drill_set NAME VALUE: persist and export one value (replaces an earlier value).
drill_set() {
  local name=$1 value=$2
  { grep -v "^export $name=" "$DRILL_STATE_FILE" || true; } > "$DRILL_STATE_FILE.tmp"
  printf 'export %s=%q\n' "$name" "$value" >> "$DRILL_STATE_FILE.tmp"
  mv "$DRILL_STATE_FILE.tmp" "$DRILL_STATE_FILE"
  export "$name=$value"
}

# drill_need NAME...: fail unless every named variable is set.
drill_need() {
  local name
  for name in "$@"; do
    if [ -z "${!name:-}" ]; then
      echo "restore drill: $name is not set (run drill_discover, or the earlier README step that sets it)" >&2
      return 1
    fi
  done
}

_drill_out() {
  aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

_drill_res() {
  aws cloudformation describe-stack-resource --stack-name "$STACK" --logical-resource-id "$1" \
    --query StackResourceDetail.PhysicalResourceId --output text
}

# drill_discover: read every production fact the drill needs and store it in the state file.
drill_discover() {
  drill_need AWS_REGION || return 1
  STACK=${STACK_NAME:-agenticml-hosted}
  local td_arn fs_mt subnets
  drill_set STACK "$STACK"
  drill_set PROD_CLUSTER "$(_drill_out ClusterName)"
  drill_set PROD_SERVICE "$(_drill_out ServiceName)"
  drill_set PROD_DB "$(_drill_out DbInstanceId)"
  drill_set PROD_FS "$(_drill_out EfsId)"
  drill_set PROD_B "$(_drill_out ObjectsBucket)"
  drill_set LOG_GROUP "$(_drill_out LogGroup)"
  drill_set TASK_SG "$(_drill_out TaskSecurityGroup)"
  drill_set TASK_SUBNETS "$(_drill_out TaskSubnets)"
  drill_set PROD_HOSTNAME "$(_drill_out ServiceUrl | sed 's#^https\?://##; s#/.*##')"
  drill_set PROD_ALB_DNS "$(_drill_out AlbDnsName)"
  drill_set PROD_TG "$(_drill_res TargetGroup)"
  drill_set PROD_EXTRA_IDS "$PROD_TG $PROD_ALB_DNS"
  drill_set VAULT "$(_drill_res BackupVault)"
  drill_set BACKUP_ROLE "$(aws iam get-role --role-name "$(_drill_res BackupRole)" --query Role.Arn --output text)"

  # The live production task definition (read only): input to mk_td.py and source of the roles.
  td_arn=$(aws ecs describe-services --cluster "$PROD_CLUSTER" --services "$PROD_SERVICE" \
    --query 'services[0].taskDefinition' --output text)
  aws ecs describe-task-definition --task-definition "$td_arn" --query taskDefinition > "$DRILL_STATE_DIR/prod-td.json"
  drill_set PROD_TD "${td_arn##*/}"
  drill_set TASK_ROLE "$(aws ecs describe-task-definition --task-definition "$td_arn" --query taskDefinition.taskRoleArn --output text)"
  drill_set EXEC_ROLE "$(aws ecs describe-task-definition --task-definition "$td_arn" --query taskDefinition.executionRoleArn --output text)"

  # The production database: the restore reuses its network, parameter group and CA.
  local q='DBInstances[0]'
  drill_set SECRET_ARN "$(aws rds describe-db-instances --db-instance-identifier "$PROD_DB" --query "$q.MasterUserSecret.SecretArn" --output text)"
  drill_set RDS_SG "$(aws rds describe-db-instances --db-instance-identifier "$PROD_DB" --query "$q.VpcSecurityGroups[0].VpcSecurityGroupId" --output text)"
  drill_set DB_SUBNET_GROUP "$(aws rds describe-db-instances --db-instance-identifier "$PROD_DB" --query "$q.DBSubnetGroup.DBSubnetGroupName" --output text)"
  drill_set DB_PARAM_GROUP "$(aws rds describe-db-instances --db-instance-identifier "$PROD_DB" --query "$q.DBParameterGroups[0].DBParameterGroupName" --output text)"
  drill_set DB_OPTION_GROUP "$(aws rds describe-db-instances --db-instance-identifier "$PROD_DB" --query "$q.OptionGroupMemberships[0].OptionGroupName" --output text)"
  drill_set DB_CA "$(aws rds describe-db-instances --db-instance-identifier "$PROD_DB" --query "$q.CACertificateIdentifier" --output text)"
  drill_set PROD_DB_HOST "$(aws rds describe-db-instances --db-instance-identifier "$PROD_DB" --query "$q.Endpoint.Address" --output text)"

  # The production filesystem: same KMS key; mount target and security group for the drill copy.
  drill_set PROD_FS_ARN "$(aws efs describe-file-systems --file-system-id "$PROD_FS" --query 'FileSystems[0].FileSystemArn' --output text)"
  drill_set EFS_KMS "$(aws efs describe-file-systems --file-system-id "$PROD_FS" --query 'FileSystems[0].KmsKeyId' --output text)"

  # One availability zone for everything that mounts the drill filesystem: the AZ of the last
  # task subnet unless DRILL_AZ is set. The task subnet and the mount-target subnet match it.
  subnets=${TASK_SUBNETS//,/ }
  if [ -z "${DRILL_AZ:-}" ]; then
    drill_set DRILL_AZ "$(aws ec2 describe-subnets --subnet-ids $subnets --query 'Subnets[].AvailabilityZone' --output text | tr '\t' '\n' | sort | tail -1)"
  fi
  drill_set DRILL_TASK_SUBNET "$(aws ec2 describe-subnets --subnet-ids $subnets \
    --filters "Name=availability-zone,Values=$DRILL_AZ" --query 'Subnets[0].SubnetId' --output text)"
  fs_mt=$(aws efs describe-mount-targets --file-system-id "$PROD_FS" \
    --query "MountTargets[?AvailabilityZoneName=='$DRILL_AZ']|[0].MountTargetId" --output text)
  drill_set DATA_SUBNET "$(aws efs describe-mount-targets --mount-target-id "$fs_mt" --query 'MountTargets[0].SubnetId' --output text)"
  drill_set EFS_SG "$(aws efs describe-mount-target-security-groups --mount-target-id "$fs_mt" --query 'SecurityGroups[0]' --output text)"
  echo "discovered; state in $DRILL_STATE_FILE"
}
