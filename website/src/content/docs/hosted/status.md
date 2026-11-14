---
title: Service status and roadmap
description: What is live on the hosted service, what is being checked, and what is still open.
sidebar:
  order: 4
  badge:
    text: '3.0'
    variant: tip
---

This page says where the hosted service at `https://app.agenticml.xyz` stands. Updated by hand; last updated Nov 13, 2026. It changes when a check finishes, so it can be behind.

:::note[Public preview]
The hosted service is a public preview. It runs on a best-effort basis, with no SLA. The terms of service, acceptable use policy and privacy policy are not published yet.
:::

## Right now

The page does not show live status. To see whether the service is up, open `https://app.agenticml.xyz/healthz`. It answers `{"ok":true}` when the server is running.

The service runs one server task and one database in a single availability zone. There is no spare copy, so an outage is possible.

## What is live

- **Nov 12, 2026:** first deploy. The service answers at `https://app.agenticml.xyz` with a valid certificate, and the health check above works.
- **Nov 13, 2026:** sign-in with Google, or email and password, is on. See [Sign-up](/agenticml-lite/hosted/#sign-up) for the current sign-up state.

## In progress

First customer workspaces, checked end to end:

- Create two workspaces, each with an admin, a member and a sync key.
- Confirm each person sees only their own workspaces, with the right roles.
- Sync one test run from a training machine into each workspace, and confirm it shows in the right dashboard and nowhere else.

## Not done yet

These checks are planned and have not been run on the hosted service:

- **Update while a sync is running:** deploy a new version during an upload and confirm no acknowledged upload is lost.
- **Isolation:** try a key, a session and an address from one workspace against another, and confirm each gets "not found".
- **Older client:** confirm a 2.2 client can sync to the hosted service.
- **Object encryption:** confirm stored objects use the configured encryption, and that downloads match the source bytes.
- **Alarms:** trigger one alarm and confirm the notification arrives.
- **Restore drill:** restore the database, the file storage and old object versions into a separate test service, and record how long it takes and how much is recovered. Backups exist, but no restore has been tested.
- **Storage speed:** measure the file storage with 1 and 8 clients syncing at once.
- **Policies:** the terms of service, acceptable use policy and privacy policy are not published.

## Problems

Report a problem or a service disruption on [GitHub Issues](https://github.com/agenticML/agenticml-lite/issues). Put "hosted" in the title, and do not post sync keys or private data.
