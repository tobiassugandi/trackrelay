locals {
  worker_autoscaling_policy = {
    policy_version                = 6
    maximum_capacity              = 8
    minimum_capacity              = 1
    metric_period_seconds         = 60
    scale_in_messages_per_minute  = 120
    scale_in_queue_work_threshold = 10
    scale_in_cooldown_seconds     = 60
    scale_in_evaluation_periods   = 1
    scale_in_period_seconds       = 10
    scale_in_quiet_seconds        = 120
    scale_out_messages_per_minute = null
    scale_out_period_seconds      = 10
    scale_out_rate_per_second     = 3
    scale_out_cooldown_seconds    = 60
    scale_out_evaluation_periods  = 2
  }
}

resource "aws_appautoscaling_target" "async_worker" {
  count = var.worker_autoscaling_enabled && local.async_runtime_enabled ? 1 : 0

  max_capacity       = local.worker_autoscaling_policy.maximum_capacity
  min_capacity       = local.worker_autoscaling_policy.minimum_capacity
  resource_id        = "service/${aws_ecs_cluster.async[0].name}/${aws_ecs_service.async_worker[0].name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"

  tags = {
    Name = "${local.name_prefix}-worker"
  }
}

resource "aws_appautoscaling_policy" "async_worker_scale_out" {
  count = var.worker_autoscaling_enabled && local.async_runtime_enabled ? 1 : 0

  name               = "${local.name_prefix}-worker-scale-out"
  policy_type        = "StepScaling"
  resource_id        = aws_appautoscaling_target.async_worker[0].resource_id
  scalable_dimension = aws_appautoscaling_target.async_worker[0].scalable_dimension
  service_namespace  = aws_appautoscaling_target.async_worker[0].service_namespace

  step_scaling_policy_configuration {
    adjustment_type         = "ExactCapacity"
    cooldown                = local.worker_autoscaling_policy.scale_out_cooldown_seconds
    metric_aggregation_type = "Maximum"

    step_adjustment {
      metric_interval_lower_bound = 0
      scaling_adjustment          = local.worker_autoscaling_policy.maximum_capacity
    }
  }
}

resource "aws_appautoscaling_policy" "async_worker_scale_in" {
  count = var.worker_autoscaling_enabled && local.async_runtime_enabled ? 1 : 0

  name               = "${local.name_prefix}-worker-scale-in"
  policy_type        = "StepScaling"
  resource_id        = aws_appautoscaling_target.async_worker[0].resource_id
  scalable_dimension = aws_appautoscaling_target.async_worker[0].scalable_dimension
  service_namespace  = aws_appautoscaling_target.async_worker[0].service_namespace

  step_scaling_policy_configuration {
    adjustment_type         = "ExactCapacity"
    cooldown                = local.worker_autoscaling_policy.scale_in_cooldown_seconds
    metric_aggregation_type = "Maximum"

    step_adjustment {
      metric_interval_lower_bound = 0
      scaling_adjustment          = local.worker_autoscaling_policy.minimum_capacity
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "async_worker_backlog_high" {
  count = var.worker_autoscaling_enabled && local.async_runtime_enabled ? 1 : 0

  actions_enabled     = true
  alarm_actions       = [aws_appautoscaling_policy.async_worker_scale_out[0].arn]
  alarm_description   = "Scale the experiment worker pool to its frozen maximum when demand rises."
  alarm_name          = "${local.name_prefix}-worker-demand-high"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  datapoints_to_alarm = local.worker_autoscaling_policy.scale_out_evaluation_periods
  dimensions = {
    QueueName = aws_sqs_queue.delivery[0].name
  }
  evaluation_periods = local.worker_autoscaling_policy.scale_out_evaluation_periods
  metric_name        = "ArrivalRate"
  namespace          = "TrackRelay/Elasticity"
  period             = local.worker_autoscaling_policy.scale_out_period_seconds
  statistic          = "Maximum"
  threshold          = local.worker_autoscaling_policy.scale_out_rate_per_second
  treat_missing_data = "notBreaching"

  tags = {
    Name = "${local.name_prefix}-worker-demand-high"
  }
}

resource "aws_cloudwatch_metric_alarm" "async_worker_empty" {
  count = var.worker_autoscaling_enabled && local.async_runtime_enabled ? 1 : 0

  actions_enabled     = true
  alarm_actions       = [aws_appautoscaling_policy.async_worker_scale_in[0].arn]
  alarm_description   = "Return the worker pool to its minimum after sustained low demand and queue work."
  alarm_name          = "${local.name_prefix}-worker-release-safe"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  datapoints_to_alarm = local.worker_autoscaling_policy.scale_in_evaluation_periods
  evaluation_periods  = local.worker_autoscaling_policy.scale_in_evaluation_periods
  threshold           = local.worker_autoscaling_policy.scale_in_quiet_seconds
  treat_missing_data  = "notBreaching"

  metric_query {
    id          = "release_safe"
    expression  = "FILL(quiet, 0)"
    period      = local.worker_autoscaling_policy.scale_in_period_seconds
    label       = "Low arrivals and low unfinished queue work"
    return_data = true
  }

  metric_query {
    id          = "quiet"
    return_data = false
    metric {
      dimensions  = { QueueName = aws_sqs_queue.delivery[0].name }
      metric_name = "QuietSeconds"
      namespace   = "TrackRelay/Elasticity"
      period      = local.worker_autoscaling_policy.scale_in_period_seconds
      stat        = "Minimum"
    }
  }
  tags = {
    Name = "${local.name_prefix}-worker-release-safe"
  }
}
