output "rehost_ecr_repository_url" {
  description = "Repository that receives the synchronous API image."
  value       = aws_ecr_repository.api.repository_url
}

output "worker_ecr_repository_url" {
  description = "Repository that receives the asynchronous worker image."
  value       = aws_ecr_repository.worker.repository_url
}

output "simulator_ecr_repository_url" {
  description = "Repository that receives the controlled simulator image."
  value       = aws_ecr_repository.simulator.repository_url
}

output "delivery_queue_url" {
  description = "URL consumed by API publishers and worker receivers."
  value       = aws_sqs_queue.delivery.url
}

output "delivery_queue_arn" {
  description = "ARN used to scope API and worker queue permissions."
  value       = aws_sqs_queue.delivery.arn
}

output "delivery_dead_letter_queue_url" {
  description = "URL inspected by asynchronous processing guardrails."
  value       = aws_sqs_queue.delivery_dead_letter.url
}

output "delivery_dead_letter_queue_arn" {
  description = "ARN verified against the source queue redrive policy."
  value       = aws_sqs_queue.delivery_dead_letter.arn
}

output "async_ecs_cluster_name" {
  description = "ECS cluster used by the asynchronous services and migration task."
  value       = aws_ecs_cluster.async.name
}

output "async_api_url" {
  description = "Temporary HTTP endpoint restricted to the approved benchmark CIDR."
  value       = "http://${aws_lb.async.dns_name}"
}

output "rehost_instance_id" {
  description = "EC2 instance managed through AWS Systems Manager."
  value       = aws_instance.rehost.id
}

output "rehost_public_ip" {
  description = "Temporary public endpoint address for the bounded session."
  value       = aws_instance.rehost.public_ip
}

output "rds_endpoint" {
  description = "Private PostgreSQL endpoint consumed only by the rehost installer."
  sensitive   = true
  value       = aws_db_instance.postgres.address
}

output "rds_master_secret_arn" {
  description = "RDS-managed credential secret consumed only by the rehost instance role."
  sensitive   = true
  value       = aws_db_instance.postgres.master_user_secret[0].secret_arn
}

output "rds_resolved_engine_version" {
  description = "Concrete PostgreSQL minor selected by the saved Terraform plan."
  value       = data.aws_rds_engine_version.postgres.version_actual
}

output "rds_identifier" {
  description = "Non-secret RDS identifier used to select CloudWatch metrics."
  value       = aws_db_instance.postgres.identifier
}
