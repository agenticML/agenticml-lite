resource "aws_sns_topic" "alarms" { name = "${local.name}-alarms" }
resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}
locals {
  alarms = {
    alb_5xx = {
      namespace  = "AWS/ApplicationELB"
      metric     = "HTTPCode_ELB_5XX_Count"
      statistic  = "Sum"
      threshold  = 5
      comparison = "GreaterThanThreshold"
      dimensions = { LoadBalancer = aws_lb.main.arn_suffix }
    }
    unhealthy = {
      namespace  = "AWS/ApplicationELB"
      metric     = "UnHealthyHostCount"
      statistic  = "Maximum"
      threshold  = 1
      comparison = "GreaterThanOrEqualToThreshold"
      dimensions = { LoadBalancer = aws_lb.main.arn_suffix, TargetGroup = aws_lb_target_group.main.arn_suffix }
    }
    rds_storage = {
      namespace  = "AWS/RDS"
      metric     = "FreeStorageSpace"
      statistic  = "Minimum"
      threshold  = 2000000000
      comparison = "LessThanThreshold"
      dimensions = { DBInstanceIdentifier = aws_db_instance.main.identifier }
    }
    rds_cpu = {
      namespace  = "AWS/RDS"
      metric     = "CPUUtilization"
      statistic  = "Average"
      threshold  = 80
      comparison = "GreaterThanThreshold"
      dimensions = { DBInstanceIdentifier = aws_db_instance.main.identifier }
    }
    running_tasks = {
      namespace  = "ECS/ContainerInsights"
      metric     = "RunningTaskCount"
      statistic  = "Minimum"
      threshold  = 2
      comparison = "LessThanThreshold"
      dimensions = { ClusterName = aws_ecs_cluster.main.name, ServiceName = aws_ecs_service.main.name }
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "main" {
  for_each            = local.alarms
  alarm_name          = "${local.name}-${each.key}"
  namespace           = each.value.namespace
  metric_name         = each.value.metric
  statistic           = each.value.statistic
  dimensions          = each.value.dimensions
  comparison_operator = each.value.comparison
  threshold           = each.value.threshold
  period              = each.key == "unhealthy" ? 60 : 300
  evaluation_periods  = each.key == "unhealthy" ? 5 : 1
  treat_missing_data  = each.key == "running_tasks" ? "breaching" : "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}
resource "aws_cloudwatch_metric_alarm" "latency" {
  alarm_name          = "${local.name}-latency-p95"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "TargetResponseTime"
  extended_statistic  = "p95"
  dimensions          = { LoadBalancer = aws_lb.main.arn_suffix, TargetGroup = aws_lb_target_group.main.arn_suffix }
  comparison_operator = "GreaterThanThreshold"
  threshold           = 5
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}
