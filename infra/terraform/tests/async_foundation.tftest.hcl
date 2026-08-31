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
