resource "aws_efs_file_system" "main" {
  encrypted        = true
  performance_mode = "generalPurpose"
  throughput_mode  = "elastic"
}
resource "aws_efs_mount_target" "main" {
  count           = 2
  file_system_id  = aws_efs_file_system.main.id
  subnet_id       = aws_subnet.private[count.index].id
  security_groups = [aws_security_group.efs.id]
}
resource "aws_efs_access_point" "main" {
  file_system_id = aws_efs_file_system.main.id
  posix_user {
    uid = 10001
    gid = 10001
  }
  root_directory {
    path = "/agenticml"
    creation_info {
      owner_uid   = 10001
      owner_gid   = 10001
      permissions = "0700"
    }
  }
}
resource "aws_efs_file_system_policy" "main" {
  file_system_id = aws_efs_file_system.main.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowTaskRoleViaAccessPoint"
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.task.arn }
        Action    = ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"]
        Resource  = aws_efs_file_system.main.arn
        Condition = {
          Bool         = { "aws:SecureTransport" = "true" }
          StringEquals = { "elasticfilesystem:AccessPointArn" = aws_efs_access_point.main.arn }
        }
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "elasticfilesystem:Client*"
        Resource  = aws_efs_file_system.main.arn
        Condition = {
          Bool = { "aws:SecureTransport" = "false" }
        }
      },
      {
        Sid       = "DenyOutsideAccessPoint"
        Effect    = "Deny"
        Principal = "*"
        Action    = "elasticfilesystem:Client*"
        Resource  = aws_efs_file_system.main.arn
        Condition = {
          StringNotEquals = { "elasticfilesystem:AccessPointArn" = aws_efs_access_point.main.arn }
        }
      },
    ]
  })
}

resource "aws_backup_vault" "main" { name = local.name }

resource "aws_backup_plan" "main" {
  name = local.name
  rule {
    rule_name         = "daily"
    target_vault_name = aws_backup_vault.main.name
    schedule          = "cron(0 5 * * ? *)"
    lifecycle { delete_after = 35 }
  }
}

resource "aws_iam_role" "backup" {
  name = "${local.name}-backup"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "backup.amazonaws.com" }
        Action    = "sts:AssumeRole"
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "backup" {
  role       = aws_iam_role.backup.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup"
}

resource "aws_iam_role_policy_attachment" "backup_restore" {
  role       = aws_iam_role.backup.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForRestores"
}

resource "aws_backup_selection" "main" {
  name         = local.name
  plan_id      = aws_backup_plan.main.id
  iam_role_arn = aws_iam_role.backup.arn
  resources    = [aws_efs_file_system.main.arn, aws_db_instance.main.arn]
  depends_on   = [aws_iam_role_policy_attachment.backup]
}
