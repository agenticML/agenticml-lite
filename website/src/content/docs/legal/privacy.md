---
title: Privacy Policy
description: Privacy practices and data handling for the agenticml-lite hosted preview service.
draft: true
---

Last updated: [[OWNER: date]]

[[OWNER: operator name or legal entity]] ("we", "us", or "our") respects your privacy. This Privacy Policy describes how we collect, store, use, and protect your information when you use the agenticml-lite hosted service at `https://app.agenticml.xyz` ("the Service").

**Self-hosted notice:** This Privacy Policy applies solely to our hosted service. When you run the open-source `agenticml-lite` server on your own infrastructure (self-hosted mode), your data remains entirely on your machines; we do not collect, receive, or store any information from self-hosted instances.

## 1. Information We Collect

### Account and identity information
- **Sign-in credentials:** When you sign up and sign in using Google OAuth or email/password through Firebase Authentication, we receive and store your email address (and whether it is verified), display name, and unique Firebase user identifier (`firebase_uid`).
- **Workspaces and roles:** We store your workspace memberships and assigned roles (`owner`, `admin`, `member`, `viewer`), along with pending workspace invitations.

### Machine credentials
- **Sync API keys:** API keys generated for machines and automated training runs are stored in the database exclusively as cryptographic SHA-256 digests. Plaintext keys are displayed only once upon generation and are never logged or stored.

### Experiment and run data (Customer Data)
When your training scripts sync with the service using `agenticml`, the following data is uploaded to your workspace:
- **Run metadata:** Configurations, hyperparameters, execution parameters, and tags (`meta.json`).
- **Metrics:** Scalar values logged over time, along with aggregated summaries (last, minimum, maximum, and count per metric).
- **Run events:** Config and metadata updates, code snapshots, artifacts, and start and finish times.
- **Environment:** Machine hostname, operating system platform, Python version, and installed package versions.
- **Lineage and code:** Shadow Git repositories containing committed code snapshots and Git diffs associated with your runs.
- **Artifacts:** Content-addressed files, model weights, and media files (such as images or figures) logged during runs.

### Operational and security logs (Audit Trail)
- **Audit records:** The service maintains an append-only audit log (`audit_events`) that records security-relevant actions, including sign-ins, API key issuance and revocation, member invitations and role changes, workspace creation and deletion, and plan updates.
- **Technical data:** Audit entries include the timestamp, the acting account ID, the action taken, and the client IP address.

## 2. Cookies and Tracking

- **One essential session cookie:** `__Host-aml_session` (`Secure`, `HttpOnly`, `SameSite=Strict`), used only to keep you signed in. The dashboard also keeps display preferences (theme, code diff view, chart settings) in your browser's local storage. They are never sent to us.
- **No analytics or advertising cookies:** We do not use third-party web analytics tools (such as Google Analytics or PostHog), marketing pixels, or tracking cookies.

## 3. How We Use Your Information

We use the information we collect to:
- Operate, maintain, and provide the hosted experiment tracking service.
- Authenticate users, verify API keys, and enforce workspace isolation and role permissions.
- Track resource utilization against plan limits (5 GB storage, 2 GB daily uploads, 600 requests per minute, 5 people).
- Detect and prevent abuse, security threats, and unauthorized access.

**No model training:** We do not sell your personal data. We do not use your source code, models, experiment metrics, or customer data to train artificial intelligence or machine learning models. [[OWNER: confirm these commitments before publishing]]

## 4. Third-Party Subprocessors

We host and operate the service using the following third-party service providers:

- **Amazon Web Services (AWS):** Cloud hosting infrastructure in the United States (AWS region `us-east-2`, Ohio):
  - **AWS Application Load Balancer:** TLS termination and public traffic routing.
  - **Amazon ECS Fargate:** Running containerized server processes.
  - **Amazon RDS PostgreSQL:** Storing accounts, workspaces, keys, sessions, audit events, metric summaries, and committed file sizes.
  - **Amazon EFS:** Storing raw experiment run files and lineage Git repositories.
  - **Amazon S3:** Storing content-addressed artifact objects and media blobs.
  - **AWS Secrets Manager and CloudWatch:** Managing server secrets and health alarms.
- **Google Firebase:** User authentication and identity token verification (Firebase Authentication).

## 5. Security and Staff Access

- **Encryption:** Traffic to the service is encrypted in transit with TLS 1.2 or 1.3. Data at rest is encrypted (RDS storage encryption, EFS filesystem encryption, and S3 server-side encryption with AES256).
- **Multi-tenancy isolation:** Workspaces are strictly partitioned. Data queries enforce PostgreSQL Row-Level Security (RLS), and S3 objects are stored under workspace-specific prefixes without cross-workspace deduplication.
- **Staff access boundaries:** Server administrators do not have ambient read access to your workspace data, runs, code diffs, or artifacts. Through the service, support personnel can only access your workspace if you explicitly invite our support account as a member or admin. Adding that account, and any change to its role, is recorded in the workspace audit log. Operators may still need to access the underlying infrastructure to run, repair or secure the service. [[OWNER: confirm for lawyer]]

## 6. Data Retention and Backups

- **Active data:** Your experiment data is retained for as long as your workspace remains active.
- **Deletion:** When a workspace is deleted, its database records, EFS run files, and S3 objects are deleted. If removing S3 objects fails, the failure is logged. [[OWNER: confirm how failed storage cleanup is handled]] Copies in backups and old S3 versions age out within 35 days. A record of the deletion is written to the server audit log.
- **Backups:**
  - Automated RDS PostgreSQL snapshots with 7-day Point-in-Time Recovery (PITR) and daily AWS Backup retained for up to 35 days.
  - Amazon EFS daily AWS Backup retained for up to 35 days.
  - Amazon S3 object versioning with noncurrent versions retained for 30 days.
  - Load balancer access logs (which include client IP addresses) are kept in Amazon S3 for 90 days. Server application logs are kept in Amazon CloudWatch Logs for 30 days.
  - Backups age out automatically according to these schedules. Backups are maintained on a best-effort basis as part of the public preview; restore drills have not yet been completed, and we make no tested-restore or multi-region guarantees.

## 7. Your Rights and Deletion Requests

- **Access and export:** You can view, inspect, and download your experiment metrics, run files, and artifacts at any time through the dashboard or by using the `agenticml` CLI and Python SDK.
- **Account and data deletion:** You may request complete deletion of your account, personal data, and workspaces by contacting us at [[OWNER: contact address]].
- **Team management:** Workspace administrators can manage team members, revoke keys, and remove user memberships directly in the workspace settings.

## 8. Children's Privacy

The service is not directed to individuals under the age of [[OWNER: minimum age]] years. We do not knowingly collect personal information from children under this age. If we become aware that a child has provided us with personal information, we will take steps to delete that information promptly.

## 9. Changes to This Privacy Policy

We may update this Privacy Policy from time to time. When changes are made, we will update the "Last updated" date at the top of this document. Continued use of the service after any modification constitutes acceptance of the updated policy.

## 10. Contact Us

- **Privacy inquiries and deletion requests:** Email [[OWNER: contact address]].
- **General technical support:** Submit an issue on GitHub Issues at [agenticML/agenticml-lite/issues](https://github.com/agenticML/agenticml-lite/issues). For the hosted service, put "hosted" in the title.
