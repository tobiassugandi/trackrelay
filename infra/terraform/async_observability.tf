locals {
  async_metric_period_seconds = 60

  async_observability_metric_contract = {
    alb_request_count = {
      namespace   = "AWS/ApplicationELB"
      metric_name = "RequestCount"
      statistic   = "Sum"
      dimensions  = ["LoadBalancer"]
    }
    alb_elb_4xx = {
      namespace   = "AWS/ApplicationELB"
      metric_name = "HTTPCode_ELB_4XX_Count"
      statistic   = "Sum"
      dimensions  = ["LoadBalancer"]
    }
    alb_elb_5xx = {
      namespace   = "AWS/ApplicationELB"
      metric_name = "HTTPCode_ELB_5XX_Count"
      statistic   = "Sum"
      dimensions  = ["LoadBalancer"]
    }
    alb_target_4xx = {
      namespace   = "AWS/ApplicationELB"
      metric_name = "HTTPCode_Target_4XX_Count"
      statistic   = "Sum"
      dimensions  = ["LoadBalancer"]
    }
    alb_target_5xx = {
      namespace   = "AWS/ApplicationELB"
      metric_name = "HTTPCode_Target_5XX_Count"
      statistic   = "Sum"
      dimensions  = ["LoadBalancer"]
    }
    api_p95_latency = {
      namespace   = "AWS/ApplicationELB"
      metric_name = "TargetResponseTime"
      statistic   = "p95"
      dimensions  = ["LoadBalancer"]
    }
    worker_running_tasks = {
      namespace   = "ECS/ContainerInsights"
      metric_name = "RunningTaskCount"
      statistic   = "Average"
      dimensions  = ["ClusterName", "ServiceName"]
    }
    source_queue_visible = {
      namespace   = "AWS/SQS"
      metric_name = "ApproximateNumberOfMessagesVisible"
      statistic   = "Maximum"
      dimensions  = ["QueueName"]
    }
    source_queue_in_flight = {
      namespace   = "AWS/SQS"
      metric_name = "ApproximateNumberOfMessagesNotVisible"
      statistic   = "Maximum"
      dimensions  = ["QueueName"]
    }
    source_queue_delayed = {
      namespace   = "AWS/SQS"
      metric_name = "ApproximateNumberOfMessagesDelayed"
      statistic   = "Maximum"
      dimensions  = ["QueueName"]
    }
    source_queue_oldest_age = {
      namespace   = "AWS/SQS"
      metric_name = "ApproximateAgeOfOldestMessage"
      statistic   = "Maximum"
      dimensions  = ["QueueName"]
    }
    dead_letter_queue_visible = {
      namespace   = "AWS/SQS"
      metric_name = "ApproximateNumberOfMessagesVisible"
      statistic   = "Maximum"
      dimensions  = ["QueueName"]
    }
  }

  async_observability_expression_contract = {
    observed_request_rate = {
      expression = "(m_requests + m_elb_4xx + m_elb_5xx) / ${local.async_metric_period_seconds}"
      label      = "Observed ALB requests/s"
    }
    request_error_percentage = {
      expression = "IF((m_requests + m_elb_4xx + m_elb_5xx) > 0, 100 * (m_target_4xx + m_target_5xx + m_elb_4xx + m_elb_5xx) / (m_requests + m_elb_4xx + m_elb_5xx), 0)"
      label      = "Request errors (%)"
    }
    source_queue_work = {
      expression = "m_visible + m_in_flight + m_delayed"
      label      = "Unfinished source-queue messages"
    }
  }

  async_cluster_name        = "${local.name_prefix}-async"
  async_worker_service_name = "${local.name_prefix}-worker"
  delivery_queue_name       = "${local.name_prefix}-delivery"
  delivery_dead_letter_name = "${local.name_prefix}-delivery-dlq"
}

