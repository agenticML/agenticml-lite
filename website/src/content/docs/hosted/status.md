---
title: Service status and roadmap
description: What is live on the hosted service, what is being checked, and what is still open.
sidebar:
  order: 4
  badge:
    text: '3.0'
    variant: tip
---

This page says where the hosted service at `https://app.agenticml.xyz` stands. Updated by hand; last updated Nov 16, 2026. It changes when a check finishes, so it can be behind.

:::note[Public preview]
The hosted service is a public preview. It runs on a best-effort basis, with no SLA. See the [terms of service](/agenticml-lite/legal/terms/), [acceptable use policy](/agenticml-lite/legal/acceptable-use/) and [privacy policy](/agenticml-lite/legal/privacy/).
:::

## Right now

The page does not show live status. To see whether the service is up, open `https://app.agenticml.xyz/healthz`. It answers `{"ok":true}` when the server is running.

The service runs one server task and one database in a single availability zone. There is no spare copy, so an outage is possible.

## What is live

- **Nov 12, 2026:** first deploy. The service answers at `https://app.agenticml.xyz` with a valid certificate, and the health check above works.
- **Nov 13, 2026:** sign-in with Google, or email and password, is on. See [Sign-up](/agenticml-lite/hosted/#sign-up) for the current sign-up state.
- **Nov 14, 2026:** workspace admins manage members and API keys on a Settings page, and server admins can archive and restore workspaces.
- **Nov 15, 2026:** the 3.0 client from `dev` keeps its sync state per workspace, so one local store can sync to several workspaces.
- **Nov 15, 2026:** the terms of service, acceptable use policy and privacy policy are published.
- **Nov 16, 2026:** two customer workspaces, each with an admin, a member and a sync key, checked end to end: each person sees only their own workspaces, and a test run synced into each shows only in its own dashboard.
- **Nov 16, 2026:** a new server version rolled out while a 1.5 GB upload was running; every upload finished, and the server's copy matched the local run byte for byte.

## In progress

The launch checks are being run on the hosted service:

- **Isolation:** try a key, a session and an address from one workspace against another, and confirm each gets "not found".
- **Older client:** confirm a 2.2 client can sync to the hosted service.
- **Object encryption:** confirm stored objects use the configured encryption, and that downloads match the source bytes.
- **Alarms:** trigger one alarm and confirm the notification arrives.

## Not done yet

These checks are planned and have not been run on the hosted service:

- **Restore drill:** restore the database, the file storage and old object versions into a separate test service, and record how long it takes and how much is recovered. Backups exist, but no restore has been tested.
- **Storage speed:** measure the file storage with 1 and 8 clients syncing at once.

## Problems

Report a problem or a service disruption on [GitHub Issues](https://github.com/agenticML/agenticml-lite/issues). Put "hosted" in the title, and do not post sync keys or private data.
