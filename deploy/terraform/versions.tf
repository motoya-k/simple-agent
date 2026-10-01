terraform {
  required_version = ">= 1.6"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 6.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
  # Keep state in an encrypted S3 bucket: it holds the generated database
  # password. Configure with `terraform init -backend-config=...`.
  backend "s3" {}
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = var.name, ManagedBy = "terraform" }
  }
}
