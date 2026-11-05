# Example ECS Fargate stack for the AgenticML server. Adapt it; do not treat
# it as a reusable module.
#
# One task only: the server holds a single-writer lock on the data directory.
# desired_count is 1, and deployments use minimumHealthyPercent=0 /
# maximumPercent=100 so two tasks never run at once.

data "aws_region" "current" {}

locals {
  name  = "agenticml-server"
  image = "ghcr.io/agenticml/agenticml-server:2.0.0"
  port  = 8080
  uid   = 10001
}

# --- Networking -------------------------------------------------------------
# Ingress matches the plan: ALB from var.allowed_cidrs; task only from the
# ALB on 8080; EFS only from the task on 2049. Task egress is HTTPS (image,
# logs, secrets) plus NFS; ALB egress is only to the task. Standalone rules
# avoid a Terraform cycle between the groups.

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "ALB for the AgenticML server"
  vpc_id      = var.vpc_id

  tags = {
    Name = "${local.name}-alb"
  }
}

resource "aws_security_group" "task" {
  name        = "${local.name}-task"
  description = "Fargate task for the AgenticML server"
  vpc_id      = var.vpc_id

  tags = {
    Name = "${local.name}-task"
  }
}

resource "aws_security_group" "efs" {
  name        = "${local.name}-efs"
  description = "EFS mount targets for the AgenticML server"
  vpc_id      = var.vpc_id

  tags = {
    Name = "${local.name}-efs"
  }
}

resource "aws_security_group_rule" "alb_https" {
  type              = "ingress"
  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from allowed CIDRs"
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  cidr_blocks       = var.allowed_cidrs
}

resource "aws_security_group_rule" "alb_http" {
  type              = "ingress"
  security_group_id = aws_security_group.alb.id
  description       = "HTTP from allowed CIDRs (redirects to HTTPS)"
  from_port         = 80
  to_port           = 80
  protocol          = "tcp"
  cidr_blocks       = var.allowed_cidrs
}

resource "aws_security_group_rule" "alb_to_task" {
  type                     = "egress"
  security_group_id        = aws_security_group.alb.id
  description              = "To the task on 8080"
  from_port                = local.port
  to_port                  = local.port
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.task.id
}

resource "aws_security_group_rule" "task_from_alb" {
  type                     = "ingress"
  security_group_id        = aws_security_group.task.id
  description              = "From the ALB on 8080"
  from_port                = local.port
  to_port                  = local.port
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.alb.id
}

resource "aws_security_group_rule" "task_https" {
  type              = "egress"
  security_group_id = aws_security_group.task.id
  description       = "HTTPS for image pull, logs, and secrets"
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  cidr_blocks       = ["0.0.0.0/0"]
}

resource "aws_security_group_rule" "task_nfs" {
  type                     = "egress"
  security_group_id        = aws_security_group.task.id
  description              = "NFS to EFS"
  from_port                = 2049
  to_port                  = 2049
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.efs.id
}

resource "aws_security_group_rule" "efs_from_task" {
  type                     = "ingress"
  security_group_id        = aws_security_group.efs.id
  description              = "NFS from the task"
  from_port                = 2049
  to_port                  = 2049
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.task.id
}

# --- EFS --------------------------------------------------------------------

resource "aws_efs_file_system" "data" {
  encrypted = true

  tags = {
    Name = local.name
  }
}

resource "aws_efs_mount_target" "data" {
  for_each = toset(var.private_subnet_ids)

  file_system_id  = aws_efs_file_system.data.id
  subnet_id       = each.value
  security_groups = [aws_security_group.efs.id]
}

resource "aws_efs_access_point" "data" {
  file_system_id = aws_efs_file_system.data.id

  posix_user {
    uid = local.uid
    gid = local.uid
  }

  root_directory {
    path = "/agenticml"
    creation_info {
      owner_uid   = local.uid
      owner_gid   = local.uid
      permissions = "0750"
    }
  }

  tags = {
    Name = local.name
  }
}

# --- Secret -----------------------------------------------------------------
# Value is set out of band so the admin key is not in Terraform state:
#   aws secretsmanager put-secret-value --secret-id <arn> --secret-string <key>

resource "aws_secretsmanager_secret" "server_key" {
  name        = "${local.name}-key"
  description = "AGENTICML_SERVER_KEY (admin key named env). Put the value out of band."
  # Example stack: delete immediately on destroy so a re-apply can recreate
  # the same name. Production should use a recovery window (7–30 days).
  recovery_window_in_days = 0
}

# --- Logs -------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "server" {
  name              = "/ecs/${local.name}"
  retention_in_days = 30
}

# --- IAM --------------------------------------------------------------------

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "execution" {
  statement {
    sid = "Logs"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [
      aws_cloudwatch_log_group.server.arn,
      "${aws_cloudwatch_log_group.server.arn}:*",
    ]
  }

  statement {
    sid       = "Secret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.server_key.arn]
  }
}

