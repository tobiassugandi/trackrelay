output "rehost_ecr_repository_url" {
  description = "Repository that receives the synchronous API image."
  value       = aws_ecr_repository.api.repository_url
}

output "worker_ecr_repository_url" {
  description = "Repository that receives the asynchronous worker image."
  value       = try(aws_ecr_repository.worker[0].repository_url, null)
}

output "simulator_ecr_repository_url" {
  description = "Repository that receives the controlled simulator image."
  value       = try(aws_ecr_repository.simulator[0].repository_url, null)
}

output "delivery_queue_url" {
  description = "URL consumed by API publishers and worker receivers."
  value       = try(aws_sqs_queue.delivery[0].url, null)
}

output "delivery_queue_arn" {
  description = "ARN used to scope API and worker queue permissions."
  value       = try(aws_sqs_queue.delivery[0].arn, null)
}

output "delivery_dead_letter_queue_url" {
  description = "URL inspected by asynchronous processing guardrails."
  value       = try(aws_sqs_queue.delivery_dead_letter[0].url, null)
}

output "delivery_dead_letter_queue_arn" {
  description = "ARN verified against the source queue redrive policy."
  value       = try(aws_sqs_queue.delivery_dead_letter[0].arn, null)
}

output "async_ecs_cluster_name" {
  description = "ECS cluster used by the asynchronous services and migration task."
  value       = try(aws_ecs_cluster.async[0].name, null)
}

output "async_service_names" {
  description = "Fixed service names checked for convergence after migration."
  value = var.async_services_enabled && local.async_runtime_enabled ? [
    aws_ecs_service.async_api[0].name,
    aws_ecs_service.async_simulator[0].name,
    aws_ecs_service.async_worker[0].name,
  ] : []
}

output "async_service_capacity" {
  description = "Fixed non-secret service capacity recorded after convergence."
  value = var.async_services_enabled && local.async_runtime_enabled ? {
    api = {
      cpu_units     = local.async_task_capacity.api.cpu_units
      desired_count = aws_ecs_service.async_api[0].desired_count
      memory_mib    = local.async_task_capacity.api.memory_mib
      service_name  = aws_ecs_service.async_api[0].name
    }
    simulator = {
      cpu_units     = local.async_task_capacity.simulator.cpu_units
      desired_count = aws_ecs_service.async_simulator[0].desired_count
      memory_mib    = local.async_task_capacity.simulator.memory_mib
      service_name  = aws_ecs_service.async_simulator[0].name
    }
    worker = {
      cpu_units     = local.async_task_capacity.worker.cpu_units
      desired_count = aws_ecs_service.async_worker[0].desired_count
      memory_mib    = local.async_task_capacity.worker.memory_mib
      service_name  = aws_ecs_service.async_worker[0].name
    }
  } : {}
}

output "async_api_url" {
  description = "Temporary HTTP endpoint restricted to the approved benchmark CIDR."
  value       = try("http://${aws_lb.async[0].dns_name}", null)
}

output "async_observability_dimensions" {
  description = "Non-secret dimensions used to collect asynchronous experiment evidence."
  value = var.async_services_enabled && local.async_runtime_enabled ? {
    api_service_name        = aws_ecs_service.async_api[0].name
    cluster_name            = aws_ecs_cluster.async[0].name
    dashboard_name          = aws_cloudwatch_dashboard.async[0].dashboard_name
    delivery_queue_name     = aws_sqs_queue.delivery[0].name
    load_balancer_dimension = aws_lb.async[0].arn_suffix
    rds_identifier          = aws_db_instance.postgres.identifier
    simulator_service_name  = aws_ecs_service.async_simulator[0].name
    worker_service_name     = aws_ecs_service.async_worker[0].name
  } : {}
}

output "async_migration_run_configuration" {
  description = "Non-secret inputs for running the one-off migration before services are enabled."
  value = local.async_runtime_enabled ? {
    cluster_name        = aws_ecs_cluster.async[0].name
    security_group_ids  = [aws_security_group.async_migration[0].id]
    subnet_ids          = aws_subnet.async_public[*].id
    task_definition_arn = aws_ecs_task_definition.async_migration[0].arn
  } : null
}

output "rehost_instance_id" {
  description = "EC2 instance managed through AWS Systems Manager."
  value       = try(aws_instance.rehost[0].id, null)
}

output "rehost_public_ip" {
  description = "Temporary public endpoint address for the bounded session."
  value       = try(aws_instance.rehost[0].public_ip, null)
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
