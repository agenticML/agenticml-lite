---
title: Sign-in and sync keys
description: How people sign in to the hosted dashboard and how machines authenticate with sync keys.
sidebar:
  order: 3
  badge:
    text: '3.0'
    variant: tip
---

This page explains how people sign in to the hosted dashboard at `https://app.agenticml.xyz` and how training machines authenticate using sync keys.

## Two ways in

Access to the hosted service is split between web browsers and training machines:

| Access | Method | Purpose |
| --- | --- | --- |
| People | Web sign-in (Google or email and password) | Open the dashboard and manage workspaces |
| Machines | Sync key (`agenticml` CLI) | Upload runs and check authentication |

A web sign-in never gives a training machine access to sync runs. A sync key never signs anyone in to the web dashboard. The hosted sign-in page accepts only Google accounts or verified email and password accounts. It does not provide an API key sign-in form.

## What a sync key is

A sync key is an authentication token bound to exactly one workspace and one user account.

The server resolves the workspace directly from the key upon connection. You do not configure a workspace slug on the client.

A sync key acts with its user's current workspace role on every request:

- A key belonging to a member, admin, or owner can sync runs and check identity.
- A key belonging to a viewer can only call `agenticml whoami`. It cannot sync runs.
- If a user's role is changed to `viewer`, or if the user is removed from the workspace, their keys stop syncing immediately. In the dashboard key table, an unusable key appears with an **inactive** badge indicating the reason (such as "role viewer cannot sync" or "user is missing").

Sync keys have two scopes:

- `sync`: The default scope. Intended for training machines.
- `admin`: Can only be created by workspace owners and admins. On the hosted service, an admin key cannot sign in to the dashboard, because sign-in is Google or email and password only. It syncs the same way as a sync key, so use `sync` keys for machines.

Keys do not expire. A sync key remains valid until it is revoked or until the user account loses sync permissions.

Key names must use letters, digits, `.`, `_`, or `-`, up to 64 characters. The names `env`, `.`, and `..` are reserved.

## Make a key

Workspace owners, admins, and members can create sync keys. Viewers cannot create keys, and the **API keys** section is hidden from them.

1. Open **Settings** in the top bar, then click **API keys**.
2. Click **New key**.
3. Enter a name for the machine (such as `gpu-box-1`). Key names allow letters, digits, `.`, `_`, and `-`, up to 64 characters.
4. Leave **Scope** as `sync`.
5. Click **Create**.

Workspace owners and admins also see a **User** field to create a key for another person in the workspace. If the selected user has the viewer role, the key is created as **inactive**.

