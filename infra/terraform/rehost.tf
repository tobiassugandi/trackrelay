data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_ssm_parameter" "rehost_ami" {
  name = var.rehost_ami_parameter
}

resource "aws_vpc" "rehost" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name = "${local.name_prefix}-vpc"
  }
}

resource "aws_internet_gateway" "rehost" {
  vpc_id = aws_vpc.rehost.id

  tags = {
    Name = "${local.name_prefix}-igw"
  }
}

resource "aws_subnet" "rehost_public" {
  availability_zone       = data.aws_availability_zones.available.names[0]
  cidr_block              = "10.42.1.0/24"
  map_public_ip_on_launch = true
  vpc_id                  = aws_vpc.rehost.id

  tags = {
    Name = "${local.name_prefix}-public"
  }
}

resource "aws_route_table" "rehost_public" {
  vpc_id = aws_vpc.rehost.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.rehost.id
  }

  tags = {
    Name = "${local.name_prefix}-public"
  }
}

resource "aws_route_table_association" "rehost_public" {
  route_table_id = aws_route_table.rehost_public.id
  subnet_id      = aws_subnet.rehost_public.id
}

resource "aws_security_group" "rehost" {
  name        = "${local.name_prefix}-host"
  description = "TrackRelay synchronous rehost; API only, no SSH"
  vpc_id      = aws_vpc.rehost.id

  ingress {
    description = "TrackRelay API from the approved benchmark location"
    cidr_blocks = [var.api_ingress_cidr]
    from_port   = 8000
    protocol    = "tcp"
    to_port     = 8000
  }

  egress {
    cidr_blocks = ["0.0.0.0/0"]
    from_port   = 0
    protocol    = "-1"
    to_port     = 0
  }

  tags = {
    Name = "${local.name_prefix}-host"
  }
}

resource "aws_ecr_repository" "api" {
  force_delete         = true
  image_tag_mutability = "MUTABLE"
  name                 = "${local.name_prefix}-api"

  encryption_configuration {
    encryption_type = "AES256"
  }

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_iam_role" "rehost" {
  name = "${local.name_prefix}-instance"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "rehost" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
    "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
  ])

  policy_arn = each.value
  role       = aws_iam_role.rehost.name
}

resource "aws_iam_instance_profile" "rehost" {
  name = "${local.name_prefix}-instance"
  role = aws_iam_role.rehost.name
}

resource "aws_instance" "rehost" {
  ami                                  = data.aws_ssm_parameter.rehost_ami.value
  associate_public_ip_address          = true
  availability_zone                    = data.aws_availability_zones.available.names[0]
  disable_api_stop                     = false
  disable_api_termination              = false
  iam_instance_profile                 = aws_iam_instance_profile.rehost.name
  instance_initiated_shutdown_behavior = "terminate"
  instance_type                        = var.rehost_instance_type
  monitoring                           = true
  subnet_id                            = aws_subnet.rehost_public.id
  user_data                            = file("${path.module}/bootstrap/rehost.sh")
  user_data_replace_on_change          = true
  vpc_security_group_ids               = [aws_security_group.rehost.id]

  dynamic "credit_specification" {
    for_each = var.rehost_instance_type == "t3.small" ? [true] : []

    content {
      cpu_credits = "standard"
    }
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_protocol_ipv6          = "disabled"
    http_put_response_hop_limit = 1
    http_tokens                 = "required"
    instance_metadata_tags      = "disabled"
  }

  root_block_device {
    delete_on_termination = true
    encrypted             = true
    volume_size           = var.rehost_root_volume_gib
    volume_type           = "gp3"

    tags = merge(local.common_tags, {
      Name = "${local.name_prefix}-root"
    })
  }

  tags = {
    Name = "${local.name_prefix}-host"
  }

  depends_on = [
    aws_iam_role_policy_attachment.rehost,
    aws_route_table_association.rehost_public,
  ]
}
