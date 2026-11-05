variable "vpc_id" {
  type        = string
  description = "Existing VPC ID."
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "Two private subnet IDs for the Fargate task and EFS mount targets. A NAT gateway is required to pull the image from ghcr.io; VPC endpoints cannot reach ghcr.io and only cover CloudWatch Logs and Secrets Manager. To skip NAT, mirror the image to ECR (or use an ECR pull-through cache) and change local.image."

  # Subnets must be in different AZs: EFS allows only one mount target per AZ.
  validation {
    condition     = length(var.private_subnet_ids) >= 2
    error_message = "Provide at least two private subnet IDs in different Availability Zones."
  }
}

variable "public_subnet_ids" {
  type        = list(string)
  description = "Two public subnet IDs for the internet-facing ALB."

  validation {
    condition     = length(var.public_subnet_ids) >= 2
    error_message = "Provide at least two public subnet IDs in different Availability Zones."
  }
}

variable "certificate_arn" {
  type        = string
  description = "ACM certificate ARN for hostname (must be in this region)."
}

variable "hostname" {
  type        = string
  description = "Public DNS name for the server, with no scheme (for example aml.example.com). Used as AGENTICML_SERVER_PUBLIC_URL. Point an alias at the ALB."
}

variable "allowed_cidrs" {
  type        = list(string)
  description = "CIDR blocks allowed to reach the ALB on ports 80 and 443."
  default     = ["0.0.0.0/0"]
}
