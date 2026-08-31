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
  target          = aws_security_group.rehost
  override_during = plan
  values = {
    id = "sg-mocked-rehost"
  }
}

override_resource {
  target          = aws_security_group.async_api
  override_during = plan
  values = {
    id = "sg-mocked-api"
  }
}

override_resource {
  target          = aws_security_group.async_worker
  override_during = plan
  values = {
    id = "sg-mocked-worker"
  }
}

override_resource {
  target          = aws_security_group.async_migration
  override_during = plan
  values = {
    id = "sg-mocked-migration"
  }
}

run "rds_is_private_small_and_disposable" {
  command = plan

  assert {
    condition     = length(aws_subnet.database) == 2
    error_message = "The RDS subnet group must span two private subnets."
  }

  assert {
    condition     = aws_subnet.database[0].availability_zone != aws_subnet.database[1].availability_zone
    error_message = "The RDS subnets must occupy different Availability Zones."
  }

  assert {
    condition     = alltrue([for subnet in aws_subnet.database : !subnet.map_public_ip_on_launch])
    error_message = "Database subnets must not assign public addresses."
  }

  assert {
    condition     = alltrue([for rule in aws_security_group.database.ingress : rule.from_port == 5432 && rule.to_port == 5432 && rule.protocol == "tcp"])
    error_message = "Every database ingress rule must expose only PostgreSQL."
  }

  assert {
    condition     = alltrue([for rule in aws_security_group.database.ingress : rule.cidr_blocks == null])
    error_message = "The database must not accept PostgreSQL from an IP CIDR."
  }

  assert {
    condition     = length(aws_security_group.database.ingress) == 4
    error_message = "Only the rehost, asynchronous API, migration, and worker may reach PostgreSQL."
  }

  assert {
    condition = toset(flatten([
      for rule in aws_security_group.database.ingress : tolist(rule.security_groups)
    ])) == toset(["sg-mocked-rehost", "sg-mocked-api", "sg-mocked-migration", "sg-mocked-worker"])
    error_message = "Database ingress must name only the four authorized compute security groups."
  }

  assert {
    condition     = aws_db_instance.postgres.instance_class == "db.t4g.micro"
    error_message = "Cloud session 1 must use the cost-bounded RDS class."
  }

  assert {
    condition     = aws_db_instance.postgres.allocated_storage == 20
    error_message = "Cloud session 1 must use exactly 20 GiB of RDS storage."
  }

  assert {
    condition     = aws_db_instance.postgres.storage_type == "gp3" && aws_db_instance.postgres.storage_encrypted
    error_message = "RDS storage must use encrypted gp3."
  }

  assert {
    condition     = !aws_db_instance.postgres.publicly_accessible && !aws_db_instance.postgres.multi_az
    error_message = "The migration database must be private and single-AZ."
  }

  assert {
    condition     = aws_db_instance.postgres.manage_master_user_password
    error_message = "RDS must generate and manage the master password in Secrets Manager."
  }

  assert {
    condition     = aws_db_instance.postgres.backup_retention_period == 0 && aws_db_instance.postgres.delete_automated_backups && aws_db_instance.postgres.skip_final_snapshot
    error_message = "The synthetic database must retain no backup or final snapshot."
  }

  assert {
    condition     = !aws_db_instance.postgres.deletion_protection
    error_message = "The bounded experiment database must remain removable."
  }

  assert {
    condition     = !aws_db_instance.postgres.performance_insights_enabled && aws_db_instance.postgres.monitoring_interval == 0
    error_message = "Optional RDS monitoring charges must remain disabled."
  }

  assert {
    condition     = local.rehost_database_secret_actions == ["secretsmanager:GetSecretValue"]
    error_message = "The rehost role needs narrowly scoped access to the managed database secret."
  }

  assert {
    condition     = local.rehost_database_discovery_actions == ["rds:DescribeDBInstances"]
    error_message = "The rehost role needs read-only RDS metadata discovery."
  }
}

variables {
  api_ingress_cidr = "203.0.113.10/32"
  aws_profile      = "trackrelay-admin"
  aws_region       = "ap-southeast-3"
  session_id       = "cloud-session-4-20260824T090000Z"
}

run "economical_baseline_is_burstable_and_disposable" {
  command = plan

  assert {
    condition     = aws_instance.rehost.instance_type == "t3.small"
    error_message = "The economical baseline must use t3.small."
  }

  assert {
    condition     = aws_instance.rehost.credit_specification[0].cpu_credits == "standard"
    error_message = "The burstable baseline must not incur unlimited-credit charges."
  }

  assert {
    condition     = aws_instance.rehost.monitoring
    error_message = "Stage 9.3 requires one-minute EC2 resource evidence."
  }

  assert {
    condition     = aws_instance.rehost.root_block_device[0].volume_size == 16
    error_message = "The ephemeral root volume must stay at 16 GiB."
  }

  assert {
    condition     = aws_instance.rehost.root_block_device[0].delete_on_termination
    error_message = "The root volume must be deleted with the instance."
  }

  assert {
    condition     = aws_instance.rehost.metadata_options[0].http_tokens == "required"
    error_message = "The instance must require IMDSv2 tokens."
  }

  assert {
    condition     = one(aws_security_group.rehost.ingress).from_port == 8000
    error_message = "Only the TrackRelay API port should be exposed."
  }

  assert {
    condition     = one(aws_security_group.rehost.ingress).cidr_blocks == tolist(["203.0.113.10/32"])
    error_message = "API ingress must be restricted to the approved CIDR."
  }

  assert {
    condition     = aws_ecr_repository.api.force_delete
    error_message = "Teardown must remove the ECR repository even with images."
  }

  assert {
    condition     = strcontains(aws_instance.rehost.user_data, "docker-compose-linux-x86_64")
    error_message = "The x86_64 rehost must install the Docker Compose plugin."
  }

  assert {
    condition     = strcontains(aws_instance.rehost.user_data, "sha256sum --check")
    error_message = "The downloaded Compose binary must be checksum verified."
  }
}

run "compute_optimized_tier_uses_c7i_flex_without_cpu_credits" {
  command = plan

  variables {
    rehost_instance_type = "c7i-flex.large"
  }

  assert {
    condition     = aws_instance.rehost.instance_type == "c7i-flex.large"
    error_message = "The compute-optimized tier must use c7i-flex.large."
  }

  assert {
    condition     = length(aws_instance.rehost.credit_specification) == 0
    error_message = "The non-burstable compute tier must have no credit configuration."
  }
}

run "memory_optimized_tier_uses_m7i_flex_without_cpu_credits" {
  command = plan

  variables {
    rehost_instance_type = "m7i-flex.large"
  }

  assert {
    condition     = aws_instance.rehost.instance_type == "m7i-flex.large"
    error_message = "The memory-optimized tier must use m7i-flex.large."
  }

  assert {
    condition     = length(aws_instance.rehost.credit_specification) == 0
    error_message = "The non-burstable memory tier must have no credit configuration."
  }
}

run "unapproved_instance_type_is_rejected" {
  command = plan

  variables {
    rehost_instance_type = "c7g.4xlarge"
  }

  expect_failures = [var.rehost_instance_type]
}
