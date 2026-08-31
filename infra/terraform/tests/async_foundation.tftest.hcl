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
  target          = aws_sqs_queue.delivery
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
    master_user_secret = [{
      kms_key_id    = "arn:aws:kms:ap-southeast-3:123456789012:key/mocked"
      secret_arn    = "arn:aws:secretsmanager:ap-southeast-3:123456789012:secret:trackrelay-test"
      secret_status = "active"
    }]
  }
}

override_resource {
  target          = aws_security_group.async_load_balancer
  override_during = plan
  values = {
    id = "sg-mocked-async-alb"
  }
}

override_resource {
  target          = aws_security_group.async_worker
  override_during = plan
  values = {
    id = "sg-mocked-async-worker"
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
  target          = aws_sqs_queue.delivery_dead_letter
  override_during = plan
  values = {
    arn = "arn:aws:sqs:ap-southeast-3:123456789012:trackrelay-test-delivery-dlq"
    id  = "https://sqs.ap-southeast-3.amazonaws.com/123456789012/trackrelay-test-delivery-dlq"
    url = "https://sqs.ap-southeast-3.amazonaws.com/123456789012/trackrelay-test-delivery-dlq"
  }
}

variables {
  api_ingress_cidr = "203.0.113.10/32"
  aws_profile      = "trackrelay-admin"
  aws_region       = "ap-southeast-3"
  session_id       = "cloud-session-3-20260831T120000Z"
}

run "service_images_have_disposable_encrypted_registries" {
  command = plan

  assert {
    condition = (
      endswith(aws_ecr_repository.api.name, "-api")
      && endswith(aws_ecr_repository.worker.name, "-worker")
      && endswith(aws_ecr_repository.simulator.name, "-simulator")
    )
    error_message = "Repository names must match native teardown discovery."
  }

  assert {
    condition = alltrue([
      aws_ecr_repository.api.force_delete,
      aws_ecr_repository.worker.force_delete,
      aws_ecr_repository.simulator.force_delete,
    ])
    error_message = "Every service repository must be removable with its images."
  }

  assert {
    condition = alltrue([
      aws_ecr_repository.api.image_scanning_configuration[0].scan_on_push,
      aws_ecr_repository.worker.image_scanning_configuration[0].scan_on_push,
      aws_ecr_repository.simulator.image_scanning_configuration[0].scan_on_push,
    ])
    error_message = "Every service repository must scan images on push."
  }

  assert {
    condition = alltrue([
      aws_ecr_repository.api.encryption_configuration[0].encryption_type == "AES256",
      aws_ecr_repository.worker.encryption_configuration[0].encryption_type == "AES256",
      aws_ecr_repository.simulator.encryption_configuration[0].encryption_type == "AES256",
    ])
    error_message = "Every service repository must encrypt images at rest."
  }
}

run "delivery_queue_retries_to_one_restricted_dlq" {
  command = plan

  assert {
    condition = (
      endswith(aws_sqs_queue.delivery.name, "-delivery")
      && endswith(aws_sqs_queue.delivery_dead_letter.name, "-delivery-dlq")
    )
    error_message = "Queue names must match native teardown discovery."
  }

  assert {
    condition = (
      aws_sqs_queue.delivery.sqs_managed_sse_enabled
      && aws_sqs_queue.delivery_dead_letter.sqs_managed_sse_enabled
    )
    error_message = "The source queue and DLQ must use SQS-managed encryption."
  }

  assert {
    condition = (
      aws_sqs_queue.delivery.delay_seconds == 0
      && aws_sqs_queue.delivery.receive_wait_time_seconds == 20
      && aws_sqs_queue.delivery.visibility_timeout_seconds == 120
    )
    error_message = "The source queue must match the worker polling and visibility contract."
  }

  assert {
    condition = (
      aws_sqs_queue.delivery.message_retention_seconds == 86400
      && aws_sqs_queue.delivery_dead_letter.message_retention_seconds == 345600
    )
    error_message = "The DLQ must retain failure evidence longer than the source queue."
  }

  assert {
    condition = (
      jsondecode(aws_sqs_queue.delivery.redrive_policy).deadLetterTargetArn
      == aws_sqs_queue.delivery_dead_letter.arn
      && jsondecode(aws_sqs_queue.delivery.redrive_policy).maxReceiveCount == 5
    )
    error_message = "The source queue must retry five receives before using its exact DLQ."
  }

  assert {
    condition = (
      jsondecode(aws_sqs_queue_redrive_allow_policy.delivery.redrive_allow_policy).redrivePermission
      == "byQueue"
      && jsondecode(aws_sqs_queue_redrive_allow_policy.delivery.redrive_allow_policy).sourceQueueArns
      == [aws_sqs_queue.delivery.arn]
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
      !aws_lb.async.internal
      && aws_lb.async.load_balancer_type == "application"
      && !aws_lb.async.enable_deletion_protection
      && aws_lb.async.drop_invalid_header_fields
      && length(aws_lb.async.subnets) == 2
    )
    error_message = "The disposable public ALB must span both asynchronous subnets."
  }

  assert {
    condition = (
      one(aws_security_group.async_load_balancer.ingress).from_port == 80
      && one(aws_security_group.async_load_balancer.ingress).cidr_blocks == tolist(["203.0.113.10/32"])
    )
    error_message = "Only HTTP from the approved benchmark address may reach the ALB."
  }

  assert {
    condition = (
      one(aws_security_group.async_load_balancer.egress).from_port == 8000
      && one(aws_security_group.async_load_balancer.egress).to_port == 8000
      && one(aws_security_group.async_load_balancer.egress).cidr_blocks == tolist([aws_vpc.rehost.cidr_block])
    )
    error_message = "The ALB may send only API-port traffic inside the experiment VPC."
  }

  assert {
    condition = (
      one(aws_security_group.async_api.ingress).from_port == 8000
      && one(aws_security_group.async_api.ingress).cidr_blocks == null
      && one(aws_security_group.async_api.ingress).security_groups == toset(["sg-mocked-async-alb"])
    )
    error_message = "Only the ALB security group may reach API tasks."
  }

  assert {
    condition = (
      length(aws_security_group.async_worker.ingress) == 0
      && one(aws_security_group.async_simulator.ingress).from_port == 8001
      && one(aws_security_group.async_simulator.ingress).security_groups == toset(["sg-mocked-async-worker"])
    )
    error_message = "Workers need no ingress and are the simulator's only caller."
  }

  assert {
    condition = (
      aws_lb_listener.async_http.port == 80
      && aws_lb_listener.async_http.protocol == "HTTP"
      && aws_lb_target_group.async_api.target_type == "ip"
      && one(aws_lb_target_group.async_api.health_check).path == "/health/ready"
    )
    error_message = "The ALB must route HTTP to ready Fargate task IPs."
  }
}

run "async_platform_has_bounded_logs_and_least_privilege_roles" {
  command = plan

  assert {
    condition = (
      one(aws_ecs_cluster.async.setting).name == "containerInsights"
      && one(aws_ecs_cluster.async.setting).value == "enabled"
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
      jsondecode(local.ecs_task_assume_role_policy).Statement[0].Principal.Service
      == "ecs-tasks.amazonaws.com"
      && aws_iam_role_policy_attachment.ecs_execution.policy_arn
      == "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
    )
    error_message = "Fargate roles must trust only ECS tasks and use the standard execution policy."
  }

  assert {
    condition = (
      jsondecode(aws_iam_role_policy.ecs_execution_database_secret.policy).Statement[0].Action
      == ["secretsmanager:GetSecretValue"]
      && jsondecode(aws_iam_role_policy.ecs_execution_database_secret.policy).Statement[0].Resource
      == aws_db_instance.postgres.master_user_secret[0].secret_arn
    )
    error_message = "The execution role may read only the RDS-managed database secret."
  }

  assert {
    condition = (
      jsondecode(aws_iam_role_policy.api_queue.policy).Statement[0].Action
      == ["sqs:SendMessage"]
      && toset(jsondecode(aws_iam_role_policy.worker_queue.policy).Statement[0].Action)
      == toset(["sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ReceiveMessage", "sqs:SendMessage"])
    )
    error_message = "API and worker queue permissions must stay role-specific."
  }
}
