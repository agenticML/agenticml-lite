---
title: Workspaces and team
description: Roles, invites, the people limit, and using several workspaces from one machine.
sidebar:
  order: 3
  badge:
    text: '3.0'
    variant: tip
---

Every run, sync key and membership on `https://app.agenticml.xyz` belongs to one workspace. A workspace has its own address, `https://app.agenticml.xyz/w/<slug>/`. If you belong to more than one, the workspace switcher is under **Settings > Account**.

The server treats a workspace you do not belong to as if it did not exist: it answers "not found" to a key, a session or an address from another workspace. A live check of this on the hosted service is still on the [open list](/agenticml-lite/hosted/status/).

## Roles

Each person has one role in each workspace.

| Role | What it can do |
| --- | --- |
| `owner` | Everything an admin can do. The creator of the workspace. Cannot be removed. |
| `admin` | Invite and remove people, change roles, and make or revoke sync keys for anyone in the workspace. |
| `member` | Read the dashboard, sync runs, and make or revoke their own sync keys. |
| `viewer` | Read the dashboard. A viewer's key can only call `agenticml whoami`; it cannot sync. |

## Invite someone

Owners and admins use **Settings > Members**.

1. Open **Settings** in the top bar, then **Members**.
2. Enter the person's email in **Login or email**.
3. Pick a role: `viewer`, `member` (the default) or `admin`.
4. Click **Send invite**, copy the link, and send it to them.

The link works once and expires after 72 hours. The person opens it and signs in with Google, or with email and password, using the same email you invited. The email must be verified. Pending invites are listed under **Pending invites** in the same card, where you can revoke them.

If the email already has an account on the hosted service, the person is added to the workspace straight away and no link is made.

To remove someone or change their role, use **Remove** or the role menu in the Members list.

## People limit

A free workspace has up to 5 people. Members and pending invites both count. When the limit is reached, inviting someone else fails with HTTP 403 and `people limit reached (5 of 5)`. Revoke an invite or remove a person to make room. The **Usage** section under **Settings > Workspace** shows how many of the 5 are used.

## Settings page

Open **Settings** in the top bar. What you see depends on your role.

- **Account:** your user, role and sign-in method, the workspace switcher, and sign out. Servers without Firebase sign-in also show a password form.
- **Workspace:** name, slug, plan and **Usage** (storage, daily uploads, requests per minute and people against the free plan limits). Owners and admins only.
- **Members:** people, roles, invites and pending invites. Owners and admins only.
- **API keys:** your sync keys, with a **New key** button. Owners and admins also see the keys of other people and can make one for a person with the **User** field. A key is shown once, when it is made.
- **Activity:** recent audit events. Owners and admins only.
- **Usage banner:** a banner at the top of the dashboard appears when storage or daily uploads pass 80% of the limit, and again when the limit is reached.

Viewers see only Account. Members see Account and API keys. The switcher appears only if you belong to more than one workspace, open sign-up is on, or you are a server admin.

Only the operator can delete a workspace. Deletion requests will go to the contact address in the Terms once they are published. Until then, open a GitHub issue titled "hosted: deletion request" with no slug, email or run data. The operator will follow up in the issue (see [Support](/agenticml-lite/hosted/#support)).

## Several workspaces from one machine

All hosted workspaces share one address, `https://app.agenticml.xyz`. The client keeps one saved key per server address, and it keeps its sync progress per server address too. So two things go wrong if you use two workspaces from one machine with a plain `agenticml login`: the second login replaces the first key, and a run that was synced to one workspace counts as "up to date" for the other.

Until this is fixed in the client, use one data directory and one key per workspace. Set `AGENTICML_DIR` to a different folder for each workspace, and pass that workspace's key in `AGENTICML_API_KEY` from your secret store:

```bash
export AGENTICML_HOST=https://app.agenticml.xyz

# workspace A
AGENTICML_DIR=$HOME/agenticml-team-a AGENTICML_API_KEY="$TEAM_A_KEY" agenticml run train.py
AGENTICML_DIR=$HOME/agenticml-team-a AGENTICML_API_KEY="$TEAM_A_KEY" agenticml sync --all

# workspace B
AGENTICML_DIR=$HOME/agenticml-team-b AGENTICML_API_KEY="$TEAM_B_KEY" agenticml run train.py
AGENTICML_DIR=$HOME/agenticml-team-b AGENTICML_API_KEY="$TEAM_B_KEY" agenticml sync --all
```

To send a run you already synced to one workspace to another, forget its saved progress first, then sync with the other workspace's key:

```bash
agenticml sync RUN_ID --reset
```

`--reset` needs explicit run ids. The next sync asks the server what it already has and uploads the rest.
