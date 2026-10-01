variable "name" {
  description = "Prefix for every resource."
  type        = string
  default     = "simple-agent"
}

variable "region" {
  type    = string
  default = "ap-northeast-1"
}

# -- network: an existing VPC ------------------------------------------------
variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  description = "Subnets for the task and the database. They need outbound access (NAT or VPC endpoints) to Bedrock, the IMAP server, ECR, CloudWatch Logs, Secrets Manager, and any MCP server's API."
  type        = list(string)
}

# -- the container -----------------------------------------------------------
variable "image" {
  description = "Image to run, e.g. <account>.dkr.ecr.ap-northeast-1.amazonaws.com/simple-agent:<tag>. Usually a derived image that adds your MCP servers."
  type        = string
}

variable "cpu_architecture" {
  description = "X86_64 or ARM64 — must match the image."
  type        = string
  default     = "X86_64"
}

variable "cpu" {
  type    = number
  default = 512
}

variable "memory" {
  type    = number
  default = 1024
}

variable "environment" {
  description = "Extra non-secret environment variables (e.g. SIMPLE_AGENT_IMAP_HOST, SIMPLE_AGENT_EMAIL_ALLOW)."
  type        = map(string)
  default     = {}
}

variable "extra_secret_names" {
  description = "Environment variables to read from Secrets Manager, besides the IMAP password and database URL (e.g. GOOGLE_SERVICE_ACCOUNT_KEY_JSON). One empty secret is created per name; set its value outside Terraform."
  type        = list(string)
  default     = []
}

# -- model -------------------------------------------------------------------
variable "model" {
  description = "Bedrock inference profile the agent uses."
  type        = string
  default     = "jp.anthropic.claude-sonnet-4-6"
}

variable "review_model" {
  type    = string
  default = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "bedrock_model_regions" {
  description = "Regions the jp. inference profiles route to."
  type        = list(string)
  default     = ["ap-northeast-1", "ap-northeast-3"]
}

# -- database ----------------------------------------------------------------
variable "create_database" {
  description = "Create an RDS Postgres instance. Set false and fill the database URL secret yourself to use an existing one."
  type        = bool
  default     = true
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "log_retention_days" {
  type    = number
  default = 90
}
