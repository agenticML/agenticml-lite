resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
}
resource "aws_db_parameter_group" "main" {
  name   = "${local.name}-pg16"
  family = "postgres16"
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
}
resource "aws_db_instance" "main" {
  identifier                   = local.name
  engine                       = "postgres"
  engine_version               = "16"
  instance_class               = "db.t4g.small"
  db_name                      = "agenticml"
  username                     = "agenticml"
  manage_master_user_password  = true
  multi_az                     = var.multi_az
  allocated_storage            = 20
  max_allocated_storage        = 100
  storage_type                 = "gp3"
  storage_encrypted            = true
  publicly_accessible          = false
  db_subnet_group_name         = aws_db_subnet_group.main.name
  vpc_security_group_ids       = [aws_security_group.rds.id]
  parameter_group_name         = aws_db_parameter_group.main.name
  backup_retention_period      = 7
  deletion_protection          = true
  performance_insights_enabled = false
  skip_final_snapshot          = false
  final_snapshot_identifier    = var.final_snapshot_identifier
  copy_tags_to_snapshot        = true
  auto_minor_version_upgrade   = true
}
