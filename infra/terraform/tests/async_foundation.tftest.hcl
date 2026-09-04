mock_provider "aws" {
  override_during = plan

  mock_data "aws_availability_zones" {
    defaults = {
      names = ["ap-southeast-3a", "ap-southeast-3b"]
    }
  }

  mock_data "aws_ssm_parameter" {
    defaults = {
      value = "ami-mocked-x86_64"
    }
  }

  mock_data "aws_rds_engine_version" {
    defaults = {
      version_actual = "17.6"
    }
  }

  mock_data "aws_rds_orderable_db_instance" {
    defaults = {
      instance_class = "db.t4g.micro"
    }
  }
}

override_resource {
  target          = aws_ecr_repository.api
  override_during = plan
  values = {
    repository_url = "123456789012.dkr.ecr.ap-southeast-3.amazonaws.com/trackrelay-test-api"
  }
}

override_resource {
  target          = aws_ecr_repository.worker[0]
  override_during = plan
  values = {
    repository_url = "123456789012.dkr.ecr.ap-southeast-3.amazonaws.com/trackrelay-test-worker"
  }
}

override_resource {
  target          = aws_ecr_repository.simulator[0]
  override_during = plan
  values = {
    repository_url = "123456789012.dkr.ecr.ap-southeast-3.amazonaws.com/trackrelay-test-simulator"
  }
}

override_resource {
  target          = aws_sqs_queue.delivery[0]
  override_during = plan
  values = {
    arn = "arn:aws:sqs:ap-southeast-3:123456789012:trackrelay-test-delivery"
    id  = "https://sqs.ap-southeast-3.amazonaws.com/123456789012/trackrelay-test-delivery"
    url = "https://sqs.ap-southeast-3.amazonaws.com/123456789012/trackrelay-test-delivery"
  }
}

override_resource {
  target          = aws_db_instance.postgres
  override_during = plan
  values = {
    address = "trackrelay-test.example.ap-southeast-3.rds.amazonaws.com"
    db_name = "trackrelay"
    master_user_secret = [{
      kms_key_id    = "arn:aws:kms:ap-southeast-3:123456789012:key/mocked"
      secret_arn    = "arn:aws:secretsmanager:ap-southeast-3:123456789012:secret:trackrelay-test"
      secret_status = "active"
    }]
    port     = 5432
    username = "trackrelay_admin"
  }
}

override_resource {
  target          = aws_security_group.async_load_balancer[0]
  override_during = plan
  values = {
    id = "sg-mocked-async-alb"
  }
}

override_resource {
  target          = aws_security_group.async_api[0]
  override_during = plan
  values = {
    id = "sg-mocked-async-api"
  }
}

override_resource {
  target          = aws_security_group.async_worker[0]
  override_during = plan
  values = {
    id = "sg-mocked-async-worker"
  }
}

override_resource {
  target          = aws_security_group.async_migration[0]
  override_during = plan
  values = {
    id = "sg-mocked-async-migration"
  }
}

override_resource {
  target          = aws_security_group.async_simulator[0]
  override_during = plan
  values = {
    id = "sg-mocked-async-simulator"
  }
}

override_resource {
  target          = aws_lb.async[0]
  override_during = plan
  values = {
    arn        = "arn:aws:elasticloadbalancing:ap-southeast-3:123456789012:loadbalancer/app/trackrelay-test-async/0123456789abcdef"
    arn_suffix = "app/trackrelay-test-async/0123456789abcdef"
  }
}

override_resource {
  target          = aws_subnet.async_public[0]
  override_during = plan
  values = {
    id = "subnet-mocked-async-a"
  }
}

override_resource {
  target          = aws_subnet.async_public[1]
  override_during = plan
  values = {
    id = "subnet-mocked-async-b"
  }
}

override_resource {
  target          = aws_sqs_queue.delivery_dead_letter[0]
  override_during = plan
  values = {
    arn = "arn:aws:sqs:ap-southeast-3:123456789012:trackrelay-test-delivery-dlq"
    id  = "https://sqs.ap-southeast-3.amazonaws.com/123456789012/trackrelay-test-delivery-dlq"
    url = "https://sqs.ap-southeast-3.amazonaws.com/123456789012/trackrelay-test-delivery-dlq"
  }
}

variables {
  api_image_digest       = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  api_ingress_cidr       = "203.0.113.10/32"
  async_services_enabled = true
  aws_profile            = "trackrelay-admin"
  aws_region             = "ap-southeast-3"
  deployment_mode        = "async"
  session_id             = "cloud-session-3-20260831T120000Z"
  simulator_image_digest = "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  worker_image_digest    = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
}

