# Docs: hosted sign-in and sync keys page

Nov 24, 2026 · Supervisor plan · Maintenance (docs) after 3.3.1 · Size: S ·
Base branch: `main` (after #98).

Status: in review (PR to `main`). Antigravity drafted the pages; Sonnet
wrote the facts test and applied the supervisor's eight fact-check fixes
(the main one: rotating a key must restart a running `sync --follow`, which
reads the key once at start); the supervisor checked every claim against the
code.

## Goal

The hosted service docs explain, in one place, how people sign in and how
training machines (GPU boxes, clusters, containers, CI) authenticate with sync
keys. Today the facts are split between step 3 and step 5 of the hosted
quickstart and the Settings section of "Workspaces and team"; the on-prem
server has a full "Authentication and keys" page, the hosted section has none.
Owner's request (Nov 24, 2026): sync keys are how GPUs talk to the service, so
the docs must make them clear.

Out of scope: code changes to keys or sign-in, the on-prem `server/keys.md`
page (it stays as is), moving the docs site (parked by the owner, Nov 24, 2026).

## Files to touch

- New: `website/src/content/docs/hosted/keys.md` ("Sign-in and sync keys").
- `website/src/content/docs/hosted/index.md`: one short paragraph "How
  machines connect" under the comparison table or Sign-up, and a Next steps
  entry for the new page.
- `website/src/content/docs/hosted/quickstart.md`: link step 3 and step 5 to
  the new page; fix the troubleshooting entry "'up to date', but the run is not
  in the dashboard": it says sync progress is kept per server, not per
  workspace, which is wrong for 3.0+ clients (`src/agenticml/sync.py`
  `_state_key` keys progress by `<host>/w/<slug>`). Say: 3.0+ clients keep
  progress per workspace; older clients per server; link to the workspaces
  section.
- `website/src/content/docs/hosted/workspaces.md`: in the Settings page list,
  link the API keys bullet to the new page. Sidebar order becomes 4.
- `website/src/content/docs/hosted/status.md`: sidebar order becomes 5.
- `tests/test_website_facts.py`: new test that the keys page states facts
  tied to code constants (see Tests).
- `docs/plans/on-prem-roadmap.md`: Maintenance fixes entry.
- This plan file.

## The new page: content outline (every fact from the code)

Front matter like the other hosted pages (title, description, sidebar order 3,
badge '3.0' tip). Plain, short sentences, same voice as the existing hosted
pages; internal links keep the `/agenticml-lite/` base.

1. **Two ways in.** A table: people sign in on the web (Google, or email and
   password through Firebase) and use the dashboard; machines use a sync key
   and the `agenticml` client (`login`, `sync`, `whoami`). A web sign-in never
   gives a machine access, and a sync key never signs anyone in to the
   dashboard (`webauth.py`: key login answers "this is a sync key; sign in with
   an admin key"; check whether the hosted Firebase login page offers key
   sign-in at all, and say only what is true).
2. **What a sync key is.** Bound to one workspace and one user
   (`server/keys.py` `KeyRecord`). The server finds the workspace from the
   key, so there is no workspace slug in the client setup. The key acts with
   its user's current role on every request: a viewer's key can only call
   `whoami`; if the user becomes a viewer or is removed, the key stops syncing
   (the key table shows it as "inactive" with the reason, `app.js`
   `keyInactiveReason`). Scopes: `sync` (default, for machines) and `admin`
   (only owners and admins can make one, `keyspage.py` `can_create`); state
   what an admin key adds on the hosted service, verified in code, or say it
   is not needed there. Keys do not expire; check `keys.py` and say so only if
   true. Name rules: letters, digits, `.`, `_`, `-`, up to 64 characters,
   `env` reserved (`_NAME`, `validate_name`).
3. **Make a key.** Short steps (Settings > API keys > New key), who can (member,
   admin, owner; not viewer), admins making a key for another person with the
   **User** field. Shown once; copy it. Link to quickstart step 3 instead of
   repeating all of it.
4. **Set up a training machine.** `agenticml login --host
   https://app.agenticml.xyz`, paste at the prompt; saved to
   `~/.config/agenticml/credentials.json`, mode 0600 (`auth.py`; also
   `$AGENTICML_CONFIG_DIR` and `$XDG_CONFIG_HOME/agenticml` overrides).
   `agenticml whoami` to check. Lookup order for the key: command flag (not
   recommended), then `AGENTICML_API_KEY`, then the saved file; for the host:
   flag, `AGENTICML_HOST`, saved default host (`resolve_connection`). Mention
   `agenticml logout` to remove a saved key (check its flags in `cli.py`).
5. **Clusters, containers and CI.** Shared home directory: every node that
   mounts it uses the same saved key, so one login covers the cluster; to keep
   keys apart set `AGENTICML_CONFIG_DIR` per machine. Containers and CI: pass
   `AGENTICML_HOST` and `AGENTICML_API_KEY` from the platform's secret store,
   never bake the key into an image or put it on a command line. Multi-GPU
   jobs: only rank 0 tracks by default (link to the distributed guide), so
   one key per job machine is enough; run `agenticml sync --follow` next to
   training (link quickstart step 6).
6. **One key per machine.** Recommend it: the key table shows each key's
   prefix, created, created by, last used and runs (`app.js` key table
   headers), so you can see which machine synced what and revoke one machine
   without stopping the others.
7. **Rotate or revoke a key.** Revoke in Settings > API keys; a machine using it
   stops on its next request and the key's dashboard sessions end (the revoke
   dialog text). Rotate: make a new key, `agenticml login` on the machine, then
   revoke the old one. Removing a person from the workspace revokes their keys
   in it. What `sync` prints when its key is revoked (quickstart troubleshooting
   text; exit status 1 one-shot, 3 for `--follow`).
