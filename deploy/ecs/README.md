# ECS Fargate example

Example stack for one AgenticML server task. Adapt it; it is not a module.
The server holds a single-writer lock, so this always runs exactly one task
(`desired_count = 1`, `deployment_minimum_healthy_percent = 0`,
`deployment_maximum_percent = 100`). Two tasks on the same EFS will fail.

`task-definition.json` is the same task shape if you are not using Terraform.
Replace the placeholders (`<account-id>`, `<region>`, `fs-XXXXXXXX`,
`fsap-XXXXXXXX`). The image entrypoint is `agenticml server serve`; do not
override the command.

## What it creates

- Encrypted EFS with mount targets in the private subnets and an access
  point (`/agenticml`, UID/GID 10001, mode 0750) mounted at `/data`
- Secrets Manager secret for `AGENTICML_SERVER_KEY` (value set out of band)
- ECS cluster, Fargate task (0.5 vCPU / 1 GiB, image
  `ghcr.io/agenticml/agenticml-server:2.0.0`, user 10001, read-only root
  plus `/tmp`), and a service
- Internet-facing ALB: HTTPS 443 with your ACM certificate, HTTP 80
  redirects to 443, target group health check `GET /healthz` on port 8080
- Security groups: ALB from `allowed_cidrs`; task only from the ALB on
  8080; EFS only from the task on 2049
- CloudWatch log group `/ecs/agenticml-server`
- AWS Backup plan: daily EFS snapshots, 35-day retention

## Variables

| Name | Description |
| --- | --- |
| `vpc_id` | Existing VPC |
| `private_subnet_ids` | Two private subnets (task + EFS), in different AZs. A NAT gateway is required to pull from ghcr.io; VPC endpoints cannot reach ghcr.io and only cover CloudWatch Logs and Secrets Manager. To skip NAT, mirror the image to ECR (or use an ECR pull-through cache) and change `local.image` |
| `public_subnet_ids` | Two public subnets (ALB) |
| `certificate_arn` | ACM certificate ARN for `hostname` |
| `hostname` | Public DNS name, no scheme (`aml.example.com`) |
| `allowed_cidrs` | CIDRs allowed to reach the ALB (default `0.0.0.0/0`) |

Region comes from `AWS_REGION` / your AWS profile.

Example `terraform.tfvars` (do not commit; `*.tfvars` is gitignored):

    vpc_id             = "vpc-xxxxxxxx"
    private_subnet_ids = ["subnet-aaa", "subnet-bbb"]
    public_subnet_ids  = ["subnet-ccc", "subnet-ddd"]
    certificate_arn    = "arn:aws:acm:us-east-1:123456789012:certificate/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    hostname           = "aml.example.com"
    allowed_cidrs      = ["203.0.113.0/24"]

Terraform loads `terraform.tfvars` automatically. Or pass
`-var-file=terraform.tfvars`.

## Apply

Create the secret first, put a value, then apply the rest. A first full
apply would start the task before the secret has a value, and ECS cannot
inject an empty secret.

    cd deploy/ecs/terraform
    terraform init
    terraform apply -target=aws_secretsmanager_secret.server_key
    KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
    aws secretsmanager put-secret-value \
      --secret-id agenticml-server-key \
      --secret-string "$KEY"
    terraform apply

That string is the admin key named `env`. Sign in to the dashboard with it.
It cannot be revoked from the Keys page. Save `$KEY` in a password manager.
You can read it back with:

    aws secretsmanager get-secret-value \
      --secret-id agenticml-server-key \
      --query SecretString \
      --output text

Point `hostname` at the ALB (`terraform output alb_dns_name`) with an
alias or CNAME.

If the task already started without a secret value, put the value then
force a new deployment:

    aws ecs update-service \
      --cluster "$(terraform output -raw cluster_name)" \
      --service "$(terraform output -raw service_name)" \
      --force-new-deployment

## Setup URL instead

To use the first-run setup URL, remove the `AGENTICML_SERVER_KEY` secret
from the task definition so the env key is not injected. Terraform users
remove the `secrets` block in `main.tf` and the Secret statement in the
execution role policy. Then:

    aws logs tail "$(terraform output -raw log_group)" --follow

Look for `No admin key yet. Open <url>/setup?token=<token>`.

## Cost

The ALB (and a NAT gateway, if the VPC does not already have one) dominate.
Fargate 0.5 vCPU / 1 GiB, EFS, AWS Backup, Secrets Manager, and CloudWatch
logs are small beside that.
