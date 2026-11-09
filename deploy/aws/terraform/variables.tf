variable "region" {
  description = "AWS region."
  type        = string
  default     = "us-east-2"
}
variable "zone_name" {
  description = "Existing public Route 53 zone."
  type        = string
  default     = "agenticml.xyz"
}
variable "hostname" {
  description = "Hosted service DNS name."
  type        = string
  default     = "app.agenticml.xyz"
}
variable "public_url" {
  description = "Canonical HTTPS origin; match hostname."
  type        = string
  default     = "https://app.agenticml.xyz"
}
variable "vpc_cidr" {
  description = "Dedicated /16 VPC; proxy trust is limited to its private subnets."
  type        = string
  default     = "10.42.0.0/16"
}
variable "firebase_project" {
  description = "Firebase project ID."
  type        = string
}
variable "firebase_api_key" {
  description = "Public Firebase web API key; not a secret."
  type        = string
}
variable "open_signup" {
  description = "Enable only after owner verifies Firebase and free limits."
  type        = bool
  default     = false
}
variable "multi_az" {
  description = "Enable RDS Multi-AZ when required."
  type        = bool
  default     = false
}
variable "objects_bucket_name" {
  description = "Globally unique private objects bucket name."
  type        = string
}
variable "logs_bucket_name" {
  description = "Globally unique ALB log bucket name."
  type        = string
}
variable "alarm_email" {
  description = "SNS alarm email; owner must confirm subscription."
  type        = string
}
variable "github_oidc_provider_arn" {
  description = "Existing GitHub OIDC provider ARN, or null to create it."
  type        = string
  default     = null
}
variable "image_tag" {
  description = "Initial private ECR image tag; push before starting service."
  type        = string
  default     = "bootstrap"
}
variable "desired_count" {
  description = "Use 0 for initial apply before first image push, then 2."
  type        = number
  default     = 2
}
variable "final_snapshot_identifier" {
  description = "Unique RDS final snapshot name; change before each destruction."
  type        = string
}

check "public_origin" {
  assert {
    condition     = var.public_url == "https://${var.hostname}"
    error_message = "public_url must be the HTTPS origin of hostname."
  }
}
