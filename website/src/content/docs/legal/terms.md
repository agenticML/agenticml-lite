---
title: Terms of Service
description: Terms governing the use of the agenticml-lite hosted preview service.
draft: true
---

Last updated: [[OWNER: date]]

Welcome to the agenticml-lite hosted service (`https://app.agenticml.xyz`), operated by [[OWNER: operator name or legal entity]] ("we", "us", or "our").

These Terms of Service ("Terms") govern your access to and use of our hosted service. By signing up, accessing, or using the service, you agree to be bound by these Terms, our [Acceptable Use Policy](/agenticml-lite/legal/acceptable-use/), and our [Privacy Policy](/agenticml-lite/legal/privacy/). If you do not agree to these Terms, do not use the service.

## 1. The Service and Public Preview

The service provides cloud-hosted machine learning experiment tracking, metric visualization, and run synchronization for the open-source `agenticml-lite` software.

**Public preview:** The service is currently offered as a public preview on a best-effort basis. We do not provide a service level agreement (SLA) or guarantee uptime. The infrastructure operates in a single cloud region (AWS `us-east-2`) without multi-region redundancy. We may modify, pause, or retire features or the service at any time with or without notice.

## 2. Eligibility and Account Registration

- **Minimum age:** You must be at least [[OWNER: minimum age]] years old to create an account or use the service.
- **Authentication:** You register and sign in using Google OAuth or a verified email address via Firebase Authentication. You must maintain a valid, verified email address.
- **Account security:** You are responsible for all activities that occur under your account. You must keep your credentials and API keys secure. API keys are displayed only once upon creation and cannot be retrieved later. If you suspect unauthorized access to your account or keys, revoke the affected keys immediately and notify us.

## 3. Workspaces and Team Access

- **Workspaces:** Data on the service is organized into isolated workspaces. Each workspace has assigned roles: `owner`, `admin`, `member`, and `viewer`.
- **Role responsibilities:** Workspace owners and administrators manage team memberships, issue and revoke sync API keys, and control workspace settings. Actions taken by invited members are attributed to the workspace.
- **Staff support access:** Server administrators and operators do not have ambient access to your workspace data through the service. If you request technical support, you must explicitly invite our support account as a member or admin to your workspace. Adding our support account to your workspace, and any change to its role, is recorded in the workspace audit log.

## 4. Plans and Free Limits

The service is currently provided free of charge under resource limits defined in our system configuration:

- **Workspaces:** 1 free personal workspace per user account.
- **Storage:** 5 GB total storage (committed run files and stored objects).
- **Daily uploads:** 2 GB of uploaded data per UTC calendar day.
- **Request rate:** 600 requests per minute per workspace.
- **People:** Up to 5 people per workspace (including active members and pending invitations).

### Enforcement of limits

When a workspace reaches its storage, daily upload, or people limit, further upload and member invitation requests return HTTP `403 Forbidden`. When a workspace exceeds the request rate, requests return HTTP `429 Too Many Requests` with a `Retry-After` header. Existing data remains accessible for viewing and downloading. We reserve the right to modify limits or suspend workspaces that circumvent these limits.

## 5. Your Data and Ownership

- **Ownership:** You retain full ownership of all data, code snapshots, Git diffs, configurations, metrics, logs, and artifacts uploaded to your workspace ("Customer Data").
- **Limited license:** You grant us and our hosting subprocessors (Amazon Web Services and Google Firebase) a limited, non-exclusive license to host, store, copy, and display Customer Data solely as necessary to provide, secure, and maintain the service.
- **No model training:** We do not use your Customer Data, source code, models, or experiment metrics to train artificial intelligence or machine learning models. [[OWNER: confirm this commitment before publishing]]

## 6. Acceptable Use

You agree to comply with our [Acceptable Use Policy](/agenticml-lite/legal/acceptable-use/). You must not:

- Upload malware, corrupted Git bundles, or hostile payloads.
- Probe, scan, or attempt to bypass workspace isolation boundaries or security controls.
- Circumvent usage limits or create automated mass accounts.
- Store protected health information (HIPAA), payment card data (PCI-DSS), government identifiers, or regulated classified materials. The service holds no formal compliance certifications.

## 7. Account and Workspace Deletion

- **Deletion upon request:** You may request complete deletion of your account and workspaces at any time by sending a request to [[OWNER: contact address]].
- **Data removal:** Deleting a workspace deletes its database records, experiment run files on Amazon EFS, and objects in Amazon S3. If removing S3 objects fails, the failure is logged. [[OWNER: confirm how failed storage cleanup is handled]] Copies in backups and in old S3 versions age out within 35 days. A record of the deletion is written to the server audit log.
- **Backups:** Copies residing in automated backup archives age out according to our retention schedule (up to 35 days).

## 8. Suspension and Termination

We may suspend or terminate your access to the service immediately, without prior notice, if we determine that you have violated these Terms or the Acceptable Use Policy, if your usage threatens system stability or security, or if required by law. When a workspace is suspended, the dashboard answers HTTP `404 Not Found` and sync clients are refused (HTTP `403`).

## 9. Disclaimers and Limitation of Liability

**"As is" disclaimer:** The service is provided on an "as is" and "as available" basis without warranties of any kind, whether express, implied, or statutory, including warranties of merchantability, fitness for a particular purpose, title, and non-infringement. We do not warrant that the service will be uninterrupted, error-free, secure, or free of data loss.

**Limitation of liability:** To the maximum extent permitted by applicable law, in no event will [[OWNER: operator name or legal entity]], its contributors, or its service providers be liable for any indirect, incidental, special, consequential, or punitive damages, or for loss of profits, data, code, or goodwill, arising out of or related to your use of or inability to use the service. Because the service is provided as a free preview, our total aggregate liability for all claims arising out of these Terms or the service will not exceed zero United States dollars ($0.00). [[OWNER/lawyer: confirm liability cap]]

## 10. Governing Law

These Terms are governed by and construed in accordance with the laws of [[OWNER: governing law jurisdiction]], without regard to its conflict of law principles.

## 11. Changes to These Terms

We may update these Terms from time to time. When we do, we will update the "Last updated" date at the top of this page. If you signed up before the publication of these Terms, or if you continue to use the service after an update, your continued use constitutes acceptance of the revised Terms.

## 12. Support and Contact

- **Support:** Technical support during the public preview is provided on a best-effort basis via GitHub Issues at [agenticML/agenticml-lite/issues](https://github.com/agenticML/agenticml-lite/issues). For the hosted service, put "hosted" in the title.
- **Legal and deletion requests:** Contact [[OWNER: contact address]].
