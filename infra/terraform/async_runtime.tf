locals {
  async_task_capacity = {
    api = {
      cpu_units  = 1024
      memory_mib = 2048
    }
    migration = {
      cpu_units  = 256
      memory_mib = 512
    }
    simulator = {
      cpu_units  = 256
      memory_mib = 512
    }
    worker = {
      cpu_units  = 256
      memory_mib = 512
    }
  }

  async_fixed_service_counts = {
    api       = 2
    simulator = 1
    worker    = 1
  }

  async_database_environment = [
    {
      name  = "TRACKRELAY_DATABASE_HOST"
      value = aws_db_instance.postgres.address
    },
    {
      name  = "TRACKRELAY_DATABASE_PORT"
      value = tostring(aws_db_instance.postgres.port)
    },
    {
      name  = "TRACKRELAY_DATABASE_NAME"
      value = aws_db_instance.postgres.db_name
    },
    {
      name  = "TRACKRELAY_DATABASE_USER"
      value = aws_db_instance.postgres.username
    },
    {
      name  = "TRACKRELAY_DATABASE_SSLMODE"
      value = "require"
    },
    {
      name  = "TRACKRELAY_DATABASE_POOL_SIZE"
      value = "5"
    },
    {
      name  = "TRACKRELAY_DATABASE_MAX_OVERFLOW"
      value = "10"
    },
  ]

  async_database_secrets = [
    {
      name      = "TRACKRELAY_DATABASE_PASSWORD"
      valueFrom = "${aws_db_instance.postgres.master_user_secret[0].secret_arn}:password::"
    },
  ]

  async_queue_environment = local.async_enabled ? [
    {
      name  = "TRACKRELAY_DELIVERY_QUEUE_BACKEND"
      value = "sqs"
    },
    {
      name  = "TRACKRELAY_SQS_QUEUE_URL"
      value = aws_sqs_queue.delivery[0].url
    },
    {
      name  = "TRACKRELAY_SQS_DEAD_LETTER_QUEUE_ARN"
      value = aws_sqs_queue.delivery_dead_letter[0].arn
    },
    {
      name  = "TRACKRELAY_AWS_REGION"
      value = var.aws_region
    },
  ] : []

  async_downstream_environment = local.async_runtime_enabled ? [
    {
      name  = "TRACKRELAY_DOWNSTREAM_URL"
      value = "http://simulator.${aws_service_discovery_private_dns_namespace.async[0].name}:8001"
    },
    {
      name  = "TRACKRELAY_DOWNSTREAM_TIMEOUT_SECONDS"
      value = "5.0"
    },
  ] : []

  async_container_base = {
    essential              = true
    linuxParameters        = { initProcessEnabled = true }
    mountPoints            = [{ sourceVolume = "tmp", containerPath = "/tmp", readOnly = false }]
    readonlyRootFilesystem = true
    stopTimeout            = 30
    user                   = "10001:10001"
  }

  async_log_configurations = {
    for role in local.async_log_groups : role => {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.async[role].name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = role
      }
    }
  }
}

resource "aws_service_discovery_private_dns_namespace" "async" {
  count = local.async_runtime_enabled ? 1 : 0

  description = "Private service discovery for one TrackRelay cloud session"
  name        = "trackrelay-${local.resource_suffix}.internal"
  vpc         = aws_vpc.rehost.id

  tags = {
    Name = "${local.name_prefix}-async"
  }
}

resource "aws_service_discovery_service" "simulator" {
  count = local.async_runtime_enabled ? 1 : 0

  name = "simulator"

  dns_config {
    namespace_id   = aws_service_discovery_private_dns_namespace.async[0].id
    routing_policy = "MULTIVALUE"

    dns_records {
      ttl  = 10
      type = "A"
    }
  }

  health_check_custom_config {}

  tags = {
    Name = "${local.name_prefix}-simulator"
  }
}

