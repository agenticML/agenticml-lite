# Package 18b †: Firebase sign-in for the hosted service

Nov 8, 2026 · Supervisor plan · Release 3.0 · Coder: Cursor (18b.1, 18b.2) ·
Size: M

Source: [on-prem-3.0-plan.md](../on-prem-3.0-plan.md), "Firebase sign-in
(package 18b, hosted)", decisions D7 and D9, and the package summary. Builds
on [18](18-workspaces.md) (accounts, memberships, invites, sessions).
Package 18c (sign-up, limits) builds on the account linking here.

† Security-sensitive: second read-only review by the coder that did not
write it (Codex). Firebase decides **who** someone is; the membership check
from 18 still decides **which workspaces** they can open.

## Goal

With `--firebase-project ID` set, people sign in to the dashboard with
Firebase (Google, or email and password handled by Firebase). The browser
signs in with the Firebase web SDK, posts the ID token once to
`POST /auth/firebase`, and the server verifies it and issues its own session
cookie. From then on sessions, CSRF and the throttle work exactly as in
2.2/18. Self-hosted servers without the flag keep 2.2 sign-in unchanged.

Out of scope: open sign-up and personal workspaces (18c), usage limits
(18c), Postgres (19), a real sign-in against Google in CI (owner check).

## Decisions made for this plan

- **Firebase web SDK is vendored** (owner approved Nov 8, 2026):
  `firebase@13.1.0` from registry.npmjs.org, only
  `firebase-app-compat.js` and `firebase-auth-compat.js`, committed under
  `src/agenticml/server/static/vendor/firebase/` with a `VERSION` note
  (version, source URL, npm integrity hash, licence Apache-2.0). The
  supervisor downloaded and checked the tarball and placed the files; the
  coder does not download anything or edit them. The dashboard keeps
  `script-src 'self'`; the sign-in pages add one Google host (see CSP).
- **The browser needs the web API key.** The Firebase web config needs a
  public `apiKey` and `authDomain` besides the project id, so 18b adds
  `--firebase-api-key` (`AGENTICML_SERVER_FIREBASE_API_KEY`, required with
  `--firebase-project`) and `--firebase-auth-domain`
  (`AGENTICML_SERVER_FIREBASE_AUTH_DOMAIN`, default
  `<project>.firebaseapp.com`). The API key is public by Firebase's design
  but is still only sent on the sign-in and invite pages.
- **Linking accounts.** An account is linked to Firebase by uid
  (`accounts.firebase_uid`, unique). The first sign-in of an unknown uid
  links to an existing account only when all hold: the token's email is
  verified, it equals the account's `login` (case-folded), and the account
  has **no** credentials (no password record, no proxy identity, no uid).
  Such accounts are created by a server admin (`server accounts add EMAIL`)
  or by an invite. An account with a password or proxy identity is never
  linked automatically. Otherwise an unknown uid gets no account in 18b
  (18c adds sign-up) and sees "no access".
- **Freshness.** `auth_time` must be at most 10 minutes old and the page
  uses Firebase in-memory persistence and signs out of Firebase after the
  post, so an old ID token cannot be replayed into a new session.

## Configuration

| Flag | Environment | Meaning |
| --- | --- | --- |
| `--firebase-project ID` | `AGENTICML_SERVER_FIREBASE_PROJECT` | Turns Firebase sign-in on |
| `--firebase-api-key KEY` | `AGENTICML_SERVER_FIREBASE_API_KEY` | Web API key (required with the project) |
| `--firebase-auth-domain HOST` | `AGENTICML_SERVER_FIREBASE_AUTH_DOMAIN` | Default `<project>.firebaseapp.com` |

With the project set:

- `POST /login` with a password or a key answers 404 `{"error": "not
  found"}`; `GET /login` serves the Firebase sign-in page.
- `/setup` is off (404). Break-glass access is the server-admin CLI.
- Trusted identity proxy (`--identity-header`) cannot be combined with
  Firebase: refuse to start with a clear message.
- Workspace invites are accepted only through Firebase sign-in (below).
- Sync keys and `/v2` are unchanged; keys are still created by a signed-in
  session (as 18).

Validation at start: project id `[a-z0-9-]{4,40}`, API key printable ASCII
≤ 100 chars, auth domain a hostname. Missing `[firebase]` extra (`jwt` not
importable) refuses to start: `Firebase sign-in needs the [firebase] extra:
pip install 'agenticml-lite[firebase]'`.

## Token verification (`server/firebase.py`, new)

`verify_id_token(token, project, *, now=None, keys=None) -> Claims` using
PyJWT (`jwt` with `cryptography`):

1. Header: `alg == "RS256"`, `kid` present; any other alg (including
   `none`, `HS256`) is refused before key lookup.
2. Keys: Google's x509 certificates from
   `https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com`,
   cached until the response's `Cache-Control: max-age` (cap 24 h,
   minimum 5 min), fetched with `requests` (timeout 5 s, no redirects to
   other hosts). An unknown `kid` triggers at most one refetch per minute.
   A fetch failure answers 503 and logs one warning; it never accepts a
   token. The fetcher is injectable (`CertificateSource`) so tests never
   call Google.
