---
title: Privacy Policy
description: Privacy practices and data handling for the agenticml-lite hosted preview service.
---

Last updated: Nov 17, 2026

The agenticML project ("agenticML", "we", "us", or "our") respects your privacy. This Privacy Policy describes how we collect, store, use, and protect your information when you use the agenticml-lite hosted service at `https://app.agenticml.xyz` ("the Service").

**Self-hosted notice:** This Privacy Policy applies solely to our hosted service. When you run the open-source `agenticml-lite` server on your own infrastructure (self-hosted mode), your data remains entirely on your machines; we do not collect, receive, or store any information from self-hosted instances.

## 1. Information We Collect

### Account and identity information
- **Sign-in credentials:** When you sign up and sign in using Google OAuth or email/password through Firebase Authentication, we receive and store your email address (and whether it is verified), display name, and unique Firebase user identifier (`firebase_uid`).
- **Workspaces and roles:** We store your workspace memberships and assigned roles (`owner`, `admin`, `member`, `viewer`), along with pending workspace invitations.

### Machine credentials
- **Sync API keys:** API keys generated for machines and automated training runs are stored in the database as SHA-256 digests, together with the first 8 characters of the key (shown in the key list so you can tell keys apart). The full key is shown once, when it is created, and the server does not write it to its logs.

### Experiment and run data (Customer Data)
When your training scripts sync with the service using `agenticml`, the following data is uploaded to your workspace:
- **Run metadata:** Configurations, hyperparameters, execution parameters, and tags (`meta.json`).
- **Metrics:** Scalar values logged over time, along with aggregated summaries (last, minimum, maximum, and count per metric).
- **Run events:** An append-only log (`events.jsonl`) of what happens during a run, such as metadata updates and start and finish times.
- **Environment:** Machine hostname, operating system platform, Python version, and installed package versions.
- **Lineage and code:** Shadow Git repositories containing committed code snapshots and Git diffs associated with your runs.
- **Artifacts:** Content-addressed files, model weights, and media files (such as images or figures) logged during runs.

