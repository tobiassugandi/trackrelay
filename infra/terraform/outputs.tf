output "rehost_ecr_repository_url" {
  description = "Repository that receives the synchronous API image."
  value       = aws_ecr_repository.api.repository_url
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
