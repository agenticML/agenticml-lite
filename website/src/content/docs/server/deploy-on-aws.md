---
title: Deploy on AWS
description: Deploy an AgenticML server in your own AWS account using CloudFormation.
sidebar:
  order: 2.6
---

Deploy an AgenticML server in your own AWS account with a single CloudFormation template. The stack creates an HTTPS load balancer, a single Graviton EC2 instance with SQLite on an encrypted data volume, and Amazon Cognito for sign-up and sign-in.

## What you get

```
browsers and          ──HTTPS :443──▶ Application Load Balancer ──HTTP :8080──▶ EC2 instance (t4g.small)
training machines                     (ACM certificate)                          agenticml server serve
                                                                                 SQLite on encrypted EBS volume
                      Amazon Cognito (sign-up / sign-in) ◀─────────────────────┘
```

The template creates:
- One Graviton EC2 instance (default `t4g.small`) in an Auto Scaling group of 1, running `agenticml-lite[oidc]` from PyPI.
- An encrypted gp3 EBS data volume for SQLite database storage and run files, with daily snapshots.
- An Application Load Balancer (ALB) terminating HTTPS with an AWS Certificate Manager (ACM) certificate.
- An Amazon Cognito user pool and public app client managing sign-up, email verification, and sign-in.
- An optional VPC with two public subnets in two availability zones, or deployment into an existing VPC.
- Instance access via AWS Systems Manager Session Manager. Port 22 is closed and no SSH keys are configured.

## What you need

- An AWS account with permissions to create IAM roles, EC2 instances, load balancers, and Cognito resources (`CAPABILITY_IAM`).
- A domain name with a Route 53 hosted zone, or an existing ACM certificate ARN in the same AWS region.
- An administrator email address.

## Cost

Running the server costs about $45–55 a month.

Estimated monthly breakdown:
- EC2 instance (`t4g.small` Graviton): ~$12–15
- Application Load Balancer: ~$16–22
- EBS gp3 data volume (50 GiB) and root volume: ~$4–6
- EBS snapshots (daily DLM retention): ~$1–3
- Public IPv4 addresses: ~$7–10
- Amazon Cognito: free up to 10,000 monthly active users on the Lite tier

This is an estimate, check the AWS pricing pages for current rates in your region.

## Launch

You can deploy using the AWS CLI or the AWS Management Console. A one-click Launch Stack link will come later.

### CLI

Run it from a clone of the agenticml-lite repository, or save the template and change `--template-file`. When you pass `CertificateArn`, the ACM certificate must be in that region.

Run `aws cloudformation deploy`:

```bash
aws cloudformation deploy \
  --stack-name agenticml \
  --template-file deploy/aws-self-hosted/agenticml.yaml \
  --region <region> \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides DomainName=aml.example.com HostedZoneId=Z0123456789ABC AdminEmail=you@example.com
```

### Console