data "aws_iam_policy_document" "task" {
  statement {
    sid = "EfsClient"
    actions = [
      "elasticfilesystem:ClientMount",
      "elasticfilesystem:ClientWrite",
    ]
    resources = [aws_efs_file_system.data.arn]

    condition {
      test     = "StringEquals"
      variable = "elasticfilesystem:AccessPointArn"
      values   = [aws_efs_access_point.data.arn]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${local.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy" "execution" {
  name   = "execution"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution.json
}

resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy" "task" {
  name   = "efs"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

# Enforce TLS on NFS rather than only setting transit_encryption on the
# volume. The Allow keeps the task role's access-point mount/write working
# (IAM auth plus TLS). Without it, attaching any file system policy would
# replace the default allow-all and break the mount.
data "aws_iam_policy_document" "efs" {
  statement {
    sid    = "AllowTaskRole"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.task.arn]
    }

    actions = [
      "elasticfilesystem:ClientMount",
      "elasticfilesystem:ClientWrite",
    ]
    resources = [aws_efs_file_system.data.arn]

    condition {
      test     = "StringEquals"
      variable = "elasticfilesystem:AccessPointArn"
      values   = [aws_efs_access_point.data.arn]
    }
  }

  statement {
    sid    = "EnforceTls"
    effect = "Deny"

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    actions   = ["*"]
    resources = [aws_efs_file_system.data.arn]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_efs_file_system_policy" "data" {
  file_system_id = aws_efs_file_system.data.id
  policy         = data.aws_iam_policy_document.efs.json
}

# --- ECS --------------------------------------------------------------------

resource "aws_ecs_cluster" "this" {
  name = local.name
}

resource "aws_ecs_task_definition" "server" {
  family                   = local.name
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([
    {
      name                   = "server"
      image                  = local.image
      essential              = true
      user                   = "${local.uid}:${local.uid}"
      readonlyRootFilesystem = true
      stopTimeout            = 30
      portMappings = [
        {
          containerPort = local.port
          protocol      = "tcp"
        }
      ]
      environment = [
        {
          name  = "AGENTICML_SERVER_PUBLIC_URL"
          value = "https://${var.hostname}"
        }
      ]
      secrets = [
        {
          name      = "AGENTICML_SERVER_KEY"
          valueFrom = aws_secretsmanager_secret.server_key.arn
        }
      ]
      mountPoints = [
        {
          sourceVolume  = "data"
          containerPath = "/data"
          readOnly      = false
        },
        {
          sourceVolume  = "tmp"
          containerPath = "/tmp"
          readOnly      = false
        }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.server.name
          awslogs-region        = data.aws_region.current.name
          awslogs-stream-prefix = "server"
        }
      }
      healthCheck = {
        command = [
          "CMD",
          "python",
          "-c",
          "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).read()"
        ]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 60
      }
    }
  ])

  volume {
    name = "data"

    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.data.id
      transit_encryption = "ENABLED"

      authorization_config {
        access_point_id = aws_efs_access_point.data.id
        iam             = "ENABLED"
      }
    }
  }

  volume {
    name = "tmp"
  }
}

resource "aws_ecs_service" "server" {
  name                               = local.name
  cluster                            = aws_ecs_cluster.this.id
  task_definition                    = aws_ecs_task_definition.server.arn
  desired_count                      = 1
  launch_type                        = "FARGATE"
  enable_execute_command             = false
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
  health_check_grace_period_seconds  = 60

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.server.arn
    container_name   = "server"
    container_port   = local.port
  }

  depends_on = [
    aws_lb_listener.https,
    aws_efs_mount_target.data,
    aws_efs_file_system_policy.data,
  ]
}

# --- ALB --------------------------------------------------------------------

resource "aws_lb" "server" {
  name                       = local.name
  internal                   = false
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.alb.id]
  subnets                    = var.public_subnet_ids
  drop_invalid_header_fields = true
}

resource "aws_lb_target_group" "server" {
  name                 = local.name
  port                 = local.port
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = var.vpc_id
  deregistration_delay = 30 # matches stopTimeout; the 300s default slows every single-task redeploy

  health_check {
    path                = "/healthz"
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.server.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"

    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.server.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.server.arn
  }
}

# --- Backup -----------------------------------------------------------------

data "aws_iam_policy_document" "backup_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["backup.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "backup" {
  name               = "${local.name}-backup"
  assume_role_policy = data.aws_iam_policy_document.backup_assume.json
}

resource "aws_iam_role_policy_attachment" "backup" {
  role       = aws_iam_role.backup.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup"
}

resource "aws_backup_vault" "efs" {
  name = "${local.name}-efs"
}

resource "aws_backup_plan" "efs" {
  name = "${local.name}-efs-daily"

  rule {
    rule_name         = "daily"
    target_vault_name = aws_backup_vault.efs.name
    schedule          = "cron(0 5 * * ? *)"

    lifecycle {
      delete_after = 35
    }
  }
}

resource "aws_backup_selection" "efs" {
  name         = "${local.name}-efs"
  plan_id      = aws_backup_plan.efs.id
  iam_role_arn = aws_iam_role.backup.arn
  resources    = [aws_efs_file_system.data.arn]

  depends_on = [aws_iam_role_policy_attachment.backup]
}
