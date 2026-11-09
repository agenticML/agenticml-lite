terraform {
  required_version = ">= 1.6.0, < 2.0.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0, >= 5.81.0"
    }
  }
  backend "s3" {}
}
provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "agenticml-hosted" }
  }
}
data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
locals {
  name = "agenticml-hosted"
}
