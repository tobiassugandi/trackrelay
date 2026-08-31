locals {
  delivery_max_receive_count = 5
}

resource "aws_ecr_repository" "worker" {
  count = local.async_enabled ? 1 : 0

  force_delete         = true
  image_tag_mutability = "MUTABLE"
  name                 = "${local.name_prefix}-worker"

  encryption_configuration {
    encryption_type = "AES256"
  }

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_repository" "simulator" {
  count = local.async_enabled ? 1 : 0

  force_delete         = true
  image_tag_mutability = "MUTABLE"
  name                 = "${local.name_prefix}-simulator"

  encryption_configuration {
    encryption_type = "AES256"
  }

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_sqs_queue" "delivery_dead_letter" {
  count = local.async_enabled ? 1 : 0

  delay_seconds             = 0
  message_retention_seconds = 345600
  name                      = "${local.name_prefix}-delivery-dlq"
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "delivery" {
  count = local.async_enabled ? 1 : 0

  delay_seconds              = 0
  message_retention_seconds  = 86400
  name                       = "${local.name_prefix}-delivery"
  receive_wait_time_seconds  = 20
  sqs_managed_sse_enabled    = true
  visibility_timeout_seconds = 120

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.delivery_dead_letter[0].arn
    maxReceiveCount     = local.delivery_max_receive_count
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "delivery" {
  count = local.async_enabled ? 1 : 0

  queue_url = aws_sqs_queue.delivery_dead_letter[0].id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.delivery[0].arn]
  })
}
