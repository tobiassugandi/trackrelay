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
        "^cloud-session-[123]-[0-9]{8}T[0-9]{6}Z$",
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
  description = "Cost-bounded ARM instance type for synchronous rehost validation."
  type        = string
  default     = "t4g.small"
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
