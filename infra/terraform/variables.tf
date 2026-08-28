variable "aws_profile" {
  description = "Name of the private AWS CLI profile used by Terraform."
  type        = string

  validation {
    condition     = length(trimspace(var.aws_profile)) > 0
    error_message = "aws_profile must not be empty."
  }
}

variable "aws_region" {
  description = "AWS region that contains every TrackRelay resource."
  type        = string

  validation {
    condition     = length(trimspace(var.aws_region)) > 0
    error_message = "aws_region must not be empty."
  }
}

variable "session_id" {
  description = "Unique ID that ties every resource to one bounded cloud session."
  type        = string

  validation {
    condition = can(
      regex(
        "^cloud-session-[1234]-[0-9]{8}T[0-9]{6}Z$",
        var.session_id,
      )
    )
    error_message = (
      "session_id must look like cloud-session-1-20260822T090000Z."
    )
  }
}

variable "api_ingress_cidr" {
  description = "Single trusted IPv4 CIDR allowed to reach the rehost API."
  type        = string

  validation {
    condition = (
      can(cidrnetmask(var.api_ingress_cidr))
      && endswith(var.api_ingress_cidr, "/32")
    )
    error_message = "api_ingress_cidr must be one explicit IPv4 /32 CIDR."
  }
}

variable "rehost_instance_type" {
  description = "Frozen EC2 tier for the synchronous infrastructure-scaling experiment."
  type        = string
  default     = "t4g.small"

  validation {
    condition = contains(
      ["t4g.small", "c8g.large", "c8g.4xlarge"],
      var.rehost_instance_type,
    )
    error_message = (
      "rehost_instance_type must be t4g.small, c8g.large, or c8g.4xlarge."
    )
  }
}

variable "rehost_root_volume_gib" {
  description = "Encrypted gp3 root-volume size for the ephemeral rehost."
  type        = number
  default     = 16

  validation {
    condition = (
      var.rehost_root_volume_gib >= 8
      && var.rehost_root_volume_gib <= 32
    )
    error_message = "rehost_root_volume_gib must be between 8 and 32 GiB."
  }
}

variable "rehost_ami_parameter" {
  description = "AWS public SSM parameter for the current ARM Amazon Linux 2023 AMI."
  type        = string
  default     = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

variable "rds_postgres_major_version" {
  description = "PostgreSQL major version resolved to a concrete minor in each saved plan."
  type        = string
  default     = "17"

  validation {
    condition     = var.rds_postgres_major_version == "17"
    error_message = "The migration target must remain PostgreSQL 17."
  }
}

variable "rds_instance_class" {
  description = "Small single-AZ RDS class for migration validation."
  type        = string
  default     = "db.t4g.micro"

  validation {
    condition     = var.rds_instance_class == "db.t4g.micro"
    error_message = "Cloud session 1 is cost-bounded to db.t4g.micro."
  }
}

variable "rds_allocated_storage_gib" {
  description = "Fixed encrypted gp3 storage for the disposable database."
  type        = number
  default     = 20

  validation {
    condition     = var.rds_allocated_storage_gib == 20
    error_message = "Cloud session 1 is fixed at 20 GiB of RDS storage."
  }
}
