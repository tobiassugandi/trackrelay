locals {
  ecs_task_assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
      },
    ]
  })

  async_log_groups = toset(["api", "migration", "simulator", "worker"])

  async_image_digests = compact([
    var.api_image_digest,
    var.worker_image_digest,
    var.simulator_image_digest,
  ])
  async_runtime_enabled = length(local.async_image_digests) == 3
}

check "async_image_digests_are_all_set_or_all_empty" {
  assert {
    condition = contains(
      [0, 3],
      length(local.async_image_digests),
    )
    error_message = "API, worker, and simulator image digests must be supplied together."
  }
}

check "async_services_require_runtime" {
  assert {
    condition     = !var.async_services_enabled || local.async_runtime_enabled
    error_message = "async_services_enabled requires all three image digests."
  }
}

resource "aws_ecs_cluster" "async" {
  name = "${local.name_prefix}-async"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = {
    Name = "${local.name_prefix}-async"
  }
}

resource "aws_cloudwatch_log_group" "async" {
  for_each = local.async_log_groups

  name              = "/trackrelay/${local.resource_suffix}/${each.key}"
  retention_in_days = 1
  skip_destroy      = false
}

resource "aws_iam_role" "ecs_execution" {
  name               = "${local.name_prefix}-ecs-execution"
  assume_role_policy = local.ecs_task_assume_role_policy
}

resource "aws_iam_role_policy_attachment" "ecs_execution" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "ecs_execution_database_secret" {
  name = "${local.name_prefix}-database-secret"
  role = aws_iam_role.ecs_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action   = ["secretsmanager:GetSecretValue"]
        Effect   = "Allow"
        Resource = aws_db_instance.postgres.master_user_secret[0].secret_arn
      },
    ]
  })
}

resource "aws_iam_role" "api_task" {
  name               = "${local.name_prefix}-api-task"
  assume_role_policy = local.ecs_task_assume_role_policy
}

resource "aws_iam_role_policy" "api_queue" {
  name = "${local.name_prefix}-delivery-publish"
  role = aws_iam_role.api_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action   = ["sqs:SendMessage"]
        Effect   = "Allow"
        Resource = aws_sqs_queue.delivery.arn
      },
    ]
  })
}

resource "aws_iam_role" "worker_task" {
  name               = "${local.name_prefix}-worker-task"
  assume_role_policy = local.ecs_task_assume_role_policy
}

resource "aws_iam_role_policy" "worker_queue" {
  name = "${local.name_prefix}-delivery-process"
  role = aws_iam_role.worker_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = [
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:ReceiveMessage",
          "sqs:SendMessage",
        ]
        Effect   = "Allow"
        Resource = aws_sqs_queue.delivery.arn
      },
    ]
  })
}

resource "aws_iam_role" "simulator_task" {
  name               = "${local.name_prefix}-simulator-task"
  assume_role_policy = local.ecs_task_assume_role_policy
}

resource "aws_iam_role" "migration_task" {
  name               = "${local.name_prefix}-migration-task"
  assume_role_policy = local.ecs_task_assume_role_policy
}