resource "aws_ecs_task_definition" "async_api" {
  count = local.async_runtime_enabled ? 1 : 0

  container_definitions = jsonencode([
    merge(local.async_container_base, {
      name  = "api"
      image = "${aws_ecr_repository.api.repository_url}@${var.api_image_digest}"
      environment = concat(
        local.async_database_environment,
        local.async_queue_environment,
        local.async_downstream_environment,
      )
      secrets = local.async_database_secrets
      portMappings = [{
        name          = "http"
        appProtocol   = "http"
        containerPort = 8000
        hostPort      = 8000
        protocol      = "tcp"
      }]
      healthCheck = {
        command = [
          "CMD",
          "python",
          "-c",
          "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/health/live', timeout=1).read()",
        ]
        interval    = 10
        retries     = 3
        startPeriod = 5
        timeout     = 2
      }
      logConfiguration = local.async_log_configurations["api"]
    }),
  ])
  cpu                      = tostring(local.async_task_capacity.api.cpu_units)
  execution_role_arn       = aws_iam_role.ecs_execution[0].arn
  family                   = "${local.name_prefix}-api"
  memory                   = tostring(local.async_task_capacity.api.memory_mib)
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  skip_destroy             = false
  task_role_arn            = aws_iam_role.api_task[0].arn
  track_latest             = false

  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }

  volume {
    name = "tmp"
  }
}

resource "aws_ecs_task_definition" "async_worker" {
  count = local.async_runtime_enabled ? 1 : 0

  container_definitions = jsonencode([
    merge(local.async_container_base, {
      name  = "worker"
      image = "${aws_ecr_repository.worker[0].repository_url}@${var.worker_image_digest}"
      environment = concat(
        local.async_database_environment,
        local.async_queue_environment,
        local.async_downstream_environment,
      )
      secrets          = local.async_database_secrets
      logConfiguration = local.async_log_configurations["worker"]
    }),
  ])
  cpu                      = tostring(local.async_task_capacity.worker.cpu_units)
  execution_role_arn       = aws_iam_role.ecs_execution[0].arn
  family                   = "${local.name_prefix}-worker"
  memory                   = tostring(local.async_task_capacity.worker.memory_mib)
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  skip_destroy             = false
  task_role_arn            = aws_iam_role.worker_task[0].arn
  track_latest             = false

  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }

  volume {
    name = "tmp"
  }
}

resource "aws_ecs_task_definition" "async_simulator" {
  count = local.async_runtime_enabled ? 1 : 0

  container_definitions = jsonencode([
    merge(local.async_container_base, {
      name  = "simulator"
      image = "${aws_ecr_repository.simulator[0].repository_url}@${var.simulator_image_digest}"
      portMappings = [{
        name          = "http"
        appProtocol   = "http"
        containerPort = 8001
        hostPort      = 8001
        protocol      = "tcp"
      }]
      healthCheck = {
        command = [
          "CMD",
          "python",
          "-c",
          "from urllib.request import urlopen; urlopen('http://127.0.0.1:8001/health/live', timeout=1).read()",
        ]
        interval    = 10
        retries     = 3
        startPeriod = 5
        timeout     = 2
      }
      logConfiguration = local.async_log_configurations["simulator"]
    }),
  ])
  cpu                      = tostring(local.async_task_capacity.simulator.cpu_units)
  execution_role_arn       = aws_iam_role.ecs_execution[0].arn
  family                   = "${local.name_prefix}-simulator"
  memory                   = tostring(local.async_task_capacity.simulator.memory_mib)
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  skip_destroy             = false
  task_role_arn            = aws_iam_role.simulator_task[0].arn
  track_latest             = false

  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }

  volume {
    name = "tmp"
  }
}

