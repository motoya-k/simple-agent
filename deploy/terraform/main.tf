# simple-agent on ECS Fargate: one task, state in Postgres, model on Bedrock
# through the task role, secrets from Secrets Manager. See the README "Deploy".

data "aws_caller_identity" "current" {}

locals {
  account = data.aws_caller_identity.current.account_id
  secrets = concat(["SIMPLE_AGENT_IMAP_PASSWORD", "SIMPLE_AGENT_DATABASE_URL"], var.extra_secret_names)
  db_name = "simple_agent"
  # What the image's defaults do not already say. Deployment-specific values
  # (IMAP host, allowlist, ...) come in through var.environment.
  base_env = {
    SIMPLE_AGENT_PROVIDER     = "bedrock"
    SIMPLE_AGENT_MODEL        = var.model
    SIMPLE_AGENT_REVIEW_MODEL = var.review_model
    # Transcripts, memory and skills all follow SIMPLE_AGENT_DATABASE_URL,
    # which is a secret below: nothing is left on the container's disk.
    AWS_REGION = var.region
  }
}

# -- network -----------------------------------------------------------------
resource "aws_security_group" "task" {
  name        = "${var.name}-task"
  description = "simple-agent task: outbound only"
  vpc_id      = var.vpc_id

  egress {
    description = "Bedrock, IMAP, ECR, logs, secrets, MCP server APIs"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "db" {
  count       = var.create_database ? 1 : 0
  name        = "${var.name}-db"
  description = "simple-agent database: Postgres from the task only"
  vpc_id      = var.vpc_id

  ingress {
    description     = "Postgres from the task"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.task.id]
  }
}

# -- image and logs ----------------------------------------------------------
resource "aws_ecr_repository" "agent" {
  name                 = var.name
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_cloudwatch_log_group" "agent" {
  name              = "/ecs/${var.name}"
  retention_in_days = var.log_retention_days
}

# -- secrets -----------------------------------------------------------------
# Created empty (except the database URL when Terraform makes the database);
# put values in with the console or `aws secretsmanager put-secret-value`, so
# they never pass through Terraform state.
resource "aws_secretsmanager_secret" "env" {
  for_each = toset(local.secrets)
  name     = "${var.name}/${each.key}"
}

# -- database ----------------------------------------------------------------
resource "random_password" "db" {
  count   = var.create_database ? 1 : 0
  length  = 32
  special = false
}

resource "aws_db_subnet_group" "db" {
  count      = var.create_database ? 1 : 0
  name       = var.name
  subnet_ids = var.private_subnet_ids
}

resource "aws_db_instance" "db" {
  count                        = var.create_database ? 1 : 0
  identifier                   = var.name
  engine                       = "postgres"
  engine_version               = "16"
  instance_class               = var.db_instance_class
  allocated_storage            = 20
  max_allocated_storage        = 100
  storage_encrypted            = true
  db_name                      = local.db_name
  username                     = "simple_agent"
  password                     = random_password.db[0].result
  db_subnet_group_name         = aws_db_subnet_group.db[0].name
  vpc_security_group_ids       = [aws_security_group.db[0].id]
  publicly_accessible          = false
  backup_retention_period      = 7
  deletion_protection          = true
  skip_final_snapshot          = false
  final_snapshot_identifier    = "${var.name}-final"
  performance_insights_enabled = true
}

resource "aws_secretsmanager_secret_version" "database_url" {
  count     = var.create_database ? 1 : 0
  secret_id = aws_secretsmanager_secret.env["SIMPLE_AGENT_DATABASE_URL"].id
  secret_string = format(
    "postgresql://%s:%s@%s/%s?sslmode=require",
    aws_db_instance.db[0].username,
    random_password.db[0].result,
    aws_db_instance.db[0].endpoint,
    local.db_name,
  )
}

# -- IAM ---------------------------------------------------------------------
data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# Used by ECS itself: pull the image, write logs, read the secrets.
resource "aws_iam_role" "execution" {
  name               = "${var.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "read_secrets" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [for s in aws_secretsmanager_secret.env : s.arn]
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "read-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.read_secrets.json
}

# Used by the agent: call the two models it is configured with, nothing else.
resource "aws_iam_role" "task" {
  name               = "${var.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

data "aws_iam_policy_document" "bedrock" {
  statement {
    sid     = "InvokeConfiguredProfiles"
    actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = [
      for profile in distinct([var.model, var.review_model]) :
      "arn:aws:bedrock:${var.region}:${local.account}:inference-profile/${profile}"
    ]
  }
  statement {
    # A cross-region profile forwards to the foundation model in each region
    # it routes to; the call must be allowed there too, but only through
    # one of the profiles above.
    sid     = "ThroughProfilesOnly"
    actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = [
      for region in var.bedrock_model_regions :
      "arn:aws:bedrock:${region}::foundation-model/anthropic.*"
    ]
    condition {
      test     = "StringLike"
      variable = "bedrock:InferenceProfileArn"
      values = [
        for profile in distinct([var.model, var.review_model]) :
        "arn:aws:bedrock:${var.region}:${local.account}:inference-profile/${profile}"
      ]
    }
  }
}

resource "aws_iam_role_policy" "task_bedrock" {
  name   = "bedrock"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.bedrock.json
}

# -- ECS ---------------------------------------------------------------------
resource "aws_ecs_cluster" "agent" {
  name = var.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_task_definition" "agent" {
  family                   = var.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([{
    name      = "agent"
    image     = var.image
    essential = true
    command   = ["simple-agent", "--email"]

    environment = [
      for k, v in merge(local.base_env, var.environment) : { name = k, value = v }
    ]
    secrets = [
      for name, secret in aws_secretsmanager_secret.env : { name = name, valueFrom = secret.arn }
    ]

    # ECS ignores the image's HEALTHCHECK; this is the one that counts.
    healthCheck = {
      command     = ["CMD", "simple-agent", "--health"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 60
    }
    # Turns in flight get 90s after SIGTERM (host.SHUTDOWN_GRACE).
    stopTimeout = 120
    # Reap the MCP server processes the agent starts.
    linuxParameters        = { initProcessEnabled = true }
    readonlyRootFilesystem = false

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.agent.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "agent"
      }
    }
  }])
}

resource "aws_ecs_service" "agent" {
  name            = var.name
  cluster         = aws_ecs_cluster.agent.id
  task_definition = aws_ecs_task_definition.agent.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  # Never two pollers at once: a deploy stops the old task before starting
  # the new one, so no mail is handled twice. The cost is a short gap.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = false
  }
}