The key secret is shown once upon creation and cannot be recovered. Copy it immediately. For a walkthrough, see [Hosted quickstart](/agenticml-lite/hosted/quickstart/#3-make-a-sync-key).

## Set up a training machine

To authenticate a machine interactively, run:

```bash
agenticml login --host https://app.agenticml.xyz
```

Paste your API key at the `API key:` prompt. The input is hidden.

`login` verifies the key with the server and writes it to `~/.config/agenticml/credentials.json` with mode 0600 (readable and writable only by you). Set `AGENTICML_CONFIG_DIR` to use another directory. If it is unset and `XDG_CONFIG_HOME` is set, the file is `$XDG_CONFIG_HOME/agenticml/credentials.json`.

Verify the connection:

```bash
agenticml whoami
```

The output shows the server host, key prefix, key name, scope, source, user, role, and workspace:

```text
https://app.agenticml.xyz  key=<prefix>…  name=gpu-box-1  scope=sync  source=file  valid
user: jane.doe@example.com (owner)
workspace: jane-doe
```

The client resolves keys and hosts in this order:

- **Key:** a key passed on the command line (`--api-key` on `sync`; not recommended), then `AGENTICML_API_KEY`, then the saved key in `credentials.json`.
- **Host:** `--host` command flag, then environment variable `AGENTICML_HOST`, then the saved default host in `credentials.json`.

If both `AGENTICML_API_KEY` and a saved file key are present, the environment variable takes precedence, and `agenticml whoami` reports `source=env`.

If `AGENTICML_API_KEY` is set without `AGENTICML_HOST` and a default host is saved in `credentials.json`, the client issues a warning that it is sending the API key to the saved default host.

To remove saved credentials for a host, run:

```bash
agenticml logout
```

`logout` defaults to the saved default host. Pass `--host URL` to remove credentials for another host.

## Clusters, containers and CI

On compute clusters with a shared home directory, saving a key with `agenticml login` makes it available to every node mounting that directory. If you want nodes to use separate keys, set `AGENTICML_CONFIG_DIR` to a node-local path on each node.

In containerized training and CI jobs, inject the host and key from your secret store rather than saving credentials to disk:

```bash
export AGENTICML_HOST=https://app.agenticml.xyz
export AGENTICML_API_KEY="$HOSTED_KEY"
```

Never bake an API key into a container image or put the raw key value on a command line.

In multi-GPU jobs, training scripts track metrics on rank 0 only by default (see [Distributed training](/agenticml-lite/guides/distributed/)). One key on the machine that runs rank 0 is enough. Run `agenticml sync --follow` alongside training (see [quickstart step 6](/agenticml-lite/hosted/quickstart/#6-track-and-sync-a-run)).

## One key per machine

Create a separate sync key for each training machine, cluster node, or CI pipeline.

The key table under **Settings > API keys** displays each key's prefix, created date, creator, last used timestamp, and run count.

The table shows how many runs each machine's key uploaded and when it was last used. It also lets you revoke one machine without stopping the others.

## Rotate or revoke a key

To revoke a key:

1. Open **Settings > API keys**.
2. Find the key in the table and click **Revoke**.
3. Confirm in the dialog: "Machines using it stop syncing on their next request, and its dashboard sessions end."

To replace a key:

1. Create a new key under **Settings > API keys**.
2. On the machine, run `agenticml login --host https://app.agenticml.xyz`, or update `AGENTICML_API_KEY`.
3. Check the new key with `agenticml whoami`.
4. Restart any running `agenticml sync --follow`, so it picks up the new key. It reads the key once at start.
5. Revoke the old key in the dashboard.

Removing a member from a workspace under **Settings > Members** automatically revokes all keys belonging to that user in the workspace.

When a key is revoked, subsequent sync attempts print:

```text
API key rejected — run agenticml login
```

A one-shot `agenticml sync` exits with status 1. A long-running `agenticml sync --follow` process exits with status 3.

## Several workspaces

`credentials.json` stores one default host and one key per host URL. Because all hosted workspaces share `https://app.agenticml.xyz`, running `agenticml login` for a second workspace overwrites the saved key.

To sync to several workspaces from one machine, provide `AGENTICML_API_KEY` per command or per shell environment. For details on per-workspace progress tracking and cursor resets, see [Several workspaces from one machine](/agenticml-lite/hosted/workspaces/#several-workspaces-from-one-machine).

## Keep keys safe

- **Shown once:** The full key secret is displayed only once when created. The server stores only a SHA-256 hash and the 8-character prefix.
- **HTTPS required:** The client enforces HTTPS. It refuses plain HTTP connections to non-local hosts.
- **No keys on command lines:** Never pass a key directly in command line arguments, where it is exposed in shell history and system process listings. To log in non-interactively, pipe the key into stdin (`echo "$HOSTED_KEY" | agenticml login --host https://app.agenticml.xyz`).
- **Secrets hygiene:** Never commit keys to source control or share notebooks containing keys. If a key is exposed, revoke it immediately under **Settings > API keys**.

## Browser sessions

Web dashboard sessions expire after 12 hours of inactivity or 7 days of total age.

To log out of the dashboard manually, click **Sign out** in the top navigation bar.

## Troubleshooting

For storage, daily upload, request rate, and member limit errors (HTTP 403 and HTTP 429), see [Hosted quickstart troubleshooting](/agenticml-lite/hosted/quickstart/#troubleshooting).

**"invalid API key" on login.** The key was mistyped, was revoked, or was created for a different server. Create a new key in **Settings > API keys**.

**"API key rejected — run `agenticml login`" during sync.** The key was revoked or the user was removed from the workspace. Create a new key and re-authenticate.

**HTTP 401 "unauthorized".** The request is missing an API key, the key is invalid, or the key was revoked.

**HTTP 403 "forbidden".** The key belongs to a user with the `viewer` role. Viewers cannot upload runs. Ask a workspace admin to change your role to `member`.

**"Verify your email with your sign-in provider, then sign in again."** When signing up with email and password, you must click the verification link sent to your inbox before signing in.

**"Your account has no access to this server. Ask a workspace admin for an invite."** Open sign-up is closed on the server, or the account has not been added to any workspace. Ask a workspace admin for an invite.