run "service_images_have_disposable_encrypted_registries" {
  command = plan

  assert {
    condition = (
      length(aws_instance.rehost) == 0
      && length(aws_iam_role.rehost) == 0
      && length(aws_subnet.rehost_public) == 0
      && length(aws_ecs_cluster.async) == 1
      && length(aws_lb.async) == 1
      && length(aws_sqs_queue.delivery) == 1
    )
    error_message = "Async mode must exclude the synchronous host and include one async foundation."
  }

  assert {
    condition = (
      endswith(aws_ecr_repository.api.name, "-api")
      && endswith(aws_ecr_repository.worker[0].name, "-worker")
      && endswith(aws_ecr_repository.simulator[0].name, "-simulator")
    )
    error_message = "Repository names must match native teardown discovery."
  }

  assert {
    condition = alltrue([
      aws_ecr_repository.api.force_delete,
      aws_ecr_repository.worker[0].force_delete,
      aws_ecr_repository.simulator[0].force_delete,
    ])
    error_message = "Every service repository must be removable with its images."
  }

  assert {
    condition = alltrue([
      aws_ecr_repository.api.image_scanning_configuration[0].scan_on_push,
      aws_ecr_repository.worker[0].image_scanning_configuration[0].scan_on_push,
      aws_ecr_repository.simulator[0].image_scanning_configuration[0].scan_on_push,
    ])
    error_message = "Every service repository must scan images on push."
  }

  assert {
    condition = alltrue([
      aws_ecr_repository.api.encryption_configuration[0].encryption_type == "AES256",
      aws_ecr_repository.worker[0].encryption_configuration[0].encryption_type == "AES256",
      aws_ecr_repository.simulator[0].encryption_configuration[0].encryption_type == "AES256",
    ])
    error_message = "Every service repository must encrypt images at rest."
  }
}

run "delivery_queue_retries_to_one_restricted_dlq" {
  command = plan

  assert {
    condition = (
      endswith(aws_sqs_queue.delivery[0].name, "-delivery")
      && endswith(aws_sqs_queue.delivery_dead_letter[0].name, "-delivery-dlq")
    )
    error_message = "Queue names must match native teardown discovery."
  }

  assert {
    condition = (
      aws_sqs_queue.delivery[0].sqs_managed_sse_enabled
      && aws_sqs_queue.delivery_dead_letter[0].sqs_managed_sse_enabled
    )
    error_message = "The source queue and DLQ must use SQS-managed encryption."
  }

  assert {
    condition = (
      aws_sqs_queue.delivery[0].delay_seconds == 0
      && aws_sqs_queue.delivery[0].receive_wait_time_seconds == 20
      && aws_sqs_queue.delivery[0].visibility_timeout_seconds == 120
    )
    error_message = "The source queue must match the worker polling and visibility contract."
  }

  assert {
    condition = (
      aws_sqs_queue.delivery[0].message_retention_seconds == 86400
      && aws_sqs_queue.delivery_dead_letter[0].message_retention_seconds == 345600
    )
    error_message = "The DLQ must retain failure evidence longer than the source queue."
  }

  assert {
    condition = (
      jsondecode(aws_sqs_queue.delivery[0].redrive_policy).deadLetterTargetArn
      == aws_sqs_queue.delivery_dead_letter[0].arn
      && jsondecode(aws_sqs_queue.delivery[0].redrive_policy).maxReceiveCount == 5
    )
    error_message = "The source queue must retry five receives before using its exact DLQ."
  }

  assert {
    condition = (
      jsondecode(aws_sqs_queue_redrive_allow_policy.delivery[0].redrive_allow_policy).redrivePermission
      == "byQueue"
      && jsondecode(aws_sqs_queue_redrive_allow_policy.delivery[0].redrive_allow_policy).sourceQueueArns
      == [aws_sqs_queue.delivery[0].arn]
    )
    error_message = "Only the TrackRelay source queue may use the delivery DLQ."
  }
}

run "async_network_exposes_only_the_load_balancer" {
  command = plan

  assert {
    condition = (
      length(aws_subnet.async_public) == 2
      && aws_subnet.async_public[0].availability_zone != aws_subnet.async_public[1].availability_zone
      && alltrue([for subnet in aws_subnet.async_public : subnet.map_public_ip_on_launch])
    )
    error_message = "The cost-bounded Fargate network must use two public Availability Zones."
  }

  assert {
    condition = (
      !aws_lb.async[0].internal
      && aws_lb.async[0].load_balancer_type == "application"
      && !aws_lb.async[0].enable_deletion_protection
      && aws_lb.async[0].drop_invalid_header_fields
      && length(aws_lb.async[0].subnets) == 2
    )
    error_message = "The disposable public ALB must span both asynchronous subnets."
  }

  assert {
    condition = (
      one(aws_security_group.async_load_balancer[0].ingress).from_port == 80
      && one(aws_security_group.async_load_balancer[0].ingress).cidr_blocks == tolist(["203.0.113.10/32"])
    )
    error_message = "Only HTTP from the approved benchmark address may reach the ALB."
  }

  assert {
    condition = (
      one(aws_security_group.async_load_balancer[0].egress).from_port == 8000
      && one(aws_security_group.async_load_balancer[0].egress).to_port == 8000
      && one(aws_security_group.async_load_balancer[0].egress).cidr_blocks == tolist([aws_vpc.rehost.cidr_block])
    )
    error_message = "The ALB may send only API-port traffic inside the experiment VPC."
  }

  assert {
    condition = (
      one(aws_security_group.async_api[0].ingress).from_port == 8000
      && one(aws_security_group.async_api[0].ingress).cidr_blocks == null
      && one(aws_security_group.async_api[0].ingress).security_groups == toset(["sg-mocked-async-alb"])
    )
    error_message = "Only the ALB security group may reach API tasks."
  }

  assert {
    condition = (
      length(aws_security_group.async_worker[0].ingress) == 0
      && length(aws_security_group.async_migration[0].ingress) == 0
      && length(aws_security_group.async_simulator[0].ingress) == 2
      && toset(flatten([for rule in aws_security_group.async_simulator[0].ingress : rule.security_groups]))
      == toset(["sg-mocked-async-api", "sg-mocked-async-worker"])
      && alltrue([for rule in aws_security_group.async_simulator[0].ingress : rule.from_port == 8001 && rule.to_port == 8001])
    )
    error_message = "Workers and migrations need no ingress, and only workers plus the API evidence path may reach the simulator."
  }

  assert {
    condition = (
      aws_lb_listener.async_http[0].port == 80
      && aws_lb_listener.async_http[0].protocol == "HTTP"
      && aws_lb_target_group.async_api[0].target_type == "ip"
      && one(aws_lb_target_group.async_api[0].health_check).path == "/health/ready"
    )
    error_message = "The ALB must route HTTP to ready Fargate task IPs."
  }
}