resource "aws_ecs_task_definition" "async_migration" {
  count = local.async_runtime_enabled ? 1 : 0

  container_definitions = jsonencode([
    merge(local.async_container_base, {
      name             = "migration"
      image            = "${aws_ecr_repository.api.repository_url}@${var.api_image_digest}"
      command          = ["alembic", "upgrade", "head"]
      environment      = local.async_database_environment
      secrets          = local.async_database_secrets
      logConfiguration = local.async_log_configurations["migration"]
    }),
  ])
  cpu                      = tostring(local.async_task_capacity.migration.cpu_units)
  execution_role_arn       = aws_iam_role.ecs_execution[0].arn
  family                   = "${local.name_prefix}-migration"
  memory                   = tostring(local.async_task_capacity.migration.memory_mib)
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  skip_destroy             = false
  task_role_arn            = aws_iam_role.migration_task[0].arn
  track_latest             = false

  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }

  volume {
    name = "tmp"
  }
}

resource "aws_ecs_service" "async_simulator" {
  count = var.async_services_enabled && local.async_runtime_enabled ? 1 : 0

  cluster                            = aws_ecs_cluster.async[0].id
  deployment_maximum_percent         = 100
  deployment_minimum_healthy_percent = 0
  desired_count                      = local.async_fixed_service_counts.simulator
  enable_ecs_managed_tags            = true
  enable_execute_command             = false
  force_delete                       = true
  launch_type                        = "FARGATE"
  name                               = "${local.name_prefix}-simulator"
  platform_version                   = "1.4.0"
  propagate_tags                     = "SERVICE"
  scheduling_strategy                = "REPLICA"
  task_definition                    = aws_ecs_task_definition.async_simulator[0].arn
  wait_for_steady_state              = false

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    assign_public_ip = true
    security_groups  = [aws_security_group.async_simulator[0].id]
    subnets          = aws_subnet.async_public[*].id
  }

  service_registries {
    registry_arn = aws_service_discovery_service.simulator[0].arn
  }

  tags = {
    Name = "${local.name_prefix}-simulator"
  }
}

resource "aws_ecs_service" "async_worker" {
  count = var.async_services_enabled && local.async_runtime_enabled ? 1 : 0

  cluster                            = aws_ecs_cluster.async[0].id
  deployment_maximum_percent         = 100
  deployment_minimum_healthy_percent = 0
  desired_count                      = local.async_fixed_service_counts.worker
  enable_ecs_managed_tags            = true
  enable_execute_command             = false
  force_delete                       = true
  launch_type                        = "FARGATE"
  name                               = "${local.name_prefix}-worker"
  platform_version                   = "1.4.0"
  propagate_tags                     = "SERVICE"
  scheduling_strategy                = "REPLICA"
  task_definition                    = aws_ecs_task_definition.async_worker[0].arn
  wait_for_steady_state              = false

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    assign_public_ip = true
    security_groups  = [aws_security_group.async_worker[0].id]
    subnets          = aws_subnet.async_public[*].id
  }

  tags = {
    Name = "${local.name_prefix}-worker"
  }

  depends_on = [aws_ecs_service.async_simulator]
}

resource "aws_ecs_service" "async_api" {
  count = var.async_services_enabled && local.async_runtime_enabled ? 1 : 0

  cluster                            = aws_ecs_cluster.async[0].id
  deployment_maximum_percent         = 100
  deployment_minimum_healthy_percent = 0
  desired_count                      = local.async_fixed_service_counts.api
  enable_ecs_managed_tags            = true
  enable_execute_command             = false
  force_delete                       = true
  health_check_grace_period_seconds  = 60
  launch_type                        = "FARGATE"
  name                               = "${local.name_prefix}-api"
  platform_version                   = "1.4.0"
  propagate_tags                     = "SERVICE"
  scheduling_strategy                = "REPLICA"
  task_definition                    = aws_ecs_task_definition.async_api[0].arn
  wait_for_steady_state              = false

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  load_balancer {
    container_name   = "api"
    container_port   = 8000
    target_group_arn = aws_lb_target_group.async_api[0].arn
  }

  network_configuration {
    assign_public_ip = true
    security_groups  = [aws_security_group.async_api[0].id]
    subnets          = aws_subnet.async_public[*].id
  }

  tags = {
    Name = "${local.name_prefix}-api"
  }

  depends_on = [aws_lb_listener.async_http[0]]
}
