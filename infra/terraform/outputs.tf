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