1. Download `agenticml.yaml` from the [latest GitHub release](https://github.com/agenticML/agenticml-lite/releases/latest/download/agenticml.yaml), or from the release for your version.
2. Open the AWS CloudFormation console.
3. Select **Create stack** > **With new resources (standard)**.
4. Under **Prerequisite - Prepare template**, select **Choose an existing template**, choose **Upload a template file**, and select `agenticml.yaml`.
5. Enter a stack name and fill in the required parameters.
6. Under **Capabilities**, select the checkbox acknowledging IAM resource creation (`CAPABILITY_IAM`).
7. Select **Submit**. Creation takes about 10 minutes.

If using an existing ACM certificate instead of a Route 53 hosted zone, add a CNAME record in your DNS provider pointing your domain to the `AlbDnsName` output.

## Parameters

| Parameter | Default | Meaning |
| --- | --- | --- |
| `DomainName` | required | Name people use to reach the server, for example `aml.example.com`. |
| `HostedZoneId` | empty | Route 53 zone: creates the certificate and DNS record. Give this or `CertificateArn`. |
| `CertificateArn` | empty | Issued ACM certificate in this region. Give this or `HostedZoneId`. |
| `AdminEmail` | required | Signing up with it makes you server administrator and owner of the `default` workspace. |
| `SignUp` | `invite-only` | `invite-only`, or `open` (every verified email gets its own workspace). |
| `AllowedEmailDomains` | empty | Comma-separated domains, no spaces. Only these domains, and the `AdminEmail`, can sign up through Cognito, in both modes. The server additionally applies it to personal workspaces when `SignUp=open`. Domains match exactly (no subdomains). Invitees must be in these domains, because Cognito refuses other domains. Empty means no restriction. |
| `CognitoDomainPrefix` | empty | Cognito sign-in domain prefix; empty derives one from the stack ID. |
| `AgenticmlVersion` | `3.4.0` | Version installed from PyPI. |
| `InstanceType` | `t4g.small` | Graviton instance type. |
| `DataVolumeSize` | `50` | Data volume in GiB (whole number from 10). A raised size applies to the volume at once and to the file system at the next instance restart or replacement. |
| `BackupRetentionDays` | `14` | Daily snapshots to keep. |
| `WorkspaceStorageGB` | `20` | Storage limit per workspace, in GB. Keep the total within `DataVolumeSize`. |
| `PeoplePerWorkspace` | `50` | Members plus pending invites per workspace. |
| `DataSnapshotId` | empty | Restore: ID of a snapshot of the old data volume, for a NEW stack only. |
| `Scheme` | `internet-facing` | `internal` keeps the load balancer private. |
| `AllowedCidr` | `0.0.0.0/0` | IPv4 range allowed to reach the load balancer. |
| `VpcId` | empty | Empty creates a VPC. Otherwise also give `PublicSubnetIds`. |
| `PublicSubnetIds` | empty | Two or more subnets of the existing VPC, comma-separated, exactly one per zone. The instance and data volume use the first one; the server trusts the load balancer in these subnets. |
| `AssignPublicIp` | `true` | The instance needs internet access (PyPI, Cognito). Must be `true` with the VPC this stack creates; `false` only with a NAT gateway or endpoints. |

## First sign-in

When stack creation finishes, open the `Url` output in a browser and choose **Sign in**:

- In invite-only mode (`SignUp=invite-only`), the admin email becomes owner of the `default` workspace at first boot and signs up through the Cognito page.
- In open mode (`SignUp=open`), every verified email gets its own personal workspace.
- `AllowedEmailDomains`: only these domains, and the `AdminEmail`, can sign up through Cognito, in both modes. The server additionally applies it to personal workspaces when `SignUp=open`. Domains match exactly (no subdomains). Invitees must be in these domains, because Cognito refuses other domains.
- The Cognito app client lets users write `email` and `name`, and a new email must be verified before it takes effect. `AllowedEmailDomains` gates sign-up only, so an admitted user could later switch to another verified email outside those domains.

## Inviting people

To invite team members:
1. Open the dashboard and navigate to **Settings** > **Members**.
2. Enter the person's email address and assign a workspace role (`viewer`, `member`, or `admin`).
3. Copy the invite link (`/invite#token=...`).
4. Send the link to the invitee. When they open it, they sign in or sign up through Cognito using that email.

## Connecting training machines

Install `agenticml-lite` on the training machine:

```bash
pip install agenticml-lite
```

Log in using the server URL:

```bash
agenticml login --host https://aml.example.com
```

Enter your API key at the prompt. Generate an API key from the dashboard under **Settings** > **API keys**. Never put an API key directly on the command line.

Synchronize training runs:

```bash
agenticml sync --follow
```

## Upgrades

To upgrade the server:
1. Perform a CloudFormation stack update with a new `AgenticmlVersion`.
2. The Auto Scaling rolling update replaces the instance with the new version.
3. Upgrades cause a few minutes of downtime while the instance is replaced.
4. Server data stays on the EBS volume and is attached to the new instance automatically.

## Backups and restore

- Daily Amazon Data Lifecycle Manager (DLM) snapshots are created automatically and kept for `BackupRetentionDays` days. Snapshots are crash-consistent (taken while the server runs).
- To restore, create a new stack with `DataSnapshotId` set to the snapshot ID of the old data volume. People sign in again with the same email; the server relinks their accounts by verified email. Afterwards run `agenticml server verify` over Session Manager:
  ```bash
  sudo -u agenticml /opt/agenticml/bin/agenticml server verify --data /var/lib/agenticml-server
  ```
- For a consistent copy at a chosen time, run `agenticml server backup --to DIR` over Session Manager. The target must be empty or absent and outside the data directory; it sits on the instance's root volume, which is replaced with the instance; so copy it off the instance (for example to S3) and check there is room. First create the directory:
  ```bash
  sudo install -d -o agenticml -g agenticml -m 0700 /var/lib/agenticml-backup
  ```
  Then run the backup:
  ```bash
  sudo -u agenticml /opt/agenticml/bin/agenticml server backup --to /var/lib/agenticml-backup --data /var/lib/agenticml-server
  ```

## Growing the disk

To expand disk capacity, increase `DataVolumeSize` in a stack update. CloudFormation expands the EBS volume immediately. The XFS file system grows automatically at the next instance restart or replacement. An EBS volume cannot be shrunk.

## Private-only (Scheme=internal)

To run the server on an internal network without public exposure, set `Scheme=internal` and configure `AllowedCidr` to your private network range. Deploy into an existing VPC with `VpcId` and `PublicSubnetIds`. `Scheme=internal` still needs the instance to reach PyPI and Cognito (public endpoints, NAT or VPC endpoints), and Cognito's sign-in page is on a public hosted domain, so this is not an air-gapped setup. See [Private networks](/server/private-network/) for proxy and client network requirements.

## Operating it

Connect to the instance using AWS Systems Manager Session Manager (SSH is disabled and port 22 is closed):

```bash
aws ssm start-session --region <region> --target <instance-id>
```

You can copy the exact command from the `SessionManagerCommand` stack output.

Operational files on the instance:
- Bootstrap log: `/var/log/agenticml-bootstrap.log`
- Service journal: `sudo journalctl -u agenticml-server`
- Environment configuration: `sudo cat /etc/agenticml-server.env`

## Deleting the stack

When you delete the CloudFormation stack:
- The EBS data volume creates a final snapshot before deletion (`DeletionPolicy: Snapshot`), and daily DLM snapshots remain.
- The Cognito user pool is retained (`DeletionPolicy: Retain`) and has deletion protection set to `ACTIVE`; the pool's domain and app client are deleted with the stack.

To delete them permanently by hand:
1. Open the Amazon EC2 console, navigate to **Snapshots**, and delete the final and daily snapshots.
2. Open the Amazon Cognito console, select the user pool, turn off deletion protection in pool settings, and delete the user pool.

## Limits and caveats

- **Never change the first subnet or its zone:** The data volume resides in the availability zone of the first subnet listed in `PublicSubnetIds`. Changing the first subnet or its zone causes CloudFormation to replace the data volume with an empty volume. When using an existing VPC, `PublicSubnetIds` must list exactly one subnet per availability zone, across at least two zones.
- **Cognito email limits:** Cognito's default email sender is limited to 50 verification and password reset emails per day. Configure Amazon SES in the Cognito user pool for production workloads.
- **Customer-managed EBS KMS keys:** The volumes are encrypted with the account's default EBS key. The instance role has no KMS permissions, which is enough for the AWS-managed `aws/ebs` key. If your account default is a customer-managed KMS key, grant the instance role and the Auto Scaling service-linked role use of that key, or switch the default to `aws/ebs`.
- **PyPI version:** The version set in `AgenticmlVersion` must be published on PyPI.
