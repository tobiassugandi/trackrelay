mock_provider "aws" {
  override_during = plan

  mock_data "aws_availability_zones" {
    defaults = {
      names = ["ap-southeast-3a"]
    }
  }

  mock_data "aws_ssm_parameter" {
    defaults = {
      value = "ami-mocked-arm64"
    }
  }
}

variables {
  api_ingress_cidr = "203.0.113.10/32"
  aws_profile      = "trackrelay-admin"
  aws_region       = "ap-southeast-3"
  session_id       = "cloud-session-1-20260824T090000Z"
}

run "rehost_is_small_and_disposable" {
  command = plan

  assert {
    condition     = aws_instance.rehost.instance_type == "t4g.small"
    error_message = "The rehost must use the cost-bounded ARM instance type."
  }

  assert {
    condition     = aws_instance.rehost.credit_specification[0].cpu_credits == "standard"
    error_message = "Burst-credit charges must be disabled."
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
}
