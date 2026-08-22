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
