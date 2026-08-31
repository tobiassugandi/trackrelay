locals {
  rehost_database_discovery_actions = ["rds:DescribeDBInstances"]
  rehost_database_secret_actions    = ["secretsmanager:GetSecretValue"]
}

data "aws_rds_engine_version" "postgres" {
  engine  = "postgres"
  latest  = true
  version = var.rds_postgres_major_version
}

data "aws_rds_orderable_db_instance" "postgres" {
  engine         = "postgres"
  engine_version = data.aws_rds_engine_version.postgres.version_actual
  instance_class = var.rds_instance_class
  license_model  = "postgresql-license"
  storage_type   = "gp3"
}

resource "aws_subnet" "database" {
  count = 2

  availability_zone       = data.aws_availability_zones.available.names[count.index]
  cidr_block              = cidrsubnet(aws_vpc.rehost.cidr_block, 8, count.index + 10)
  map_public_ip_on_launch = false
  vpc_id                  = aws_vpc.rehost.id

  tags = {
    Name = "${local.name_prefix}-database-${count.index + 1}"
  }
}

resource "aws_route_table" "database" {
  vpc_id = aws_vpc.rehost.id

  tags = {
    Name = "${local.name_prefix}-database"
  }
}

resource "aws_route_table_association" "database" {
  count = length(aws_subnet.database)

  route_table_id = aws_route_table.database.id
  subnet_id      = aws_subnet.database[count.index].id
}

resource "aws_db_subnet_group" "postgres" {
  description = "Private subnets for the TrackRelay experiment database"
  name        = "${local.name_prefix}-postgres"
  subnet_ids  = aws_subnet.database[*].id

  tags = {
    Name = "${local.name_prefix}-postgres"
  }
}

resource "aws_security_group" "database" {
  name        = "${local.name_prefix}-database"
  description = "PostgreSQL from explicitly authorized TrackRelay compute"
  vpc_id      = aws_vpc.rehost.id

  dynamic "ingress" {
    for_each = local.rehost_enabled ? [aws_security_group.rehost[0].id] : []

    content {
      description     = "PostgreSQL from the synchronous rehost"
      from_port       = 5432
      protocol        = "tcp"
      security_groups = [ingress.value]
      to_port         = 5432
    }
  }

  dynamic "ingress" {
    for_each = local.async_enabled ? [{
      description       = "PostgreSQL from asynchronous API tasks"
      security_group_id = aws_security_group.async_api[0].id
      }, {
      description       = "PostgreSQL from one-off migration tasks"
      security_group_id = aws_security_group.async_migration[0].id
      }, {
      description       = "PostgreSQL from asynchronous worker tasks"
      security_group_id = aws_security_group.async_worker[0].id
    }] : []

    content {
      description     = ingress.value.description
      from_port       = 5432
      protocol        = "tcp"
      security_groups = [ingress.value.security_group_id]
      to_port         = 5432
    }
  }

  tags = {
    Name = "${local.name_prefix}-database"
  }
}

resource "aws_db_parameter_group" "postgres" {
  description = "TrackRelay PostgreSQL ${var.rds_postgres_major_version} experiment configuration"
  family      = "postgres${var.rds_postgres_major_version}"
  name        = "${local.name_prefix}-postgres"

  tags = {
    Name = "${local.name_prefix}-postgres"
  }
}

resource "aws_db_instance" "postgres" {
  allocated_storage                   = var.rds_allocated_storage_gib
  apply_immediately                   = true
  auto_minor_version_upgrade          = false
  availability_zone                   = data.aws_availability_zones.available.names[0]
  backup_retention_period             = 0
  copy_tags_to_snapshot               = false
  db_name                             = "trackrelay"
  db_subnet_group_name                = aws_db_subnet_group.postgres.name
  delete_automated_backups            = true
  deletion_protection                 = false
  enabled_cloudwatch_logs_exports     = []
  engine                              = "postgres"
  engine_version                      = data.aws_rds_engine_version.postgres.version_actual
  iam_database_authentication_enabled = false
  identifier                          = "${local.name_prefix}-postgres"
  instance_class                      = data.aws_rds_orderable_db_instance.postgres.instance_class
  manage_master_user_password         = true
  max_allocated_storage               = 0
  monitoring_interval                 = 0
  multi_az                            = false
  network_type                        = "IPV4"
  parameter_group_name                = aws_db_parameter_group.postgres.name
  performance_insights_enabled        = false
  port                                = 5432
  publicly_accessible                 = false
  skip_final_snapshot                 = true
  storage_encrypted                   = true
  storage_type                        = "gp3"
  username                            = "trackrelay_admin"
  vpc_security_group_ids              = [aws_security_group.database.id]

  tags = {
    Name = "${local.name_prefix}-postgres"
  }

  depends_on = [aws_route_table_association.database]
}

resource "aws_iam_role_policy" "rehost_database_secret" {
  count = local.rehost_enabled ? 1 : 0

  name = "${local.name_prefix}-database-secret"
  role = aws_iam_role.rehost[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action   = local.rehost_database_secret_actions
        Effect   = "Allow"
        Resource = aws_db_instance.postgres.master_user_secret[0].secret_arn
      },
      {
        Action   = local.rehost_database_discovery_actions
        Effect   = "Allow"
        Resource = "*"
      },
    ]
  })
}