8. **Several workspaces.** One saved key per host; link to the workspaces
   section rather than repeating it.
9. **Keep keys safe.** Shown once; the server stores only a SHA-256 hash and a
   short prefix; the client refuses plain http to a non-local host
   (`normalize_host`); never paste a key into a GitHub issue, a notebook you
   share, or a command line; revoke at once if one leaks.
10. **Browser sessions.** Web sessions end after 12 hours idle or 7 days
    (`SESSION_IDLE`, `SESSION_MAX_AGE` in `webauth.py`); **Sign out** in the top
    bar.
11. **Troubleshooting** limited to keys and sign-in, short, linking back to
    the quickstart's list for limits (do not duplicate the 403/429 list).

Optional, only if it reads clearly: a small ASCII or Mermaid-free text
diagram is not needed; prefer the table in section 1.

## Approach

1. Supervisor writes this plan and the roadmap entry (done with this file).
2. `antigravity` drafts the new page and the edits to the four existing pages
   from this plan, reading the code paths named above. It does not touch tests
   or code.
3. `sonnet` adds the facts test and fixes anything in the draft the supervisor's
   fact check finds. If `antigravity` hits its limit, `sonnet` writes the
   prose too.
4. Supervisor fact-checks every sentence of the new page against the code
   (each claim above names its source), reads the diff, and runs the checks.
5. `git` agent commits, pushes `claude/hosted-keys-docs`, opens a PR to `main`.

## Constraints and conventions

- Same style as the existing hosted pages: short declarative sentences, no
  marketing words, exact UI labels in bold, commands in code blocks, dates
  absolute.
- No key value ever appears on a command line in an example (guarded by
  `test_no_login_with_key_on_the_command_line`); piping from a variable as in
  the quickstart is fine.
- Internal links use `/agenticml-lite/...` with a trailing slash; the link
  validator must pass.
- No new claim about hosted behavior that was not checked; if the code allows
  something that was never tried on app.agenticml.xyz, say what the server
  does, not that it was tested live.
- Pages must not overflow at 375px (wide tables: keep columns few and short).

## Edge cases to cover in the text

- Viewer makes a key: the UI hides API keys for viewers; an admin can still
  make one for a viewer, and it shows "inactive".
- A key whose user was removed shows "user is missing".
- Env key set and a saved key too: env wins; `whoami` shows `source=env`.
- `AGENTICML_API_KEY` set with no `AGENTICML_HOST` and a saved default host:
  the client warns that it sends the key to the saved host.

## Tests and checks

- New `test_hosted_keys_page_has_the_key_facts` in `tests/test_website_facts.py`:
  the page names `AGENTICML_API_KEY`, `AGENTICML_HOST`, `AGENTICML_CONFIG_DIR`,
  `credentials.json`, `agenticml login --host https://app.agenticml.xyz`,
  `agenticml whoami`; the session numbers match `SESSION_IDLE // 3600` hours
  and `SESSION_MAX_AGE // 86400` days; the 64-character name limit matches
  `_NAME` in `server/keys.py`. Each check fails if the fact is missing.
- `testbed/.venv/bin/python -m pytest -q tests` from the repo root.
- `website`: `npm ci && npm run build` (link validator) and
  `npm run check:overflow`.

## Done when

The hosted section has a "Sign-in and sync keys" page that a new user can
follow to set up a GPU machine, a cluster, or a CI job; every claim on it
matches the code; the quickstart cursor sentence is fixed; the facts test,
the full test suite, the site build, the link check and the 375px overflow
check pass; the PR to `main` is open.