3. Claims: `iss == https://securetoken.google.com/<project>`,
   `aud == <project>`, `exp > now`, `iat <= now + 60`,
   `auth_time <= now + 60` and `now - auth_time <= 600`, `sub` a non-empty
   string ≤ 128 chars. Leeway 60 s on clock checks only.
4. Returns `uid` (`sub`), `email` (lowercased, may be None),
   `email_verified` (bool, default False), `name` (display name, may be
   None), `provider` (`firebase.sign_in_provider`).

Every failure raises one `FirebaseTokenInvalid` with an internal reason for
the log; the client sees only `{"error": "sign-in failed"}` (401).

## Storage

Migration `0004_firebase.sql`: add to `accounts`:
`firebase_uid TEXT UNIQUE`, `email TEXT`, `email_verified INTEGER NOT NULL
DEFAULT 0 CHECK (email_verified IN (0,1))`. Use only SQL that Postgres also
accepts (package 19 runs every migration on both).

`AccountRepo` (in `db/workspaces.py`) gains:
`by_firebase_uid(uid, tx)`, `link_firebase(account_id, uid, email,
verified, tx)` (refuses if either side is already linked), `create(login,
display_name, created_by, tx)` for credential-less accounts, and
`claimable(login, tx)` implementing the linking rule above. All under
`tx.advisory_lock('users')` as the other account writes.

## Routes and flow (`webauth.py`)

- `POST /auth/firebase` (JSON body ≤ `LOGIN_BODY_MAX`): `{"id_token":
  "...", "invite": "<token>"?}`. Same CSRF origin check, client address and
  login throttle as `POST /login` (bucket by address and by `uid:` after
  verification fails/succeeds; failures count as failed logins).
  1. Verify the token. Failure → 401 `sign-in failed`, audit
     `login.failed` with `method: firebase`.
  2. Find the account by uid. If none, apply the linking rule (link and
     store email/verified), or, with an `invite`, accept the invite (below).
     If still none → 403 `{"error": "no access", "reason": "no_account"}`;
     the page shows "Your account has no access to this server. Ask a
     workspace admin for an invite." No account row is created.
  3. Refresh `email` and `email_verified` on the account from the token.
  4. Create the session (via `firebase`) exactly as password login does,
     with the account's generation, attach, audit `login.ok` with
     `method: firebase`, answer `{"ok": true, "user": login}` plus the
     cookie.
- `GET /auth/firebase/config` → `{"projectId", "apiKey", "authDomain"}`,
  only when Firebase is on (404 otherwise); `Cache-Control: no-store`.
- Invites with Firebase: `GET /invite?token=` serves the Firebase sign-in
  page in invite mode (same page, `data-invite` read from the URL by JS,
  never echoed into HTML). Acceptance in `POST /auth/firebase` requires
  `email_verified` and token email == invite `login` (case-folded); then
  the uid's account (existing, or claimable by the linking rule, or created
  credential-less with `login = email`) gets the membership in one
  transaction with the invite redemption. Mismatch or unverified → 403
  `{"error": "this invite is for a different or unverified email"}`, the
  invite stays pending. An invite login that is a 2.2 user name (not an
  email) cannot be accepted with Firebase (403, same message).
