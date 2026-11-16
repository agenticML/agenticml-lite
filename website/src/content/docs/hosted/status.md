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
- **Nov 16, 2026:** two customer workspaces, each with an admin, a member and a sync key, checked end to end: each person sees only their own workspaces, and a test run synced into each shows only in its own dashboard.
- **Nov 16, 2026:** a new server version rolled out while a 1.5 GB upload was running; every upload finished, and the server's copy matched the local run byte for byte.
- **Nov 16, 2026:** isolation between workspaces was checked from outside. With one workspace's key, another workspace's runs, URLs and sign-in all answered "not found" or asked for sign-in, and showed no data.
- **Nov 16, 2026:** a 2.2 client synced to the service, and the server's copy matched.
- **Nov 16, 2026:** stored objects are encrypted, and every stored object matches the bytes that were uploaded.
- **Nov 16, 2026:** a restore from backup was tried. The database, the files and the objects were restored into a separate test service, which passed a full check and accepted a new sync. The database restore, the slowest part, took about 13 minutes. The live service was not touched, and the test service was removed afterwards.
- **Nov 16, 2026:** storage speed was measured with 1 and 8 clients syncing at once. The results are in the project's server documentation and did not change any recommendation.
- **Nov 16, 2026:** the terms of service, acceptable use policy and privacy policy are published.

## In progress

One launch check is waiting on a person:

- **Alarms:** an alarm was triggered on Nov 16 and the notification was sent. We are waiting for the recipient to confirm the email arrived.

## Not done yet

- **Two tasks at once:** the service has only run one task at a time (apart from the short overlap during a version roll-out). Running two steady tasks has not been tried.

## Problems

Report a problem or a service disruption on [GitHub Issues](https://github.com/agenticML/agenticml-lite/issues). Put "hosted" in the title, and do not post sync keys or private data.
