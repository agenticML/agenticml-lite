# AgenticML in your own AWS account

`agenticml.yaml` is one CloudFormation template. It creates an AgenticML
server with sign-up and sign-in (Amazon Cognito) behind an HTTPS load
balancer, and prints the URL. Nothing is sent to us.

What it creates: one Graviton instance in an Auto Scaling group of exactly
one, running `agenticml-lite[oidc]` from PyPI with SQLite on a separate
encrypted data volume (daily snapshots, kept after the stack is deleted); an
application load balancer with an ACM certificate; a Cognito user pool and
app client; and, unless you pass `VpcId`, a VPC with two public subnets.
There is no SSH and no key pair. Use AWS Systems Manager Session Manager for a
shell. Replacing the instance (a new `AgenticmlVersion`, or a failed one)
means a few minutes of downtime; the data stays on the volume.

You need an AWS account, a domain name, and either a Route 53 hosted zone for
it or an ACM certificate you already issued in the same region.

## Deploy

```
aws cloudformation deploy \
  --stack-name agenticml \
  --template-file deploy/aws-self-hosted/agenticml.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides DomainName=aml.example.com HostedZoneId=Z0123456789ABC AdminEmail=you@example.com
```

When it finishes (about 10 minutes), open the `Url` output, choose Sign in,
and sign up with the admin email. With a `CertificateArn` instead of a hosted
zone, create a CNAME from the domain to the `AlbDnsName` output. Connect a
training machine with the `SyncCommand` output.

## Parameters

| Parameter | Default | Meaning |
| --- | --- | --- |
| `DomainName` | required | Name of the server, for example `aml.example.com`. |
| `HostedZoneId` | empty | Route 53 zone: creates the certificate and the DNS record. Give this or `CertificateArn`. |
| `CertificateArn` | empty | Issued ACM certificate in this region. Give this or `HostedZoneId`. |
| `AdminEmail` | required | Signing up with it makes you the server administrator and owner of the `default` workspace. |
| `SignUp` | `invite-only` | `invite-only`, or `open` (every verified email gets its own workspace). |
| `AllowedEmailDomains` | empty | Comma-separated domains, no spaces. Only these domains (and the administrator) can sign up, in the server and in Cognito. Invitees must also be in these domains: Cognito refuses a sign-up from any other domain, invited or not. Empty means no restriction. |
| `CognitoDomainPrefix` | empty | Cognito sign-in domain prefix; empty derives one from the stack ID. |
| `AgenticmlVersion` | `3.3.1` | Version installed from PyPI. |
| `InstanceType` | `t4g.small` | Graviton instance type. |
| `DataVolumeSize` | `50` | Data volume in GiB (whole number from 10). A raised size applies to the volume at once and to the file system at the next instance restart or replacement. |
| `BackupRetentionDays` | `14` | Daily snapshots to keep. |
| `WorkspaceStorageGB` | `20` | Storage limit per workspace, in GB. Keep the total within `DataVolumeSize`. |
| `PeoplePerWorkspace` | `50` | Members plus pending invites per workspace. |
| `DataSnapshotId` | empty | Restore: ID of a snapshot of the old data volume, for a NEW stack only. |
| `Scheme` | `internet-facing` | `internal` keeps the load balancer private. |
| `AllowedCidr` | `0.0.0.0/0` | IPv4 range allowed to reach the load balancer. |
| `VpcId` | empty | Empty creates a VPC. Otherwise also give `PublicSubnetIds`. |
| `PublicSubnetIds` | empty | Two or more subnets of the existing VPC, comma-separated, exactly one per zone. The instance and the data volume use the first one; the server trusts the load balancer in these subnets. |
| `AssignPublicIp` | `true` | The instance needs internet access (PyPI, Cognito). Must be `true` with the VPC this stack creates; `false` only with a NAT gateway or endpoints. |

Outputs: `Url`, `FirstSteps`, `SyncCommand`, `AlbDnsName`, `DataVolumeId`,
`CognitoUserPoolId` and `SessionManagerCommand`.

Note: `AllowedEmailDomains` gates sign-up, not later changes. A user who is
already admitted can change to another verified email outside the list.

## Things not to change

- Never change the first entry of `PublicSubnetIds` (or its zone) after the
  stack exists. The data volume lives in that zone; CloudFormation would
  replace it with an empty one. To move, restore (below).
- Never set or change `DataSnapshotId` on an existing stack.

## Backups and restore

Daily snapshots of the data volume are kept for `BackupRetentionDays` and stay
after the stack is deleted, together with a final snapshot. They are
crash-consistent: the volume is copied while the server runs. After a restore,
run `server verify` over Session Manager, as the service user:

    sudo -u agenticml /opt/agenticml/bin/agenticml server verify --data /var/lib/agenticml-server

For a consistent copy at a moment of your choice, run `server backup --to DIR`
the same way, into an empty directory on a volume with room for it.

To restore, create a new stack with the same parameters and
`DataSnapshotId=snap-...` (the snapshot of the old data volume). The data comes
back; people sign in again with the same email (the new Cognito user pool has
new accounts, and the server relinks them by verified email).

The volume uses the account's default EBS encryption key. If your account's
default key is a customer-managed KMS key, the instance role needs grants on it
(it has none), so use the AWS-managed `aws/ebs` key as the default.

## Deleting the stack

The Cognito user pool is kept (deletion protection), and so are the data
volume's final snapshot and the daily snapshots.
