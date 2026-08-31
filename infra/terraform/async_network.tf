resource "aws_subnet" "async_public" {
  count = 2

  availability_zone       = data.aws_availability_zones.available.names[count.index]
  cidr_block              = cidrsubnet(aws_vpc.rehost.cidr_block, 8, count.index + 2)
  map_public_ip_on_launch = true
  vpc_id                  = aws_vpc.rehost.id

  tags = {
    Name = "${local.name_prefix}-async-public-${count.index + 1}"
  }
}

resource "aws_route_table_association" "async_public" {
  count = length(aws_subnet.async_public)

  route_table_id = aws_route_table.rehost_public.id
  subnet_id      = aws_subnet.async_public[count.index].id
}

resource "aws_security_group" "async_load_balancer" {
  name        = "${local.name_prefix}-async-alb"
  description = "Public HTTP only from the approved benchmark location"
  vpc_id      = aws_vpc.rehost.id

  ingress {
    description = "TrackRelay API through the public load balancer"
    cidr_blocks = [var.api_ingress_cidr]
    from_port   = 80
    protocol    = "tcp"
    to_port     = 80
  }

  egress {
    cidr_blocks = [aws_vpc.rehost.cidr_block]
    from_port   = 8000
    protocol    = "tcp"
    to_port     = 8000
  }

  tags = {
    Name = "${local.name_prefix}-async-alb"
  }
}

resource "aws_security_group" "async_api" {
  name        = "${local.name_prefix}-async-api"
  description = "API tasks reachable only through the TrackRelay load balancer"
  vpc_id      = aws_vpc.rehost.id

  ingress {
    description     = "API traffic from the load balancer"
    from_port       = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.async_load_balancer.id]
    to_port         = 8000
  }

  egress {
    cidr_blocks = ["0.0.0.0/0"]
    from_port   = 0
    protocol    = "-1"
    to_port     = 0
  }

  tags = {
    Name = "${local.name_prefix}-async-api"
  }
}

resource "aws_security_group" "async_worker" {
  name        = "${local.name_prefix}-async-worker"
  description = "Worker tasks with no inbound network path"
  vpc_id      = aws_vpc.rehost.id

  egress {
    cidr_blocks = ["0.0.0.0/0"]
    from_port   = 0
    protocol    = "-1"
    to_port     = 0
  }

  tags = {
    Name = "${local.name_prefix}-async-worker"
  }
}

resource "aws_security_group" "async_migration" {
  name        = "${local.name_prefix}-async-migration"
  description = "One-off migration tasks with no inbound network path"
  vpc_id      = aws_vpc.rehost.id

  egress {
    cidr_blocks = ["0.0.0.0/0"]
    from_port   = 0
    protocol    = "-1"
    to_port     = 0
  }

  tags = {
    Name = "${local.name_prefix}-async-migration"
  }
}

resource "aws_security_group" "async_simulator" {
  name        = "${local.name_prefix}-async-simulator"
  description = "Controlled downstream reachable only by worker tasks"
  vpc_id      = aws_vpc.rehost.id

  ingress {
    description     = "Downstream delivery from worker tasks"
    from_port       = 8001
    protocol        = "tcp"
    security_groups = [aws_security_group.async_worker.id]
    to_port         = 8001
  }

  egress {
    cidr_blocks = ["0.0.0.0/0"]
    from_port   = 0
    protocol    = "-1"
    to_port     = 0
  }

  tags = {
    Name = "${local.name_prefix}-async-simulator"
  }
}

resource "aws_lb" "async" {
  name                       = "${local.name_prefix}-async"
  drop_invalid_header_fields = true
  enable_deletion_protection = false
  internal                   = false
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.async_load_balancer.id]
  subnets                    = aws_subnet.async_public[*].id

  tags = {
    Name = "${local.name_prefix}-async"
  }
}

resource "aws_lb_target_group" "async_api" {
  deregistration_delay = 30
  name                 = "${local.name_prefix}-api"
  port                 = 8000
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = aws_vpc.rehost.id

  health_check {
    enabled             = true
    healthy_threshold   = 2
    interval            = 15
    matcher             = "200"
    path                = "/health/ready"
    port                = "traffic-port"
    protocol            = "HTTP"
    timeout             = 5
    unhealthy_threshold = 2
  }

  tags = {
    Name = "${local.name_prefix}-api"
  }
}

resource "aws_lb_listener" "async_http" {
  load_balancer_arn = aws_lb.async.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    target_group_arn = aws_lb_target_group.async_api.arn
    type             = "forward"
  }
}