run "async_platform_has_bounded_logs_and_least_privilege_roles" {
  command = plan

  assert {
    condition = (
      one(aws_ecs_cluster.async[0].setting).name == "containerInsights"
      && one(aws_ecs_cluster.async[0].setting).value == "enabled"
    )
    error_message = "The asynchronous cluster must emit Container Insights metrics."
  }

  assert {
    condition = (
      toset(keys(aws_cloudwatch_log_group.async)) == toset(["api", "migration", "simulator", "worker"])
      && alltrue([for group in aws_cloudwatch_log_group.async : group.retention_in_days == 1 && !group.skip_destroy])
    )
    error_message = "Every task role needs a one-day, destroyable log group."
  }

  assert {
    condition = (
      length(aws_cloudwatch_log_group.async_performance) == 1
      && aws_cloudwatch_log_group.async_performance[0].name
      == "/aws/ecs/containerinsights/${local.name_prefix}-async/performance"
      && aws_cloudwatch_log_group.async_performance[0].retention_in_days == 1
      && !aws_cloudwatch_log_group.async_performance[0].skip_destroy
    )
    error_message = "Container Insights performance logs must be session-owned, bounded, and destroyed."
  }

  assert {
    condition = (
      jsondecode(local.ecs_task_assume_role_policy).Statement[0].Principal.Service
      == "ecs-tasks.amazonaws.com"
      && aws_iam_role_policy_attachment.ecs_execution[0].policy_arn
      == "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
    )
    error_message = "Fargate roles must trust only ECS tasks and use the standard execution policy."
  }

  assert {
    condition = (
      jsondecode(aws_iam_role_policy.ecs_execution_database_secret[0].policy).Statement[0].Action
      == ["secretsmanager:GetSecretValue"]
      && jsondecode(aws_iam_role_policy.ecs_execution_database_secret[0].policy).Statement[0].Resource
      == aws_db_instance.postgres.master_user_secret[0].secret_arn
    )
    error_message = "The execution role may read only the RDS-managed database secret."
  }

  assert {
    condition = (
      jsondecode(aws_iam_role_policy.api_queue[0].policy).Statement[0].Action
      == ["sqs:SendMessage"]
      && toset(jsondecode(aws_iam_role_policy.worker_queue[0].policy).Statement[0].Action)
      == toset(["sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ReceiveMessage", "sqs:SendMessage"])
    )
    error_message = "API and worker queue permissions must stay role-specific."
  }

  assert {
    condition = (
      jsondecode(aws_iam_role_policy.api_queue[0].policy).Statement[1].Action == ["cloudwatch:PutMetricData"]
      && jsondecode(aws_iam_role_policy.api_queue[0].policy).Statement[1].Condition.StringEquals["cloudwatch:namespace"] == "TrackRelay/Elasticity"
    )
    error_message = "API telemetry may publish only into the dedicated scaling namespace."
  }
}

