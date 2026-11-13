---
title: Acceptable Use Policy
description: Rules and acceptable use guidelines for the agenticml-lite hosted preview service.
draft: true
---

Last updated: [[OWNER: date]]

This Acceptable Use Policy ("AUP") defines acceptable and prohibited behavior on the agenticml-lite hosted service (`https://app.agenticml.xyz`), operated by [[OWNER: operator name or legal entity]] ("we", "us", or "our").

This policy applies to all users, accounts, API keys, and machine sync traffic. By using the service, you agree to comply with this policy and our [Terms of Service](/agenticml-lite/legal/terms/).

## 1. Intended Use

The service is provided exclusively for machine learning experiment tracking, logging metrics, recording configurations, tracking code lineage, and storing artifacts using the open-source `agenticml-lite` library.

## 2. Prohibited Activities

You may not use the service to:

### Security violations and attacks
- Probe, scan, or test the vulnerability of the service, its infrastructure, or its networks without explicit written permission.
- Attempt to breach, bypass, or circumvent multi-tenant workspace isolation boundaries, authentication mechanisms, or database access controls.
- Attempt to read, modify, delete, or infer the existence of another customer's workspaces, runs, keys, files, or stored objects.
- Upload malware, viruses, trojans, or corrupted or hostile Git bundles and pack files designed to disrupt Git subprocesses, consume server memory, or execute unauthorized commands.

### Abuse and resource exhaustion
- Circumvent or attempt to bypass plan quotas, rate limits, storage caps, body admission throttles, or concurrency limits.
- Automate mass account or workspace creation (for example, creating multiple accounts to evade the limit of one free workspace per user).
- Use disposable or fraudulent email addresses to register accounts.
- Launch denial-of-service (DoS) attacks, flood requests, or artificially consume shared infrastructure resources.

### Unlawful and infringing content
- Store, transmit, or share material that infringes any copyright, patent, trademark, trade secret, or other intellectual property or proprietary rights.
- Transmit or store unlawful, fraudulent, defamatory, harassing, or harmful content.
- Publicly expose API keys (for example, by committing sync keys into public version control repositories or including keys in unprotected scripts).

## 3. Prohibited Data Classifications

The service is a public preview and has **no formal compliance certifications** (such as SOC 2, ISO 27001, HIPAA, PCI-DSS, or FedRAMP). You must not upload or process:

- **Protected Health Information (PHI):** Data subject to HIPAA or similar health data regulations.
- **Payment card and financial data:** Credit card numbers, debit card details, bank account numbers, or credentials subject to PCI-DSS.
- **Government identifiers:** Social Security numbers, passport numbers, driver's license numbers, or equivalent national identification numbers.
- **Classified or export-controlled information:** Material restricted under export control laws, defense trade regulations, or government security classifications.

## 4. Quotas and Free Tier Limits

Users of the free preview plan must adhere to the configured service limits:

- **Workspaces:** 1 free personal workspace per account.
- **Storage:** 5 GB total storage (committed experiment run files and stored objects).
- **Daily uploads:** 2 GB of uploaded data per UTC calendar day.
- **Request rate:** 600 requests per minute per workspace.
- **People:** 5 members and pending invitations per workspace.

When a workspace reaches its storage, upload, or member limits, requests return HTTP `403 Forbidden`. When a workspace exceeds the request rate, requests return HTTP `429 Too Many Requests` with a `Retry-After` header. Attempting to evade these limits violates this policy.

## 5. Enforcement and Suspension

We reserve the right to investigate potential violations of this policy. If we identify abusive activity, security threats, or terms violations, we may take immediate action, including:

- **Suspending the workspace:** The workspace is immediately disabled and answers HTTP `404 Not Found` on the dashboard, and sync clients are refused (HTTP `403`).
- **Revoking API keys and sessions:** Blocking immediate access from automated sync clients or compromised credentials.
- **Deleting the workspace and account:** Permanently removing customer data and terminating access.
- **Reporting to authorities:** Reporting unlawful behavior to law enforcement when appropriate.

## 6. Reporting Violations

If you discover a security vulnerability, evidence of abuse, or a violation of this policy, please report it immediately:

- **Abuse and security reports:** Email [[OWNER: contact address]].
- **General technical issues:** File an issue on GitHub Issues at [agenticML/agenticml-lite/issues](https://github.com/agenticML/agenticml-lite/issues).

## 7. Changes to This Policy

We may update this Acceptable Use Policy periodically. Continued use of the service following any updates constitutes acceptance of the modified policy.
