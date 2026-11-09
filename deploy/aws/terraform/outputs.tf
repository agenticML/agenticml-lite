output "alb_dns_name" {
  value = aws_lb.main.dns_name
}

output "ecr_repository_url" {
  value = aws_ecr_repository.main.repository_url
}

output "cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "service_name" {
  value = aws_ecs_service.main.name
}

output "deploy_role_arn" {
  value = aws_iam_role.deploy.arn
}

output "log_group" {
  value = aws_cloudwatch_log_group.main.name
}

output "objects_bucket" {
  value = aws_s3_bucket.objects.id
}

output "efs_id" {
  value = aws_efs_file_system.main.id
}
