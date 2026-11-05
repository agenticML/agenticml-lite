output "alb_dns_name" {
  description = "ALB DNS name. Point hostname at this with an alias or CNAME."
  value       = aws_lb.server.dns_name
}

output "public_url" {
  description = "Value of AGENTICML_SERVER_PUBLIC_URL (https://hostname)."
  value       = "https://${var.hostname}"
}

output "secret_arn" {
  description = "Secrets Manager ARN for AGENTICML_SERVER_KEY. Put the value out of band."
  value       = aws_secretsmanager_secret.server_key.arn
}

output "efs_id" {
  description = "EFS file system ID."
  value       = aws_efs_file_system.data.id
}

output "cluster_name" {
  description = "ECS cluster name."
  value       = aws_ecs_cluster.this.name
}

output "service_name" {
  description = "ECS service name."
  value       = aws_ecs_service.server.name
}

output "log_group" {
  description = "CloudWatch log group for the task."
  value       = aws_cloudwatch_log_group.server.name
}