### Operational and security logs (Audit Trail)
- **Audit records:** The service records security-relevant events: sign-ins and sign-in failures, sign-outs, account creation, API key creation and revocation, member invitations, additions, removals and role changes, workspace creation, rename, suspension, archiving, plan changes and deletion, and run creation and status changes. The application only adds entries to the log, except that deleting an account redacts the server-level entries that name the person (see section 7). Events about a workspace's people, keys and runs are kept with that workspace and are deleted with it. Sign-in events and server-wide administration events are kept in a server-level log that is not deleted when a workspace is deleted. That server-level log has no fixed retention period; when you ask us to delete your account, we redact the entries that name you (your name, email address and IP address are replaced or cleared; the entry itself stays) (see section 7).
- **Technical data:** Each audit entry includes the time, who acted (their sign-in name, normally their email address, or the name of the API key), how they acted, the action, the target (for example another person's email, a run, a key or a workspace), a small JSON detail (for example a role, a workspace name or slug, a project name) and the client IP address.

## 2. Cookies and Tracking

- **Two essential cookies:** (1) `__Host-aml_session` (`Secure`, `HttpOnly`, `SameSite=Strict`), used only to keep you signed in, valid for 7 days. (2) `aml_ws` (`Secure`, `SameSite=Lax`, one year), which remembers the slug of the last workspace you opened so that we can open it when you return. It contains no account information. The dashboard also keeps display preferences (theme, code diff view, chart settings) in your browser's local storage. They are never sent to us.
- **No analytics or advertising cookies:** We do not use third-party web analytics tools (such as Google Analytics or PostHog), marketing pixels, or tracking cookies.

## 3. How We Use Your Information

We use the information we collect to:
- Operate, maintain, and provide the hosted experiment tracking service.
- Authenticate users, verify API keys, and enforce workspace isolation and role permissions.
- Track resource utilization against plan limits (5 GB storage, 2 GB daily uploads, 600 requests per minute, 5 people).
- Detect and prevent abuse, security threats, and unauthorized access.

**No model training:** We do not sell your personal data. We do not use your source code, models, experiment metrics, or customer data to train artificial intelligence or machine learning models.

## 4. Third-Party Subprocessors

We host and operate the service using the following third-party service providers:

- **Amazon Web Services (AWS):** Cloud hosting infrastructure in the United States (AWS region `us-east-2`, Ohio):
  - **AWS Application Load Balancer:** TLS termination and public traffic routing.
  - **Amazon ECS Fargate:** Running containerized server processes.
  - **Amazon RDS PostgreSQL:** Storing accounts, workspaces, keys, sessions, audit events, metric summaries, and committed file sizes.
  - **Amazon EFS:** Storing raw experiment run files and lineage Git repositories.
  - **Amazon S3:** Storing content-addressed artifact objects and media blobs.
  - **AWS Secrets Manager and Amazon CloudWatch:** Secrets Manager holds the database password; CloudWatch keeps server application logs and health alarms.
  - **AWS Backup:** Daily backup copies of the database and the EFS file system, kept for up to 35 days.
  - **Other AWS services** (Elastic Container Registry for the server image, SNS and Budgets for operator alert emails, Certificate Manager, KMS) process no Customer Data.
- **Google Firebase Authentication:** Sign-in. When you sign in, your browser talks to Google directly (`apis.google.com`, `googleapis.com` and the Firebase sign-in domain), so Google receives your IP address and browser details, and the email and password you type for email sign-in. We never receive your password; we receive a signed Firebase token that we verify, and we store your email, display name and Firebase user ID from it. The dashboard itself loads no Google or other third-party code.

## 5. Security and Staff Access

- **Encryption:** Connections from your browser or client to the load balancer use TLS 1.2 or 1.3; plain HTTP requests are redirected to HTTPS. Inside our AWS network, connections to the database, file system and S3 are encrypted, and the load balancer forwards requests to the server over plain HTTP on a private network. Data at rest is encrypted (RDS storage encryption, EFS filesystem encryption, and S3 server-side encryption with AES256).
- **Multi-tenancy isolation:** Workspaces are separated in the application: every request is checked against the signed-in account's membership, run files are stored in per-workspace directories, and S3 objects are stored under per-workspace prefixes with no sharing of objects between workspaces. Database tables that hold workspace data also enforce PostgreSQL row-level security, as a second check.
- **Staff access boundaries:** Server administrators have no access to the contents of your workspace (runs, metrics, code, diffs, artifacts, keys, members) through the web dashboard or the API unless they are a member of that workspace, and they cannot add themselves to an existing workspace. They can see account-level information on the admin page: each workspace's name and slug, plan, storage used, daily upload volume, number of members, and creation date, plus a server-wide activity log with sign-in and administration events, including email addresses and IP addresses. Server administrators can create, suspend, archive, restore and rename workspaces and change their plan and limits. If you want support to see your workspace, you add our support account as a member or admin in Settings › Members and remove it when done. Adding it and changing its role are recorded in your workspace's Activity log; what a member views is not logged. People who operate the AWS infrastructure can technically access stored data directly (database, file system and S3) and can run administration commands; this access is not recorded in your Activity log.

## 6. Data Retention and Backups

- **Active data:** Your experiment data is retained for as long as your workspace remains active.
- **Deletion:** When a workspace is deleted, its database records (members, invitations, keys, runs, metric summaries and the workspace's Activity log), its run files on EFS and its objects in S3 are deleted. This does not by itself remove your sign-in account. If removing S3 objects fails, the server logs a warning naming the workspace, and the operator retries the removal with a cleanup command once the problem is fixed; the retry is not automatic. Copies in backups and in older S3 versions are removed by their retention schedules, about 35 days at most, once the workspace's S3 objects have been deleted. A record of the deletion (who ran it, when, and the workspace id and slug) is kept in the server audit log. Workspace deletion is carried out by an operator; there is no self-serve delete button.
- **Backups:**
  - Automated RDS PostgreSQL snapshots with 7-day Point-in-Time Recovery (PITR) and daily AWS Backup retained for up to 35 days.
  - Amazon EFS daily AWS Backup retained for up to 35 days.
  - Amazon S3 object versioning with noncurrent versions retained for 30 days.
  - Load balancer access logs (which include client IP addresses) are kept in Amazon S3 for 90 days. Server application logs are kept in Amazon CloudWatch Logs for 30 days.
  - Backups age out automatically according to these schedules. Backups are maintained on a best-effort basis as part of the public preview; restore drills have not yet been completed, and we make no tested-restore or multi-region guarantees.

## 7. Your Rights and Deletion Requests

- **Access and export:** You can view, inspect, and download your experiment metrics, run files, and artifacts at any time through the dashboard or by using the `agenticml` CLI and Python SDK.
- **Account and data deletion:** You may ask us to delete your workspaces, and your account record (email, display name and sign-in identifier), by contacting [contact@agenticml.xyz](mailto:contact@agenticml.xyz). There is no self-serve deletion; we carry the request out by hand, including removing your user from Firebase Authentication and redacting server-level audit log entries that name you (your name, email address and IP address are replaced or cleared, rather than the entries being deleted). Load balancer access logs (90 days) and server logs (30 days) are not deleted on request and expire on their own schedule.
- **Team management:** Workspace administrators can manage team members, revoke keys, and remove user memberships directly in the workspace settings.

## 8. Children's Privacy

The service is not directed to individuals under the age of 16 years. We do not knowingly collect personal information from children under this age. If we become aware that a child has provided us with personal information, we will take steps to delete that information promptly.

## 9. Changes to This Privacy Policy

We may update this Privacy Policy from time to time. When changes are made, we will update the "Last updated" date at the top of this document. Continued use of the service after any modification constitutes acceptance of the updated policy.

## 10. Contact Us

- **Privacy inquiries and deletion requests:** Email [contact@agenticml.xyz](mailto:contact@agenticml.xyz).
- **General technical support:** Submit an issue on GitHub Issues at [agenticML/agenticml-lite/issues](https://github.com/agenticML/agenticml-lite/issues). For the hosted service, put "hosted" in the title.
