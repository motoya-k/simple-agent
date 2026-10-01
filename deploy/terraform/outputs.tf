output "ecr_repository_url" {
  description = "Push the image here."
  value       = aws_ecr_repository.agent.repository_url
}

output "secrets_to_fill" {
  description = "Secrets created empty; set their values outside Terraform."
  value = {
    for name, secret in aws_secretsmanager_secret.env : name => secret.arn
    if !(name == "SIMPLE_AGENT_DATABASE_URL" && var.create_database)
  }
}

output "cluster" {
  value = aws_ecs_cluster.agent.name
}

output "service" {
  value = aws_ecs_service.agent.name
}

output "log_group" {
  value = aws_cloudwatch_log_group.agent.name
}

output "task_role_arn" {
  description = "The role the agent runs as (Bedrock access is granted here)."
  value       = aws_iam_role.task.arn
}