resource "aws_cloudwatch_dashboard" "async" {
  count = local.async_enabled ? 1 : 0

  dashboard_name = "${local.name_prefix}-async"
  dashboard_body = jsonencode({
    start          = "-PT1H"
    periodOverride = "inherit"
    widgets = [
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 8
        height = 6
        properties = {
          title   = "Observed API request rate"
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          period  = local.async_metric_period_seconds
          yAxis = {
            left = { min = 0 }
          }
          metrics = [
            ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", aws_lb.async[0].arn_suffix, { id = "m_requests", stat = "Sum", visible = false }],
            ["AWS/ApplicationELB", "HTTPCode_ELB_4XX_Count", "LoadBalancer", aws_lb.async[0].arn_suffix, { id = "m_elb_4xx", stat = "Sum", visible = false }],
            ["AWS/ApplicationELB", "HTTPCode_ELB_5XX_Count", "LoadBalancer", aws_lb.async[0].arn_suffix, { id = "m_elb_5xx", stat = "Sum", visible = false }],
            [{ expression = local.async_observability_expression_contract.observed_request_rate.expression, id = "e_request_rate", label = local.async_observability_expression_contract.observed_request_rate.label }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 8
        y      = 0
        width  = 8
        height = 6
        properties = {
          title   = "API p95 target latency"
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          period  = local.async_metric_period_seconds
          yAxis = {
            left = { min = 0 }
          }
          annotations = {
            horizontal = [{ label = "500 ms SLO", value = 0.5 }]
          }
          metrics = [
            ["AWS/ApplicationELB", "TargetResponseTime", "LoadBalancer", aws_lb.async[0].arn_suffix, { id = "m_p95_latency", label = "p95 latency (seconds)", stat = "p95" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 16
        y      = 0
        width  = 8
        height = 6
        properties = {
          title   = "API request errors"
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          period  = local.async_metric_period_seconds
          yAxis = {
            left = { min = 0 }
          }
          annotations = {
            horizontal = [{ label = "1% SLO", value = 1 }]
          }
          metrics = [
            ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", aws_lb.async[0].arn_suffix, { id = "m_requests", stat = "Sum", visible = false }],
            ["AWS/ApplicationELB", "HTTPCode_Target_4XX_Count", "LoadBalancer", aws_lb.async[0].arn_suffix, { id = "m_target_4xx", stat = "Sum", visible = false }],
            ["AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", "LoadBalancer", aws_lb.async[0].arn_suffix, { id = "m_target_5xx", stat = "Sum", visible = false }],
            ["AWS/ApplicationELB", "HTTPCode_ELB_4XX_Count", "LoadBalancer", aws_lb.async[0].arn_suffix, { id = "m_elb_4xx", stat = "Sum", visible = false }],
            ["AWS/ApplicationELB", "HTTPCode_ELB_5XX_Count", "LoadBalancer", aws_lb.async[0].arn_suffix, { id = "m_elb_5xx", stat = "Sum", visible = false }],
            [{ expression = local.async_observability_expression_contract.request_error_percentage.expression, id = "e_request_errors", label = local.async_observability_expression_contract.request_error_percentage.label }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 6
        width  = 8
        height = 6
        properties = {
          title   = "Running worker tasks"
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          period  = local.async_metric_period_seconds
          yAxis = {
            left = { min = 0 }
          }
          metrics = [
            ["ECS/ContainerInsights", "RunningTaskCount", "ClusterName", local.async_cluster_name, "ServiceName", local.async_worker_service_name, { id = "m_worker_tasks", label = "Running workers", stat = "Average" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 8
        y      = 6
        width  = 8
        height = 6
        properties = {
          title   = "Delivery queue work"
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          period  = local.async_metric_period_seconds
          yAxis = {
            left = { min = 0 }
          }
          metrics = [
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", local.delivery_queue_name, { id = "m_visible", stat = "Maximum", visible = false }],
            ["AWS/SQS", "ApproximateNumberOfMessagesNotVisible", "QueueName", local.delivery_queue_name, { id = "m_in_flight", stat = "Maximum", visible = false }],
            ["AWS/SQS", "ApproximateNumberOfMessagesDelayed", "QueueName", local.delivery_queue_name, { id = "m_delayed", stat = "Maximum", visible = false }],
            [{ expression = local.async_observability_expression_contract.source_queue_work.expression, id = "e_queue_work", label = local.async_observability_expression_contract.source_queue_work.label }],
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", local.delivery_dead_letter_name, { id = "m_dlq_visible", label = "Visible DLQ messages", stat = "Maximum" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 16
        y      = 6
        width  = 8
        height = 6
        properties = {
          title   = "Oldest delivery message"
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          period  = local.async_metric_period_seconds
          yAxis = {
            left = { min = 0 }
          }
          metrics = [
            ["AWS/SQS", "ApproximateAgeOfOldestMessage", "QueueName", local.delivery_queue_name, { id = "m_oldest_age", label = "Oldest message (seconds)", stat = "Maximum" }],
          ]
        }
      },
    ]
  })
}
