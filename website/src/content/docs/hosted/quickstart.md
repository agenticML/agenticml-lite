---
title: Hosted quickstart
description: Go from no account to a synced run on the hosted service.
sidebar:
  order: 2
  badge:
    text: '3.0'
    variant: tip
---

This page takes you from no account to a run you can see at `https://app.agenticml.xyz`. It needs Python 3.10 or newer on the training machine. The hosted service is a public preview; read the [overview](/agenticml-lite/hosted/) first if you have not.

## 1. Sign up

Open `https://app.agenticml.xyz/login`. The page has two tabs, **Sign in** and **Sign up**.

- **Google:** click **Continue with Google**. Google has already verified your email.
- **Email and password:** open the **Sign up** tab, enter your email and a password, and click **Sign up**. The page tells you to check your inbox to verify your email. Open the link in that email, come back, and sign in. Until the email is verified you cannot get a workspace or accept an invite.

Whether anyone can sign up is stated in [Sign-up](/agenticml-lite/hosted/#sign-up). If it is off, the page says "Your account has no access to this server. Ask a workspace admin for an invite." Ask for an invite, then follow [Workspaces and team](/agenticml-lite/hosted/workspaces/#invite-someone).

## 2. Find your personal workspace

Your first verified sign-in creates a free personal workspace and makes you its owner. It is named after your Google name, or the part of your email before the `@`, followed by "'s workspace". Its address is `https://app.agenticml.xyz/w/<slug>/`. The slug comes from your email before the `@`: `jane.doe@example.com` becomes `jane-doe`, and a number is added if that slug is taken.

## 3. Make a sync key

A training machine uploads with a sync key.

1. In the dashboard, open **Keys**.
2. Click **New key**.
3. Enter a name for the machine, such as `gpu-box-1`. Use letters, digits, `.`, `_` and `-`, up to 64 characters.
4. Leave **Scope** as `sync`.
5. Click **Create**.

The key is shown once and cannot be recovered. Copy it now. The dialog also shows a ready-made login command that has the key in it. Do not use that command: it puts the key in your shell history and in the process list. Use the next steps instead.

## 4. Install the client

PyPI has only 1.0.0, which cannot sync to a server. Install 3.0 from GitHub (it needs `git`):

```bash
pip install 'agenticml-lite[system] @ git+https://github.com/agenticML/agenticml-lite@dev'
```

This follows the `dev` branch, so what you get moves as development continues. The hosted service needs client 2.2 or newer. A 2.2 client is expected to work but has not been checked against the hosted service yet, so use 3.0.

## 5. Log in

```bash
agenticml login --host https://app.agenticml.xyz
```

Paste your key at the `API key:` prompt. It is not shown as you type. `login` checks the key with the server, then saves it in `~/.config/agenticml/credentials.json` (readable only by you) and prints a line like this:

```text
Logged in to https://app.agenticml.xyz as gpu-box-1 (scope sync) with key <prefix>…
```

Check the login at any time:

```bash
agenticml whoami
```

It prints the host, the key name and scope, then your user and role and the workspace the key belongs to:

```text
https://app.agenticml.xyz  key=<prefix>…  name=gpu-box-1  scope=sync  source=file  valid
user: jane.doe@example.com (owner)
workspace: jane-doe
```

To log in without a prompt, pipe the key in, for example `echo "$HOSTED_KEY" | agenticml login --host https://app.agenticml.xyz`. On a machine where you do not want to save a key, set `AGENTICML_HOST` and `AGENTICML_API_KEY` instead; `sync` and `whoami` read them. Do not put the key on the command line.

## 6. Track and sync a run

Track a run as usual. It is written to disk on the training machine first:

```bash
agenticml run train.py
```

Upload your runs:

```bash
agenticml sync --all
```

To upload while a run is still training, leave this running (for example in `tmux`):

```bash
agenticml sync --follow
```

`--follow` checks for new data every 30 seconds and sends a heartbeat every 60 seconds while a run is active. Stop it with Ctrl-C.

## 7. Open the dashboard

Open `https://app.agenticml.xyz/w/<slug>/` with your slug. Your runs are listed there. The charts, run pages and comparisons work as in the [local dashboard](/agenticml-lite/guides/dashboard/).

## Troubleshooting

**HTTP 426, "this server needs agenticml-lite >= 2.2".** The client is too old. The message suggests `pip install -U agenticml-lite`, but that installs 1.0.0 today. Install 3.0 from GitHub as in step 4.

**"invalid API key" on login.** The key was copied wrongly, was revoked, or belongs to another server. Make a new key on the **Keys** page.

**"API key rejected — run agenticml login" during sync.** The key was revoked or removed. A one-shot `agenticml sync` exits with status 1. `agenticml sync --follow` exits with status 3. Make a new key and run `agenticml login --host https://app.agenticml.xyz` again.

**HTTP 403.** Read the message after the status code:

- `storage limit reached: ... of 5.0 GB used; delete runs or ask for more`: the workspace is full. Existing runs stay readable. `sync --follow` tries again every 10 minutes.
- `daily upload limit reached ... resets at 00:00 UTC`: the 2 GB daily upload limit is used up. Uploads work again at 00:00 UTC.
- `people limit reached (5 of 5)`: seen when inviting, not when syncing. See [Workspaces and team](/agenticml-lite/hosted/workspaces/#people-limit).
- `forbidden`: the key belongs to a viewer. Viewers cannot sync. Ask a workspace admin to change your role, then make a new key.

The limits are listed in [Free plan and limits](/agenticml-lite/hosted/#free-plan-and-limits).

**HTTP 429.** The workspace went over 600 requests a minute. Wait for the number of seconds in the `Retry-After` header. The client also waits and tries again.

**"up to date", but the run is not in the dashboard.** Sync keeps its progress per server, not per workspace, so a run that was already synced to one workspace is not sent to another. See [Several workspaces from one machine](/agenticml-lite/hosted/workspaces/#several-workspaces-from-one-machine).

**A run shows as "stale".** A run is marked stale when it is still `running` but nothing has arrived for more than 600 seconds. Run `agenticml sync --follow` during training so heartbeats arrive.
