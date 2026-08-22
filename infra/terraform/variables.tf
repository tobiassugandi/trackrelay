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