run "async_observability_has_one_native_metric_contract" {
  command = plan

  assert {
    condition = (
      toset(keys(output.async_observability_dimensions)) == toset([
        "api_service_name",
        "cluster_name",
        "dashboard_name",
        "dead_letter_queue_name",
        "delivery_queue_name",
        "load_balancer_dimension",
        "rds_identifier",
        "simulator_service_name",
        "worker_service_name",
        "scaling_metrics_namespace",
      ])
      && output.async_observability_dimensions.api_service_name == "trackrelay-8a7e37db-api"
      && output.async_observability_dimensions.cluster_name == "trackrelay-8a7e37db-async"
      && output.async_observability_dimensions.dashboard_name == "trackrelay-8a7e37db-async"
      && output.async_observability_dimensions.dead_letter_queue_name == "trackrelay-8a7e37db-delivery-dlq"
      && output.async_observability_dimensions.delivery_queue_name == "trackrelay-8a7e37db-delivery"
      && output.async_observability_dimensions.load_balancer_dimension == "app/trackrelay-test-async/0123456789abcdef"
      && output.async_observability_dimensions.rds_identifier == "trackrelay-8a7e37db-postgres"
      && output.async_observability_dimensions.simulator_service_name == "trackrelay-8a7e37db-simulator"
      && output.async_observability_dimensions.worker_service_name == "trackrelay-8a7e37db-worker"
    )
    error_message = "CloudWatch collection must receive the exact non-secret session dimensions."
  }

  assert {
    condition = (
      local.async_metric_period_seconds == 60
      && toset(keys(local.async_observability_metric_contract)) == toset([
        "alb_elb_4xx",
        "alb_elb_5xx",
        "alb_request_count",
        "alb_target_4xx",
        "alb_target_5xx",
        "api_cpu_utilization",
        "api_memory_utilization",
        "api_p95_latency",
        "dead_letter_queue_visible",
        "source_queue_delayed",
        "source_queue_in_flight",
        "source_queue_oldest_age",
        "source_queue_visible",
        "simulator_cpu_utilization",
        "simulator_memory_utilization",
        "worker_cpu_utilization",
        "worker_running_tasks",
      ])
    )
    error_message = "The asynchronous experiment must use one explicit 60-second native metric contract."
  }

  assert {
    condition = (
      local.async_observability_metric_contract.api_p95_latency == {
        dimensions  = ["LoadBalancer"]
        metric_name = "TargetResponseTime"
        namespace   = "AWS/ApplicationELB"
        statistic   = "p95"
      }
      && local.async_observability_metric_contract.worker_running_tasks == {
        dimensions  = ["ClusterName", "ServiceName"]
        metric_name = "RunningTaskCount"
        namespace   = "ECS/ContainerInsights"
        statistic   = "Average"
      }
      && local.async_observability_metric_contract.worker_cpu_utilization == {
        dimensions  = ["ClusterName", "ServiceName"]
        metric_name = "CPUUtilization"
        namespace   = "AWS/ECS"
        statistic   = "Maximum"
      }
      && local.async_observability_metric_contract.api_cpu_utilization == {
        dimensions  = ["ClusterName", "ServiceName"]
        metric_name = "CPUUtilization"
        namespace   = "AWS/ECS"
        statistic   = "Maximum"
      }
      && local.async_observability_metric_contract.api_memory_utilization == {
        dimensions  = ["ClusterName", "ServiceName"]
        metric_name = "MemoryUtilization"
        namespace   = "AWS/ECS"
        statistic   = "Maximum"
      }
      && local.async_observability_metric_contract.simulator_cpu_utilization == {
        dimensions  = ["ClusterName", "ServiceName"]
        metric_name = "CPUUtilization"
        namespace   = "AWS/ECS"
        statistic   = "Maximum"
      }
      && local.async_observability_metric_contract.simulator_memory_utilization == {
        dimensions  = ["ClusterName", "ServiceName"]
        metric_name = "MemoryUtilization"
        namespace   = "AWS/ECS"
        statistic   = "Maximum"
      }
      && local.async_observability_metric_contract.source_queue_oldest_age == {
        dimensions  = ["QueueName"]
        metric_name = "ApproximateAgeOfOldestMessage"
        namespace   = "AWS/SQS"
        statistic   = "Maximum"
      }
    )
    error_message = "Latency, worker-count, and message-age metrics must retain their native namespaces, dimensions, and statistics."
  }

  assert {
    condition = (
      local.async_observability_expression_contract.observed_request_rate.expression
      == "(m_requests + m_elb_4xx + m_elb_5xx) / 60"
      && local.async_observability_expression_contract.request_error_percentage.expression
      == "IF((m_requests + m_elb_4xx + m_elb_5xx) > 0, 100 * (m_target_4xx + m_target_5xx + m_elb_4xx + m_elb_5xx) / (m_requests + m_elb_4xx + m_elb_5xx), 0)"
      && local.async_observability_expression_contract.source_queue_work.expression
      == "m_visible + m_in_flight + m_delayed"
    )
    error_message = "Rate, error, and unfinished-work expressions must remain comparable across fixed and elastic runs."
  }

  assert {
    condition = (
      length(aws_cloudwatch_dashboard.async) == 1
      && endswith(aws_cloudwatch_dashboard.async[0].dashboard_name, "-async")
      && jsondecode(aws_cloudwatch_dashboard.async[0].dashboard_body).start == "-PT1H"
      && jsondecode(aws_cloudwatch_dashboard.async[0].dashboard_body).periodOverride == "inherit"
      && length(jsondecode(aws_cloudwatch_dashboard.async[0].dashboard_body).widgets) == 7
      && alltrue([
        for widget in jsondecode(aws_cloudwatch_dashboard.async[0].dashboard_body).widgets : (
          widget.type == "metric"
          && widget.properties.period == 60
          && widget.properties.region == "ap-southeast-3"
        )
      ])
    )
    error_message = "Async mode must create one compact seven-panel dashboard at the contract's native period."
  }

  assert {
    condition = (
      jsondecode(aws_cloudwatch_dashboard.async[0].dashboard_body).widgets[0].properties.metrics[3][0].expression
      == local.async_observability_expression_contract.observed_request_rate.expression
      && jsondecode(aws_cloudwatch_dashboard.async[0].dashboard_body).widgets[1].properties.metrics[0][1]
      == "TargetResponseTime"
      && jsondecode(aws_cloudwatch_dashboard.async[0].dashboard_body).widgets[1].properties.metrics[0][4].stat
      == "p95"
      && jsondecode(aws_cloudwatch_dashboard.async[0].dashboard_body).widgets[1].properties.annotations.horizontal[0].value
      == 0.5
      && jsondecode(aws_cloudwatch_dashboard.async[0].dashboard_body).widgets[2].properties.annotations.horizontal[0].value
      == 1
    )
    error_message = "The API panels must expose request rate, p95 latency, request errors, and both frozen SLO lines."
  }

  assert {
    condition = (
      jsondecode(aws_cloudwatch_dashboard.async[0].dashboard_body).widgets[3].properties.metrics[0][0]
      == "ECS/ContainerInsights"
      && jsondecode(aws_cloudwatch_dashboard.async[0].dashboard_body).widgets[3].properties.metrics[0][1]
      == "RunningTaskCount"
      && jsondecode(aws_cloudwatch_dashboard.async[0].dashboard_body).widgets[3].properties.metrics[0][3]
      == "trackrelay-8a7e37db-async"
      && jsondecode(aws_cloudwatch_dashboard.async[0].dashboard_body).widgets[3].properties.metrics[0][5]
      == "trackrelay-8a7e37db-worker"
      && jsondecode(aws_cloudwatch_dashboard.async[0].dashboard_body).widgets[4].properties.metrics[3][0].expression
      == local.async_observability_expression_contract.source_queue_work.expression
      && jsondecode(aws_cloudwatch_dashboard.async[0].dashboard_body).widgets[5].properties.metrics[0][1]
      == "ApproximateAgeOfOldestMessage"
      && jsondecode(aws_cloudwatch_dashboard.async[0].dashboard_body).widgets[6].properties.metrics[0][1]
      == "CPUUtilization"
      && jsondecode(aws_cloudwatch_dashboard.async[0].dashboard_body).widgets[6].properties.metrics[1][1]
      == "MemoryUtilization"
      && jsondecode(aws_cloudwatch_dashboard.async[0].dashboard_body).widgets[6].properties.metrics[2][5]
      == "trackrelay-8a7e37db-simulator"
      && jsondecode(aws_cloudwatch_dashboard.async[0].dashboard_body).widgets[6].properties.metrics[3][1]
      == "MemoryUtilization"
      && jsondecode(aws_cloudwatch_dashboard.async[0].dashboard_body).widgets[6].properties.annotations.horizontal[0].value
      == 70
    )
    error_message = "The processing panels must align worker count, queue state, message age, and fixed API headroom."
  }
}