- `CSP`: dashboard CSP stays `script-src 'self'`. On the Firebase sign-in
  and invite pages only: `script-src 'self' https://apis.google.com`
  (the vendored auth bundle injects `https://apis.google.com/js/api.js`
  for the Google popup/redirect helper; found when vendoring, a deviation
  from the 3.0 plan's "only connect-src and frame-src"), `connect-src
  'self' https://identitytoolkit.googleapis.com
  https://securetoken.googleapis.com https://www.googleapis.com
  https://apis.google.com`, and `frame-src https://<authDomain>
  https://apis.google.com`. These pages show no workspace data. Every other
  page keeps the existing CSP unchanged. reCAPTCHA hosts are not allowed
  (no phone sign-in).
- Session payload (`/api/session`) gains `"auth": "firebase"` when on, so
  the dashboard hides "change password" (the account page links to nothing
  for passwords under Firebase) and the sign-out also calls Firebase
  `signOut` on the sign-in page.

## Sign-in page (18b.2)

`static/firebase-login.html` + `static/firebase-login.js` (module), served
at `GET /login` and `GET /invite` when Firebase is on; vendored SDK files
served from `/static/vendor/firebase/*.js` through `_ASSETS`
(`text/javascript`). The page:

- loads config from `/auth/firebase/config`, `firebase.initializeApp`,
  `setPersistence(NONE)` (in-memory);
- offers "Continue with Google" (`signInWithPopup`, falling back to
  `signInWithRedirect` when the popup is blocked) and email + password
  (`signInWithEmailAndPassword`, "Create account" with
  `createUserWithEmailAndPassword` + `sendEmailVerification`, "Forgot
  password" with `sendPasswordResetEmail`);
- after sign-in gets `getIdToken(true)`, posts it (with the invite token in
  invite mode), signs out of Firebase, then goes to `/` (or shows the
  server's error text: no access, unverified email, wrong invite email);
- uses the existing `app.css` login card styles, no inline script, no
  inline event handlers; no horizontal overflow at 375px.

## CLI

- `agenticml server serve` and `server up` accept the three flags (up only
  passes them through).
- `agenticml server accounts add LOGIN [--display-name NAME]
  [--server-admin]`: creates a credential-less account (for a hosted
  operator to pre-create themselves or a workspace owner before
  `workspaces create --owner`). Audited (`account.create`, server-level).
- `server accounts list` shows `firebase` (linked yes/no) and
  `email_verified`.

## Packaging

`pyproject.toml` optional dependencies (D7), added here once for 18b, 19
and 21 so later packages do not edit the same lines:
`firebase = ["PyJWT[crypto]>=2.8"]`, `postgres = ["psycopg[binary]>=3.1"]`,
`s3 = ["boto3>=1.34"]`, `hosted = ["agenticml-lite[firebase,postgres,s3]"]`.
`dev` gains `PyJWT[crypto]>=2.8` and `psycopg[binary]>=3.1` so the suite can
run every test. The base install stays `requests` only; `firebase.py`
imports `jwt` lazily. Add the vendored files to package data.

## Constraints and conventions

- No new runtime dependency in the base install; no network call in tests.
- Never log ID tokens, the API key, or emails at info level (debug only,
  and never tokens).
- Every "not yours" answer stays 404 as in 18; the new 403s are only about
  the signed-in person's own account (no access, wrong invite email).
- Match the existing webauth style: helpers on `WebAuth`, `_headers`,
  `_audit`, throttle tickets released in `finally`.
- Sessions created by Firebase sign-in behave like password sessions:
  generation check, idle and max age, CSRF, logout.

## Edge cases

- Token for another project (aud/iss), expired, future `iat`, old
  `auth_time`, wrong signature, unknown `kid`, `alg` none/HS256, missing
  `sub`, oversize body, non-JSON body: all 401 `sign-in failed` (body
  errors 400 as `/login`), no session.
- Certificate endpoint down or returns garbage: 503, never accept.
- Unverified email with a linked account: sign-in works (they were already
  admitted), but cannot accept invites. Unverified email for an unknown uid:
  no linking, no account.
- Linked uid whose account is now a member of no workspace: session works,
  `/` shows the existing "no workspaces" picker state.
- Email changed in Firebase: the uid stays linked; `email` column updates;
  `login` does not change.
- Two concurrent first sign-ins for the same uid: the unique `firebase_uid`
  plus the `users` advisory lock give one link; the loser re-reads.
- `--firebase-project` with `--identity-header`: refuse to start.
- Server admin flag unchanged by Firebase; `server accounts
  set-server-admin` still the only way.

## Tasks

- **18b.1 (server):** `firebase.py`, migration 0004, `AccountRepo`
  additions, `POST /auth/firebase`, config route, invite acceptance,
  login/setup/password-off rules, CLI flags and `accounts add`, pyproject
  extras, tests below except UI.
- **18b.2 (page):** vendored SDK wiring (files supplied by the supervisor),
  `firebase-login.html/js`, CSP per page, session payload `auth`, dashboard
  hides password change, UI tests.

## Tests and checks

- `tests/test_server_firebase.py` (new): generate an RSA key and a
  self-signed cert with `cryptography`, inject it as the certificate
  source, sign tokens with PyJWT. Cases: valid; wrong signature; other
  project (iss, aud separately); expired; `iat` in the future; `auth_time`
  older than 10 min; `alg` none and HS256; unknown kid (one refetch, then
  refused); missing/oversize `sub`; certificate fetch failure → 503.
- Flow: linked uid signs in and gets a working session (CSRF-protected
  write works); credential-less account claimed by verified email; account
  with a password is **not** claimed; unverified email not claimed; unknown
  uid → 403 no access and no account row; invite accepted by matching
  verified email (membership created, invite gone); mismatching or
  unverified email → 403 and invite still pending; password and key
  `POST /login` → 404 with Firebase on; `/setup` 404; throttle counts
  Firebase failures; audit rows written; `--identity-header` with Firebase
  refused.
- Isolation suite still passes (new routes are account-level, add them to
  the route table's isolation expectations).
- UI (`testbed`, `-m ui`): Firebase sign-in page renders with a stub
  Firebase (serve a test double for the vendored files via a test-only
  route override or Playwright request interception), error texts shown,
  no horizontal overflow at 375px.
- Full suite and UI suite pass.

## Done when

Token verification is tested with locally signed tokens (wrong signature,
issuer, audience, expired, unverified email, unknown uid) without calling
Google; invite acceptance by verified email works; the UI suite covers the
sign-in page; the full and UI suites pass. One real sign-in against the
Firebase project is an owner check, tracked in the Backlog until done.
