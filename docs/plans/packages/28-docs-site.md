# Package 28: Docs site with 2.0, 3.0 and the hosted service

Nov 13, 2026 · Supervisor plan · Release 3.0 · Coder: Sonnet; prose: Antigravity;
git: git agent; owner steps marked **Owner**

## Goal

The GitHub Pages site (<https://agenticml.github.io/agenticml-lite/>) becomes
the front door for agenticml-lite. A visitor can tell what each version gives
them (1.0, 2.0, 2.1, 2.2, 3.0), choose between local tracking, a self-hosted
server and the hosted service, and go from no account to a synced run on
<https://app.agenticml.xyz> using only the site. The site never claims a
release, install path or guarantee that does not exist yet.

## What is true today (checked Nov 13, 2026)

Every sentence on the site is checked against this table. Re-check it before
28.3 and 28.4 start, and again before each publish.

| Thing | Fact | Wording the site may use |
| --- | --- | --- |
| PyPI | Only `1.0.0` (Nov 1, 2026). Only the `v1.0.0` tag exists. `pip install agenticml-lite` gives a local-only client that cannot sync to a 2.x or 3.0 server. | "`pip install agenticml-lite` installs 1.0.0 (local tracking only)" until 28.9 |
| 2.0, 2.1, 2.2 | Merged to `main` (2.2.0 is `bd0d499`, merged as `e0916d7`). Never tagged, never on PyPI. | "Shipped in the 2.2 code on `main`; never published on its own; included in 3.0" |
| 3.0 | On `dev`, `pyproject.toml` version 3.0.0. Not tagged, not on PyPI. The working install is `pip install 'agenticml-lite[system] @ git+https://github.com/agenticML/agenticml-lite@dev'` (`deploy/aws/README.md`). | "Pre-release; install from GitHub" |
| Hosted service | Live at `app.agenticml.xyz` (AWS us-east-2). Firebase sign-in (Google, email/password). **Open sign-up on** since Nov 13: a verified sign-in with no account gets one free personal workspace. AWS roadmap 4.2b–4.4, Phase 5 and Phase 6 are open. One task, single-AZ database. | "Public preview, best effort, no SLA". Never "production", "stable", "highly available", or restore/durability promises |
| Free plan | `deploy/aws/limits.json` (mirrored in `docs/HOSTED.md`): 5 GB storage, 2 GB uploads per UTC day, 600 requests/minute per workspace, 5 people (members plus pending invites), 1 free workspace per account. No paid plan, no price. | The numbers, "as of" a date, with the 403/429 behaviour from `docs/SERVER.md` (storage, daily upload and people limits answer 403; request rate answers 429 with `Retry-After`) |
| Client to hosted | Server requires client 2.2+ (`DEFAULT_MIN_CLIENT`, `src/agenticml/server/clientwindow.py`); older clients get 426. 2.2 → hosted is unit-tested but AWS roadmap step 5.3 has not run. | Recommend the 3.0 client; 2.2 "expected to work, not yet checked against the hosted service" |
| Legal | No terms, acceptable use or privacy policy anywhere. `/login` links to nothing; `/terms` and `/privacy` redirect to sign-in. | Nothing that implies policies exist |
| Live site | Built from `main` on Nov 7. Home page links only local-tracking pages. `installation.md` says "pre-alpha (`0.1.0.dev0`)". Server pages on `main` describe the 2.x server and tell readers to `pip install agenticml-lite`, which gives 1.0.0. | The live site is already wrong; 28.1 fixes that first |
| Domain | `agenticml.xyz` has no DNS record; only `app` exists (Namecheap). Pages `https_enforced` is false. Repo description says "sync to the AgenticML server"; homepage URL empty. | Do not link `agenticml.xyz` |
| Publishing | `.github/workflows/docs.yml` deploys only on pushes to `main` touching `website/**`; the `github-pages` environment allows only `main`. `main` and `dev` share `website/` history from `bd0d499`; `dev` changed only the six `server/*.md` pages. | See "Publishing route" |

## Owner decisions needed

Recommended option first. Until the owner answers, the recommended option
is the working default for 28.1–28.6; changing D1, D5 or D7 later is a text
edit. 28.6 needs D3 answered; 28.7 needs D1, D2, D4 and D6; 28.8 needs 28.7
live.

| # | Decision | Recommended | Alternatives |
| --- | --- | --- | --- |
| D1 | Open sign-up until the policies are live | Turn it off (`HOSTED_OPEN_SIGNUP=false`, redeploy) if the policies are more than a few days away; either way, no "Sign up" call to action on the site before 28.7 is live | Leave it on and don't promote it |
| D2 | Where the policies live | Docs site under `/legal/` (text edits need no hosted deploy; the sign-in page links out with plain `<a>` tags, no CSP change) | Served by the app at `/terms`, `/acceptable-use`, `/privacy` behind an opt-in `AGENTICML_SERVER_LEGAL_DIR` (needs server code, a hosted deploy per edit, and a † second review) |
| D3 | Publishing route before `dev` → `main` | A docs-only PR to `main` that copies `website/` and `docs.yml` from `dev` | Allow `dev` on the `github-pages` environment (a later `main` push silently overwrites it); wait for the full 3.0 merge and PyPI release |
| D4 | Policy facts | Owner supplies: operator name or entity, contact address, governing law, minimum age, deletion by email request, who approves (with a lawyer) | – |
| D5 | Support channel | GitHub Issues with a `hosted` label until a mailbox exists; later `support@agenticml.xyz` (Namecheap forwarding, check it does not touch the `app` record) | A mailbox now |
| D6 | Users who signed up before the policies | Email them the policies when they go live; continued use means acceptance | Click-through on next sign-in (new code) |
| D7 | Install command until PyPI 3.0.0 | Git install `@dev` with a "moves with development" warning, and the owner releases 3.0.0 soon | Owner cuts `v3.0.0rc1` and the docs pin to it |
| D8 | Custom domain | Later (Backlog): `docs.agenticml.xyz` after PyPI 3.0.0 | Now; or the apex `agenticml.xyz` |
| D9 | Versioned docs or changelog tooling | None now: a plain Releases page; revisit after `v2.2.0` and `v3.0.0` exist | `starlight-versions` or a blog/changelog plugin |

### Decisions taken (owner delegated them to the supervisor, Nov 13, 2026)

The owner asked for every buildable part of package 28 in one PR to `dev`,
with the supervisor deciding. Taken:

- **D1:** open sign-up stays as the owner set it (changing it is an AWS
  step). The site says "public preview" and names invites; 28.8's Sign in
  link and banner are built in this PR. The gate is publishing: the
  docs-only PR to `main` (28.6) should wait until the policies are live,
  unless the owner accepts publishing first.
- **D2:** policies live on the docs site under `/legal/`. They are written
  as drafts (`draft: true`, left out of production builds) with
  `[[OWNER: ...]]` placeholders until the owner and a lawyer approve them.
- **D3:** docs-only PR to `main`, after this PR merges (owner merges).
- **D4, D6:** owner; the drafts mark every fact the owner must supply.
- **D5:** GitHub Issues (`https://github.com/agenticML/agenticml-lite/issues`)
  is the support channel for now.
- **D7:** git install from `dev`, with a "moves with development" warning.
- **D8, D9:** as recommended (later; none).
- **28.7 links:** the server option is built and wired into the hosted
  stack with an empty default (GitHub variable `HOSTED_LEGAL_BASE_URL`), so
  nothing shows on `app.agenticml.xyz` until the owner publishes the
  policies and sets the variable.
- **Not in this PR:** 28.6 (owner merges to `main`, GitHub settings) and
  28.9 (needs 3.0.0 on PyPI).
- **Prose:** Antigravity drafts the Releases, hosted and policy pages from
  fact sheets; Sonnet wires them, fixes facts and runs the checks.

## Publishing route (D3)

1. Every site change lands on `dev` through a normal PR, like all other work.
2. To publish, the git agent branches from `origin/main`, runs
   `git checkout origin/dev -- website/ .github/workflows/docs.yml`, and opens
   a PR to `main`. Its diff must touch only those paths (it must not revert
   the `main`-only merge commit `e0916d7`).
3. **Owner** merges it; `docs.yml` deploys.
4. When `dev` is later merged into `main` in full, `website/` is identical on
   both sides and merges cleanly.
5. Rollback: revert the docs PR on `main` (that push redeploys), or run
   `docs.yml` by `workflow_dispatch` on the previous `main` commit.
6. Drift rule: `editLink` points at `dev`, so "Edit page" PRs land on `dev`.
   Until the full merge, every published batch goes through step 2. The
   first docs PR to `main` must carry the 28.2a `docs.yml`, or `main` keeps
   the old workflow with no PR build or link check.
7. A site change (including a status page update) counts as done only when
   it is live: the git agent opens the docs-only PR, the owner merges it, and
   the live page shows the change.

## Sub-parts

| Part | What | Depends on | Who | Size |
| --- | --- | --- | --- | --- |
| 28.0 | This plan, roadmap rows, AWS roadmap Phase 7, Backlog entries | – | Supervisor | S |
| 28.1 | Truth fixes in existing pages and README | 28.0 | Sonnet | S |
| 28.2a | Docs CI: PR builds, link validator, Node pin, job permissions and concurrency | 28.1 | Sonnet | S |
| 28.3 | Releases page (1.0, 2.0, 2.1, 2.2, 3.0) | 28.2a | Antigravity prose; Sonnet wiring | S |
| 28.4 | Hosted service section, including the service status page | 28.3 | Antigravity prose; Sonnet wiring | M |
| 28.5 | Home page and navigation | 28.4 | Sonnet | S |
| 28.2b | 375px overflow check over every built page, in `docs.yml` | 28.5 | Sonnet | S |
| 28.2c | Facts test (`tests/test_website_facts.py`) | 28.4 | Sonnet | S |
| 28.6 | Publish to `main`; repo and Pages settings | 28.1–28.5, D3 | git agent; **Owner** merges and changes settings | S |
| 28.7 | Policies on the site; links on the sign-in pages | D1, D2, D4, D6; 28.6 | Antigravity drafts; **Owner + lawyer** approve; Sonnet links; **Owner** approves the hosted deploy | M |
| 28.8 | Promotion: Sign in action, banner, header link | 28.7 live | Sonnet | S |
| 28.9 | After PyPI 3.0.0: `pip install`, tag URLs, dates, `pyproject.toml` URLs | **Owner** releases 3.0.0 | Sonnet | S |

Parts 28.1–28.5 and 28.2a–c go in one PR to `dev`, on one branch, one coder
at a time in the order of the table (several parts edit
`website/astro.config.mjs`). The branch is pushed as a draft after 28.1 and
updated per part. A part adds a link only to a page that already exists, so
the link validator stays green at every commit: 28.1 links to Releases and
the hosted quickstart only as plain text, and 28.3/28.4 turn those into
links.

### 28.1 Truth fixes

Files and changes (branch from `dev`):

- `website/src/content/docs/getting-started/installation.md`: replace the
  pre-alpha caution with a "Which version you get" aside: PyPI has 1.0.0
  (local only); 3.0 is a pre-release installed from GitHub (exact command
  from the table); 28.3 adds the link to the Releases page. List the server extras
  (`postgres`, `s3`, `firebase`, `hosted`) and say they need 3.0.
- `server/install.md` and `server/sync.md`: the `raw.githubusercontent.com/
  .../v3.0.0/deploy/systemd/...` URLs 404 (no tag). Point them at `main`
  (check first that `deploy/systemd/` is identical on `main` and `dev`); 28.9
  moves them back to the tag.
- `server/install.md`, `server/upgrade.md`: after each `pip install [-U]
  agenticml-lite`, a one-line note that 3.0 is not on PyPI yet, with the git
  install. Do not rewrite the pages.
- `server/install.md` options table and `reference/configuration.md`: add
  `--admin-emails` / `AGENTICML_SERVER_ADMIN_EMAILS`,
  `AGENTICML_SERVER_S3_ENDPOINT_URL`, `AGENTICML_SERVER_S3_KMS_KEY_ID`, each
  checked against `src/agenticml/cli.py` and the server config code.
- `server/keys.md` "First admin": `/setup` returns 404 when Firebase sign-in
  is on.
- `server/sync.md`: keep `aml.example.com` for self-hosted examples; 28.4
  adds one line pointing hosted users to the hosted quickstart.
- `README.md:3`: drop "pre-alpha (`0.1.0.dev0`)" and the dead
  `https://agenticml.xyz` link. `README.md:213`: 3.0 uploads lineage code
  (`src/agenticml/sync.py`), so fix "Not uploaded in 2.0".
- `astro.config.mjs`: `editLink.baseUrl` → `.../edit/dev/website/`.
- Do **not** edit `pyproject.toml` here: a push to `dev` touching it queues a
  hosted deploy (`deploy.yml` paths), and the docs PR to `main` would not
  carry it. The `Homepage` URL change is in 28.9.

### 28.2a Docs CI: PR builds and link check

Files: `.github/workflows/docs.yml`, `website/package.json`,
`website/package-lock.json`, `website/astro.config.mjs`, `website/.nvmrc`.

1. Add `starlight-links-validator` at an exact version whose peer range
   includes Starlight 0.42.6 (verify with `npm view`). Local links such as
   `http://127.0.0.1:6006` must not fail the build.
2. `docs.yml`: add `pull_request` on the same paths; the deploy job runs only
   on `push` and `workflow_dispatch`. Move `pages: write` and
   `id-token: write` to the deploy job. Move the `pages` concurrency group to
   the deploy job only, and give the build job its own group
   (`docs-${{ github.ref }}`, `cancel-in-progress: true`), so a PR build can
   never cancel a queued deploy.
3. Add `"engines": {"node": ">=22"}` and `.nvmrc` (Node 20 fails the build).

### 28.2b Overflow check in CI

Files: `website/package.json`, `website/package-lock.json`, new
`website/scripts/overflow-check.mjs`, `.github/workflows/docs.yml`.

1. Add `playwright` as a dev dependency at `1.62.0` (the version `test.yml`
   pins). This is the site's second new dev dependency.
2. The script serves `dist/` under `/agenticml-lite/`, loads every page in
   `dist/sitemap-*.xml` plus the 404 page at 375×812, in light and dark, and
   fails if `document.documentElement.scrollWidth > 375` on any of them. It
   also fails if it found no pages, so it cannot pass with nothing checked.
3. `docs.yml` build job: `npx playwright install --with-deps chromium`
   (cached like `test.yml`), then `node scripts/overflow-check.mjs` after the
   build.

### 28.2c Facts test

New `tests/test_website_facts.py` (default suite, no browser, no Node):

- The free-plan numbers on the hosted pages match `deploy/aws/limits.json`.
- The minimum client on the hosted and Releases pages matches
  `DEFAULT_MIN_CLIENT`.
- No `raw.githubusercontent.com/.../v3.0.0/` URL while a `PUBLISHED_3_0`
  constant in the test is false (flipped in 28.9).
- No `agenticml login ... --key <value>` example (match
  `login[^\n]*--key`; `agenticml export --key` is a metric filter and is
  fine).

### 28.3 Releases page

New `website/src/content/docs/releases.md`, top-level sidebar entry
"Releases". Sources: the roadmap, the 2.0, 2.2 and 3.0 plans, `README.md`,
`docs/SERVER.md`, `git log origin/main --merges` for dates (coder verifies,
never from memory).

1. "Which version should I use?": table Path | Version | Install (local only:
   1.0.0 from PyPI or 3.0 from GitHub; self-hosted server: 3.0; hosted: 3.0
   client, 2.2 not yet checked). "Check yours": `agenticml --version`,
   `pip show agenticml-lite`.
2. **3.0 (pre-release; hosted service in public preview):** server state in
   SQLite or Postgres; workspaces on one hostname with roles and a switcher;
   Firebase sign-in and self-serve sign-up with free limits (hosted);
   several server processes and S3 objects (hosted); metric summaries for
   sorting and filtering runs; a 2.x data directory converts on first
   start; clients 2.2+. (Lineage code upload is listed once, under 2.1.)
3. **2.2 (teams):** users and roles, per-user keys, password sign-in and
   invites, trusted identity proxy, audit log, team admin page.
4. **2.1 (full fidelity):** lineage code diffs on the server, by-reference
   artifacts, `server status` / `verify`, `server up` with Tailscale.
5. **2.0 (self-hosted server):** `agenticml server serve`, protocol v2 sync,
   `sync --follow`, shared dashboard with key login and the Keys page,
   systemd units. Breaking: the old hosted sync is gone, there is no
   default host (`login` needs `--host` the first time), Python 3.9
   dropped, no Docker image, server is POSIX-only.
6. **1.0.0 (Nov 1, 2026; on PyPI):** the local-first tracker; its sync to the
   old `api.agenticml.xyz` service is retired.

2.0–2.2 are labelled "shipped in the 2.2 code, never published on their own,
included in 3.0" (no release dates invented). Each section links its docs
pages. The 2.2 self-hosted docs stay reachable for people running 2.2 from
git: link the `website/` tree at `bd0d499` on GitHub.

### 28.4 Hosted service section

New group `website/src/content/docs/hosted/`, in the sidebar after Getting
started, each page with `sidebar.badge: {text: '3.0', variant: 'tip'}`.
`server/index.md` links to it.

- `index.md` **Hosted service:** what it is, who should use it vs
  self-hosting; a Local / Self-hosted / Hosted table (where data lives, cost,
  sign-in, teams, setup); the free plan and what happens at each limit;
  "What we store and where" from `docs/HOSTED.md` and `docs/THREAT_MODEL.md`
  (run files, metrics, artifacts, code snapshots and lineage; AWS us-east-2;
  encrypted at rest; backups up to 35 days, not yet restore-tested); support
  (D5); a "Public preview, best effort, no SLA; policies not published yet"
  aside, removed in 28.7.
- `quickstart.md` **Hosted quickstart** (`Steps`): sign up at
  `https://app.agenticml.xyz` (Google or email; verify the email, then sign
  in again); land in your personal workspace; Keys page → new sync key (shown
  once); install the client (D7 aside; 1.0.0 cannot sync to hosted);
  `agenticml login --host https://app.agenticml.xyz` and paste the key at the
  prompt (not on the command line); `agenticml whoami`; `agenticml run
  train.py`; `agenticml sync --all` or `sync --follow`; open the workspace
  dashboard. Troubleshooting: 426 (old client), 401 (key rejected;
  `sync --follow` exits 3, a one-shot `sync` exits 1), 403
  (storage, daily upload, people limit), 429, runs shown as stale. If open
  sign-up is off, say once that an invite is needed.
- `workspaces.md` **Workspaces and team:** roles (owner, admin, member,
  viewer); invites (72-hour link; the email must be verified and match);
  people limit; several workspaces from one machine (one `AGENTICML_DIR` and
  key per workspace, because sync state is kept per host, roadmap Backlog);
  Usage, Keys and Team pages.
- `status.md` **Service status and roadmap:** the public view of
  `docs/plans/aws-deployment-roadmap.md` (its source; the page does not
  link to that file) in plain language, with dates and no AWS identifiers:
  live since Nov 12 (first deploy) and
  Nov 13 (Firebase sign-in, open sign-up); in progress (first customer
  workspaces); not yet done (launch checks: rolling deploy, isolation, 2.2
  client, S3 encryption, alarms; restore drill and storage benchmark). Health
  check `https://app.agenticml.xyz/healthz`. "Updated by hand; last updated
  <date>". Rule: the PR that records an AWS roadmap phase result also updates
  this page, and the update is done when it is live (publishing route, step
  7; AWS roadmap step 7.6).

The supervisor walks the quickstart against the code (CLI flags, page names,
messages). The open-sign-up sentence lives in one place (a shared snippet or
the index page) so D1 flips it once.

### 28.5 Home page and navigation

- `index.mdx`: own `title` (fix "agenticml-lite | agenticml-lite"); tagline
  that covers local, self-hosted and hosted; keep `pip install` but label it
  per the table; a "Three ways to use it" `CardGrid` (local / self-hosted /
  hosted preview) linking Quickstart, Server, Hosted service; LinkCards for
  Releases and Service status. No "Sign up" action yet (28.8).
- Sidebar order: Getting started, Hosted service, Guides, Server, Reference,
  Releases.
- 404 page uses Starlight's default; check it in the overflow run.

### 28.6 Publish

1. Merge the 28.1–28.5 and 28.2a–c PR into `dev` (owner).
2. git agent: docs-only PR to `main` (route above). **Owner** merges.
3. **Owner**, GitHub settings: tick "Enforce HTTPS" for Pages; repo
   description "Local-first experiment tracking with a self-hosted server and
   a hosted service" (or the owner's wording); homepage URL = the docs site.
4. Live checks: `curl -sf` 200 for `/releases/`, `/hosted/`,
   `/hosted/quickstart/`, `/hosted/status/`; `0.1.0.dev0` not on the live
   installation page; every external link on new pages returns 200 (except
   app pages behind sign-in).

### 28.7 Policies and sign-in links (owner-gated)

1. Antigravity drafts `website/src/content/docs/legal/{terms,acceptable-use,
   privacy}.md` from a fact sheet only: data held (email, name, Firebase uid,
   roles, sessions, audit events with IP addresses, all synced run data
   including code); AWS us-east-2; processors AWS and Google Firebase;
   backups up to 35 days, S3 old versions 30 days; session cookies only, no
   analytics; free limits; no SLA; staff access only by customer invite;
   deletion by email request (D4). Placeholders `[[OWNER: ...]]`. No
   certifications, SLA, multi-region or proven-restore claims.
2. **Owner and lawyer** approve the text; nothing ships before that. Publish
   through 28.6's route.
3. Sonnet adds the links. With Firebase on, one page,
   `src/agenticml/server/static/firebase-login.html` (with
   `firebase-login.js`), serves both `/login` and `/invite`; `invite.html`
   is the password-invite page and is not changed.
   - New server option `--legal-base-url` /
     `AGENTICML_SERVER_LEGAL_BASE_URL`, unset by default, so self-hosted
     servers (even with their own Firebase project) never show agenticML's
     policies. The hosted stack sets it (`deploy/aws/` task environment) to
     the docs site's `/legal/` URL.
   - The server adds the three URLs to the Firebase config response
     (`_firebase_config` in `webauth.py`) only when the option is set;
     `firebase-login.js` renders a footer ("By continuing you agree to the
     Terms and Acceptable use policy. Privacy policy.") only when they are
     present. Plain `<a>` links; no CSP change.
   - `.auth-legal` in `app.css`; option in `docs/SERVER.md`.
   - Tests: `tests/test_server_firebase.py` (config has the URLs only when
     set; an https URL is required), and
     `testbed/tests/test_server_firebase_dashboard.py` (footer shown on
     `/login` and `/invite?token=x` when set, absent when unset, no overflow
     at 375px).
   - † This edits the sign-in page and the hosted deploy config: supervisor
     review plus a second review (pending while Codex and Cursor are
     disabled).
4. **Owner** approves the hosted deploy, then does D6 and, if D1 turned
   sign-up off, turns it back on. Remove the "policies not published" aside.

### 28.8 Promotion (after 28.7 is live)

- Home hero: "Sign in to the hosted service" → `https://app.agenticml.xyz`.
- Site-wide banner through Starlight route data middleware
  (`src/routeData.ts`, set `banner` on every page; frontmatter `banner` is
  per page only), at most ~90 characters: "Hosted service in public
  preview. 3.0 installs from GitHub until it is on PyPI."
- A "Sign in" link in the header as a second `social` entry pointing at
  `https://app.agenticml.xyz`.
- Check: the built `/` and `/guides/python-sdk/` pages contain the banner
  text and the Sign in link (a step in the 28.2b script), and the overflow
  check passes.

### 28.9 After the owner releases 3.0.0

- Every git install becomes `pip install agenticml-lite`; the systemd URLs
  move to the `v3.0.0` tag; the facts test constant flips.
- Releases page gets the 3.0.0 date; 2.x stays "included in 3.0".
- `pyproject.toml`: `Homepage` → docs URL (in a release, so PyPI shows it).
- Update the 2.2 → hosted wording after AWS roadmap step 5.3.
- Shorten the banner. Publish through 28.6, or through the full merge.

## Constraints and conventions

- No claim beyond the "What is true today" table; the supervisor checks every
  `pip install` line and every number.
- No new hosting, no analytics, no tracking scripts. Two new dev
  dependencies (the link checker and Playwright for the overflow check).
- Internal links keep the `/agenticml-lite/` base (24 today; recount with
  `grep -rnoE "[(\"']/agenticml-lite/" website/src/content/docs` before a
  domain change).
- Plain, short sentences, matching the existing docs voice.
- Zero horizontal overflow at 375px on every page, light and dark.
- `pyproject.toml` and anything under `src/` or `deploy/aws/` change only in
  28.7 and 28.9, because those paths trigger a hosted deploy.

## Edge cases

- `@dev` moves under users: say so wherever it appears; 28.9 removes it.
- 1.0.0 users following old docs reach the retired `api.agenticml.xyz`; the
  Releases page says so.
- Open sign-up turned off again: hosted pages already mention invites.
- Limits change: pages say "as of" and the facts test fails until updated.
- The Keys page copies `agenticml login --host ... --key <key>`, which puts
  the key in shell history and contradicts the docs (Backlog entry).
- Someone on 2.2 from git reads 3.0 server pages: Releases links the 2.2
  docs at `bd0d499`.

## Tests and checks

- `cd website && npm ci && npm run build` (Node 22) passes, including the
  link validator with 0 errors.
- The overflow check passes in `docs.yml` on the PR (0 pages wider than
  375px, light and dark).
- `testbed/.venv/bin/python -m pytest -q tests` from the repo root
  (includes `test_website_facts.py`); for 28.7 also `.venv/bin/python -m
  pytest -q -m ui` from `testbed/`.
- `grep -rn "0.1.0.dev0" website/src README.md` prints nothing; `grep -rniE
  "production-ready|guaranteed uptime|\bSLA\b" website/src` finds only "no
  SLA".

## Done when

- 28.1–28.5 and 28.2a–c: merged to `dev`; build, link check, overflow
  check and pytest green; roadmap rows updated.
- 28.6: the live site serves Releases and the Hosted pages, has no pre-alpha
  text, Enforce HTTPS is on, and the repo description and homepage are set.
- 28.7: the three policies are live and `/login` and `/invite?token=x` on
  `app.agenticml.xyz` show the footer links (checked in a browser, since
  the footer is rendered by script); the Backlog entry
  "Release 3.0.0: tag, publish, legal pages" is updated.
- 28.8: home page Sign in action and banner live, still 0 overflow at 375px.
- 28.9: `pip install agenticml-lite==3.0.0` works and no git install remains.

## Resume notes (Nov 13, 2026)

Work stopped here at the owner's request; the branch and a draft PR to `dev` hold the state.

- **Built:** 28.0 (plan), 28.1, 28.2a, 28.3, 28.4 and 28.7. 28.1, 28.2a and 28.3 passed their verifier. 28.4's verifier was stopped before it finished, so run it again. 28.7 passed its verifier after two fix rounds.
- **Not started:** 28.5 (home page and navigation), 28.2b (375px check over every page in `docs.yml`), 28.2c (`tests/test_website_facts.py`) and 28.8 (promotion). 28.6 and 28.9 stay owner-gated.
- **Open findings:**
  - `website/src/content/docs/server/sync.md` ~line 94: the curl URL now points at `main`, but the next line still says to use the tag that matches the installed version. Make the two agree.
  - 28.4: D5 names a `hosted` label for GitHub Issues, but the repository has none. The pages say "put 'hosted' in the title". Create the label, or keep that wording.
  - For D1, the open-sign-up sentence also appears in the dated Nov 13 entry on the status page and in `releases.md` (~line 55).
  - 28.7 (low): the terms and privacy drafts promise that a failed S3 cleanup "is retried by the operator". No runbook step does that yet.
- **Still to do before ready:**
  - The supervisor reviews the whole diff (this is also the † second review for 28.7).
  - Run `pytest -q tests`, then `-m ui` from `testbed/`, the Node 22 site build and the 375px check.
  - Update these rows and mark the PR ready.
- **Build note:** the site build needs Node 22 (`website/.nvmrc`); Node 20 fails.