run "async_task_definitions_are_digest_pinned_fargate_contracts" {
  command = plan

  assert {
    condition = alltrue([
      for definition in [aws_ecs_task_definition.async_api[0], aws_ecs_task_definition.async_simulator[0]] : (
        jsondecode(definition.container_definitions)[0].healthCheck.timeout == 5
        && jsondecode(definition.container_definitions)[0].healthCheck.startPeriod == 30
        && jsondecode(definition.container_definitions)[0].healthCheck.retries == 3
        && jsondecode(definition.container_definitions)[0].healthCheck.command[3] == "trackrelay.healthcheck"
      )
    ])
    error_message = "HTTP probes need startup/CPU margin and must match the image probe contract."
  }

  assert {
    condition = alltrue([
      for definition in [
        aws_ecs_task_definition.async_api[0],
        aws_ecs_task_definition.async_worker[0],
        aws_ecs_task_definition.async_simulator[0],
        aws_ecs_task_definition.async_migration[0],
        ] : (
        definition.network_mode == "awsvpc"
        && definition.requires_compatibilities == toset(["FARGATE"])
        && one(definition.runtime_platform).cpu_architecture == "X86_64"
        && one(definition.runtime_platform).operating_system_family == "LINUX"
        && !definition.skip_destroy
        && !definition.track_latest
      )
    ])
    error_message = "Every runtime role must use a pinned, destroyable x86_64 Fargate definition."
  }

  assert {
    condition = (
      jsondecode(aws_ecs_task_definition.async_api[0].container_definitions)[0].image
      == "123456789012.dkr.ecr.ap-southeast-3.amazonaws.com/trackrelay-test-api@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      && jsondecode(aws_ecs_task_definition.async_worker[0].container_definitions)[0].image
      == "123456789012.dkr.ecr.ap-southeast-3.amazonaws.com/trackrelay-test-worker@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      && jsondecode(aws_ecs_task_definition.async_simulator[0].container_definitions)[0].image
      == "123456789012.dkr.ecr.ap-southeast-3.amazonaws.com/trackrelay-test-simulator@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
      && jsondecode(aws_ecs_task_definition.async_migration[0].container_definitions)[0].image
      == "123456789012.dkr.ecr.ap-southeast-3.amazonaws.com/trackrelay-test-api@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    )
    error_message = "Every role must reference its exact ECR repository and immutable image digest."
  }

  assert {
    condition = alltrue([
      for definition in [
        aws_ecs_task_definition.async_api[0],
        aws_ecs_task_definition.async_worker[0],
        aws_ecs_task_definition.async_simulator[0],
        aws_ecs_task_definition.async_migration[0],
        ] : (
        jsondecode(definition.container_definitions)[0].user == "10001:10001"
        && jsondecode(definition.container_definitions)[0].readonlyRootFilesystem
        && jsondecode(definition.container_definitions)[0].linuxParameters.initProcessEnabled
        && jsondecode(definition.container_definitions)[0].mountPoints[0].containerPath == "/tmp"
        && jsondecode(definition.container_definitions)[0].logConfiguration.logDriver == "awslogs"
      )
    ])
    error_message = "Every container must remain non-root, read-only, init-enabled, writable only at /tmp, and log through awslogs."
  }

  assert {
    condition = (
      aws_ecs_task_definition.async_api[0].cpu == "1024"
      && aws_ecs_task_definition.async_api[0].memory == "2048"
      && alltrue([
        for definition in [
          aws_ecs_task_definition.async_worker[0],
          aws_ecs_task_definition.async_simulator[0],
          aws_ecs_task_definition.async_migration[0],
        ] : definition.cpu == "256" && definition.memory == "512"
      ])
      && jsondecode(aws_ecs_task_definition.async_migration[0].container_definitions)[0].command
      == ["alembic", "upgrade", "head"]
    )
    error_message = "The API must retain its fixed headroom tier while supporting roles stay cost-bounded and migration remains one-shot."
  }
}

run "async_runtime_injects_only_the_password_and_discovers_the_simulator" {
  command = plan

  assert {
    condition = (
      aws_service_discovery_private_dns_namespace.async[0].name
      == "trackrelay-8a7e37db.internal"
      && aws_service_discovery_service.simulator[0].name == "simulator"
      && one(aws_service_discovery_service.simulator[0].dns_config).routing_policy
      == "MULTIVALUE"
      && one(one(aws_service_discovery_service.simulator[0].dns_config).dns_records).type
      == "A"
      && one(one(aws_service_discovery_service.simulator[0].dns_config).dns_records).ttl
      == 10
      && length(aws_service_discovery_service.simulator[0].health_check_custom_config)
      == 0
    )
    error_message = "The worker must discover replaceable simulator task IPs through stable private Cloud Map DNS."
  }

  assert {
    condition = (
      { for item in jsondecode(aws_ecs_task_definition.async_api[0].container_definitions)[0].environment : item.name => item.value }["TRACKRELAY_DATABASE_SSLMODE"]
      == "require"
      && { for item in jsondecode(aws_ecs_task_definition.async_api[0].container_definitions)[0].environment : item.name => item.value }["TRACKRELAY_DELIVERY_QUEUE_BACKEND"]
      == "sqs"
      && { for item in jsondecode(aws_ecs_task_definition.async_api[0].container_definitions)[0].environment : item.name => item.value }["TRACKRELAY_DOWNSTREAM_URL"]
      == "http://simulator.trackrelay-8a7e37db.internal:8001"
      && { for item in jsondecode(aws_ecs_task_definition.async_worker[0].container_definitions)[0].environment : item.name => item.value }["TRACKRELAY_DOWNSTREAM_URL"]
      == "http://simulator.trackrelay-8a7e37db.internal:8001"
    )
    error_message = "API and worker runtime environment must select TLS, SQS, and private simulator discovery."
  }

  assert {
    condition = alltrue([
      for definition in [
        aws_ecs_task_definition.async_api[0],
        aws_ecs_task_definition.async_worker[0],
        aws_ecs_task_definition.async_migration[0],
        ] : (
        length(jsondecode(definition.container_definitions)[0].secrets) == 1
        && jsondecode(definition.container_definitions)[0].secrets[0].name == "TRACKRELAY_DATABASE_PASSWORD"
        && jsondecode(definition.container_definitions)[0].secrets[0].valueFrom
        == "arn:aws:secretsmanager:ap-southeast-3:123456789012:secret:trackrelay-test:password::"
        && !contains(
          [for item in jsondecode(definition.container_definitions)[0].environment : item.name],
          "TRACKRELAY_DATABASE_PASSWORD",
        )
      )
    ])
    error_message = "Database consumers must receive only the password through ECS secret injection."
  }
}

run "async_services_are_fixed_and_start_only_when_enabled" {
  command = plan

  assert {
    condition = toset(output.async_service_names) == toset([
      aws_ecs_service.async_api[0].name,
      aws_ecs_service.async_simulator[0].name,
      aws_ecs_service.async_worker[0].name,
    ])
    error_message = "The deployment controller must receive exactly the three fixed service names."
  }

  assert {
    condition = (
      toset(keys(output.async_service_capacity)) == toset(["api", "simulator", "worker"])
      && output.async_service_capacity.api.cpu_units == 1024
      && output.async_service_capacity.api.desired_count == 2
      && output.async_service_capacity.api.memory_mib == 2048
      && output.async_service_capacity.api.service_name == aws_ecs_service.async_api[0].name
      && output.async_service_capacity.simulator.cpu_units == 256
      && output.async_service_capacity.simulator.desired_count == 1
      && output.async_service_capacity.simulator.memory_mib == 512
      && output.async_service_capacity.simulator.service_name == aws_ecs_service.async_simulator[0].name
      && output.async_service_capacity.worker.cpu_units == 256
      && output.async_service_capacity.worker.desired_count == 1
      && output.async_service_capacity.worker.memory_mib == 512
      && output.async_service_capacity.worker.service_name == aws_ecs_service.async_worker[0].name
    )
    error_message = "The controller must receive the exact fixed capacity for every service."
  }

  assert {
    condition = (
      aws_ecs_service.async_api[0].desired_count == 2
      && aws_ecs_service.async_worker[0].desired_count == 1
      && aws_ecs_service.async_simulator[0].desired_count == 1
      && local.async_fixed_service_counts == {
        api       = 2
        simulator = 1
        worker    = 1
      }
    )
    error_message = "API capacity must remain fixed at two replicas while worker and simulator remain at one."
  }

  assert {
    condition = alltrue([
      for service in [
        aws_ecs_service.async_api[0],
        aws_ecs_service.async_worker[0],
        aws_ecs_service.async_simulator[0],
        ] : (
        service.launch_type == "FARGATE"
        && service.platform_version == "1.4.0"
        && service.deployment_minimum_healthy_percent == 0
        && service.deployment_maximum_percent == 100
        && one(service.network_configuration).assign_public_ip
        && !service.enable_execute_command
        && service.force_delete
        && !service.wait_for_steady_state
        && one(service.deployment_circuit_breaker).enable
        && one(service.deployment_circuit_breaker).rollback
      )
    ])
    error_message = "Each fixed-capacity service must retain guarded Fargate replacement and teardown."
  }

  assert {
    condition = (
      one(aws_ecs_service.async_api[0].load_balancer).container_name == "api"
      && one(aws_ecs_service.async_api[0].load_balancer).container_port == 8000
      && one(aws_ecs_service.async_api[0].network_configuration).security_groups
      == toset(["sg-mocked-async-api"])
      && one(aws_ecs_service.async_worker[0].network_configuration).security_groups
      == toset(["sg-mocked-async-worker"])
      && one(aws_ecs_service.async_simulator[0].network_configuration).security_groups
      == toset(["sg-mocked-async-simulator"])
      && length(aws_ecs_service.async_simulator[0].service_registries) == 1
    )
    error_message = "Services must retain their exact ingress and discovery boundaries."
  }

  assert {
    condition = (
      { for item in jsondecode(aws_ecs_task_definition.async_api[0].container_definitions)[0].environment : item.name => item.value }["TRACKRELAY_SCALING_METRICS_QUEUE_NAME"] == aws_sqs_queue.delivery[0].name
    )
    error_message = "The publisher must be present before the policy-only transition, including fixed deployments."
  }
}

run "worker_elasticity_policy_is_bounded_and_queue_driven" {
  command = plan

  variables {
    worker_autoscaling_enabled = true
  }

  assert {
    condition = (
      local.worker_autoscaling_policy.minimum_capacity == 1
      && local.worker_autoscaling_policy.maximum_capacity == 8
      && local.worker_autoscaling_policy.policy_version == 4
      && local.worker_autoscaling_policy.scale_out_messages_per_minute == null
      && local.worker_autoscaling_policy.scale_out_period_seconds == 10
      && local.worker_autoscaling_policy.scale_out_rate_per_second == 3
      && local.worker_autoscaling_policy.scale_in_messages_per_minute == 120
      && local.worker_autoscaling_policy.scale_in_queue_work_threshold == 10
      && local.worker_autoscaling_policy.metric_period_seconds == 60
      && local.worker_autoscaling_policy.scale_out_evaluation_periods == 2
      && local.worker_autoscaling_policy.scale_in_evaluation_periods == 3
      && local.worker_autoscaling_policy.scale_out_cooldown_seconds == 60
      && local.worker_autoscaling_policy.scale_in_cooldown_seconds == 60
    )
    error_message = "Worker elasticity must retain its frozen v4 bounds, thresholds, periods, and cooldowns."
  }

  assert {
    condition = (
      length(aws_appautoscaling_target.async_worker) == 1
      && aws_appautoscaling_target.async_worker[0].min_capacity == 1
      && aws_appautoscaling_target.async_worker[0].max_capacity == 8
      && aws_appautoscaling_target.async_worker[0].resource_id == "service/trackrelay-8a7e37db-async/trackrelay-8a7e37db-worker"
      && aws_appautoscaling_target.async_worker[0].service_namespace == "ecs"
      && aws_appautoscaling_target.async_worker[0].scalable_dimension == "ecs:service:DesiredCount"
    )
    error_message = "Only the worker service may scale between one and eight tasks."
  }

  assert {
    condition = alltrue([
      for policy in [
        aws_appautoscaling_policy.async_worker_scale_out[0],
        aws_appautoscaling_policy.async_worker_scale_in[0],
        ] : (
        policy.policy_type == "StepScaling"
        && one(policy.step_scaling_policy_configuration).adjustment_type == "ExactCapacity"
        && one(policy.step_scaling_policy_configuration).cooldown == 60
        && one(policy.step_scaling_policy_configuration).metric_aggregation_type == "Maximum"
        && tonumber(one(one(policy.step_scaling_policy_configuration).step_adjustment).metric_interval_lower_bound) == 0
      )
    ])
    error_message = "Both worker policy directions must use exact bounded capacity and one-minute cooldowns."
  }

  assert {
    condition = (
      aws_cloudwatch_metric_alarm.async_worker_backlog_high[0].metric_name == "ArrivalRate"
      && aws_cloudwatch_metric_alarm.async_worker_backlog_high[0].actions_enabled
      && aws_cloudwatch_metric_alarm.async_worker_backlog_high[0].namespace == "TrackRelay/Elasticity"
      && aws_cloudwatch_metric_alarm.async_worker_backlog_high[0].statistic == "Maximum"
      && aws_cloudwatch_metric_alarm.async_worker_backlog_high[0].period == 10
      && aws_cloudwatch_metric_alarm.async_worker_backlog_high[0].threshold == 3
      && aws_cloudwatch_metric_alarm.async_worker_backlog_high[0].evaluation_periods == 2
      && aws_cloudwatch_metric_alarm.async_worker_backlog_high[0].datapoints_to_alarm == 2
      && aws_cloudwatch_metric_alarm.async_worker_backlog_high[0].treat_missing_data == "notBreaching"
      && aws_cloudwatch_metric_alarm.async_worker_backlog_high[0].dimensions.QueueName == "trackrelay-8a7e37db-delivery"
      && length(aws_cloudwatch_metric_alarm.async_worker_backlog_high[0].alarm_actions) == 1
    )
    error_message = "Scale-out must use two ten-second high-resolution global arrival-rate periods."
  }

  assert {
    condition = (
      aws_cloudwatch_metric_alarm.async_worker_empty[0].actions_enabled
      && aws_cloudwatch_metric_alarm.async_worker_empty[0].comparison_operator == "GreaterThanOrEqualToThreshold"
      && aws_cloudwatch_metric_alarm.async_worker_empty[0].threshold == 1
      && aws_cloudwatch_metric_alarm.async_worker_empty[0].evaluation_periods == 3
      && aws_cloudwatch_metric_alarm.async_worker_empty[0].datapoints_to_alarm == 3
      && aws_cloudwatch_metric_alarm.async_worker_empty[0].treat_missing_data == "notBreaching"
      && length(aws_cloudwatch_metric_alarm.async_worker_empty[0].alarm_actions) == 1
      && length(aws_cloudwatch_metric_alarm.async_worker_empty[0].metric_query) == 5
      && one([for query in aws_cloudwatch_metric_alarm.async_worker_empty[0].metric_query : query if query.id == "release_safe"]).expression == "IF(FILL(sent, 0) < 120, IF(FILL(visible, 0) + FILL(in_flight, 0) + FILL(delayed, 0) < 10, 1, 0), 0)"
      && one([for query in aws_cloudwatch_metric_alarm.async_worker_empty[0].metric_query : query if query.id == "release_safe"]).return_data
      && one(one([for query in aws_cloudwatch_metric_alarm.async_worker_empty[0].metric_query : query if query.id == "sent"]).metric).metric_name == "NumberOfMessagesSent"
      && one(one([for query in aws_cloudwatch_metric_alarm.async_worker_empty[0].metric_query : query if query.id == "sent"]).metric).stat == "Sum"
      && one(one([for query in aws_cloudwatch_metric_alarm.async_worker_empty[0].metric_query : query if query.id == "visible"]).metric).metric_name == "ApproximateNumberOfMessagesVisible"
      && one(one([for query in aws_cloudwatch_metric_alarm.async_worker_empty[0].metric_query : query if query.id == "in_flight"]).metric).metric_name == "ApproximateNumberOfMessagesNotVisible"
      && one(one([for query in aws_cloudwatch_metric_alarm.async_worker_empty[0].metric_query : query if query.id == "delayed"]).metric).metric_name == "ApproximateNumberOfMessagesDelayed"
    )
    error_message = "Scale-in must require three low-demand, low-queue-work native SQS periods."
  }

  assert {
    condition = (
      output.worker_autoscaling_policy.minimum_capacity == 1
      && output.worker_autoscaling_policy.maximum_capacity == 8
      && output.worker_autoscaling_policy.policy_version == 4
      && output.worker_autoscaling_policy.scale_out_messages_per_minute == null
      && local.worker_autoscaling_policy.scale_out_period_seconds == 10
      && local.worker_autoscaling_policy.scale_out_rate_per_second == 3
      && output.worker_autoscaling_policy.scale_in_messages_per_minute == 120
      && output.worker_autoscaling_policy.scale_in_queue_work_threshold == 10
      && output.worker_autoscaling_policy.queue_name == "trackrelay-8a7e37db-delivery"
      && output.worker_autoscaling_policy.resource_id == "service/trackrelay-8a7e37db-async/trackrelay-8a7e37db-worker"
    )
    error_message = "The transition controller must receive the frozen worker policy contract."
  }
}

run "partial_image_configuration_is_rejected" {
  command = plan

  variables {
    async_services_enabled = false
    simulator_image_digest = ""
    worker_image_digest    = ""
  }

  expect_failures = [check.async_image_digests_are_all_set_or_all_empty]
}

run "services_without_registered_runtime_are_rejected" {
  command = plan

  variables {
    api_image_digest       = ""
    async_services_enabled = true
    simulator_image_digest = ""
    worker_image_digest    = ""
  }

  expect_failures = [check.async_services_require_runtime]
}
