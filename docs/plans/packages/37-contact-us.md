# Package 37: Contact us button (docs site and dashboard)

Status: in review (plan ready Nov 24, 2026; self-hosted default changed the
same day, see D37.2).

## Goal

Give people an easy way to write to us from the docs site, the hosted
dashboard and self-hosted dashboards. The owner first asked for a chat
button ("I wanna add a feedback or contact us on github pages, and also in
hosted service"), then chose a **Contact us** button that drafts an email
instead of a chat window, and then asked that self-hosted servers write to
us too ("even for the self hosted service, i would want the email to be
forwarded to mine") (all Nov 24, 2026).

The button opens a small panel with:

- **Write an email**: a `mailto:` link that opens the visitor's own email
  app with the address, a subject and a short body already filled in.
- **Copy address**: copies the address, for people who use webmail and have
  no email app set up (a `mailto:` link does nothing visible for them).
- On the docs site only: **Ask on GitHub** (Discussions) and **Report a
  bug** (new issue).

Nothing is sent by the page: the visitor's own email app sends the mail
when they press Send, so no agenticml server ever handles it. We need no
backend, no database table, no spam protection and no change to the
privacy policy. The page loads no third-party code, so the dashboard's
Content-Security-Policy (`script-src 'self'`) and the privacy policy's "The
dashboard itself loads no Google or other third-party code" stay true, and
"nothing is sent to us" stays true for self-hosted servers.

Where the mail goes: `contact@agenticml.xyz`, the owner's Zoho Mail
mailbox (chosen Nov 16, 2026 for the policy pages). Forwarding it to a
personal inbox is an owner step in Zoho; no personal address goes in the
code (public repository).

Three parts:

- **37.1** Docs site: a floating Contact us button on every page.
- **37.2** Server and dashboard: a `--contact-email` server option, a
  floating button on dashboard pages in one of two modes (D37.2), and a
  plain Contact us link on the workspace picker and the Firebase sign-in
  page when an operator address is set.
- **37.3** Hosted template and docs: the hosted service sets the address
  explicitly; SERVER.md, the threat model, the deploy README and the docs
  site describe the option and the self-hosted default.

Out of scope:

- A form that our server sends on the visitor's behalf (AWS SES). Only if
  people run into the webmail problem; it would need its own package.
- A `ContactEmail` parameter in the self-deploy template
  (`deploy/aws-self-hosted/agenticml.yaml`) and a `server up` flag.
  The default applies there. The environment variable works only with
  `server serve` (the self-deploy template uses it); `server up` ignores it.
  Recorded under "Later (unscheduled)" in the roadmap.
- The local dashboard (`agenticml ui`) shows no button: it has the GitHub
  link already.

## Decisions

- **D37.1 Address.** `contact@agenticml.xyz`, the address the Terms and
  Privacy pages already publish. It appears as one constant per place it is
  used: `DEFAULT_CONTACT_EMAIL` in `src/agenticml/server/webauth.py`, the
  docs component, and the hosted template default. A facts test keeps all
  three equal to the address in `website/src/content/docs/legal/terms.md`.
- **D37.2 Three server modes** (changed Nov 24, 2026 at the owner's
  request; it was "off by default"). The value of `--contact-email` /
  `AGENTICML_SERVER_CONTACT_EMAIL`:
  - **Unset or empty → project mode (the default).** The dashboard shows a
    **Feedback** button that writes to the agenticml-lite team at
    `DEFAULT_CONTACT_EMAIL`. Because lab users would otherwise expect their
    own admin, the panel says who it reaches: title "Feedback to the
    agenticml-lite team", then the line "This writes to the people who make
    agenticml-lite, not to your server admin. For access or account
    problems, ask your server admin." The draft carries only the server
    version (D37.4), never workspace names or page paths, so nothing about
    a private lab server reaches us unless the user types it. No picker
    link and no sign-in page link in this mode.
  - **An address → operator mode.** The operator chose who answers
    (our hosted service sets `contact@agenticml.xyz`; a lab sets its own).
    The button is **Contact us**, the panel title is "Contact us", and the
    draft carries the page, workspace and version. The picker gets a
    Contact us link and the Firebase sign-in page a "Questions? Contact us"
    link.
  - **`off` (any case) → no button, no links.**
- **D37.3 Native popover.** The panel uses the HTML `popover` attribute
  with a `popovertarget` button (open and close, Escape and click-outside
  without our own code). Only the Copy button needs script. No new
  dependencies anywhere.
- **D37.4 What goes in the draft.** Only things the visitor can already
  see, and they see them in their email app before sending:
  - Docs: subject `agenticml docs: <page title>`, body with blank lines
    for the message, then `---` and `Page: <page URL>`.
  - Dashboard, operator mode: subject `agenticml: <workspace slug or
    "admin">`, body with blank lines, then `---`, `Page: <path + hash>`,
    `Workspace: <slug>` (left out on `/admin`) and `Server: agenticml-lite
    <version>`.
  - Dashboard, project mode: subject `agenticml (self-hosted): feedback`,
    body with blank lines, then `---` and `Server: agenticml-lite
    <version>` only.
  - Never keys, tokens, the signed-in email address (it is the sender
    anyway), cookies or query strings.
- **D37.5 Strict address check.** The server accepts only
  `local@domain.tld` made of `A-Z a-z 0-9 . _ + -` in the local part and
  `A-Z a-z 0-9 . -` in the domain, at most 254 characters, with a dot in
  the domain. Anything else (spaces, `?`, `&`, `%`, `,`, `;`, quotes,
  control characters, two `@`) stops the server at startup, like a bad
  `--legal-base-url`. This keeps the `mailto:` link from carrying extra
  headers (`?cc=`, `&bcc=`). Subject and body are always built with
  `encodeURIComponent` (JS) or `urllib.parse.quote` (Python).

## Constraints and conventions

- Plain, short wording like the rest of the UI: button labels "Contact us"
  or "Feedback" (D37.2); actions "Write an email", "Copy address", "Ask on
  GitHub", "Report a bug". Under the actions, the address in plain
  selectable text. Copy shows "Copied" for two seconds, or "Select the
  address above to copy it" when the clipboard is not available (plain
  `http://` self-hosted servers have no `navigator.clipboard`).
- Zero horizontal page overflow at 375px, light and dark, with the panel
  open and closed (dashboard and docs site).
- The floating button must not hide content: give the scrolling page enough
  bottom padding that the last row, the last button and pagination can be
  scrolled clear of it. Use `env(safe-area-inset-bottom/right)`.
  At 480px and below, the button is a round icon (44 × 44 px target) with
  an `aria-label` equal to its visible label; wider, icon plus label.
- Accessibility: the button is a real `<button>`, the panel has
  `role="dialog"` and an `aria-labelledby` title, focus moves into the
  panel when it opens and back to the button when it closes, and every
  control is reachable by keyboard.
- Hidden in print (`@media print`).
- Follow the existing dashboard style tokens in `app.css` (buttons, radii,
  `--text-*`, `--surface-*`), and Starlight's `--sl-*` variables on the
  docs site. Both themes.
- Dashboard: no inline script or style attributes that the CSP would block;
  everything in `app.js` and `app.css`.
- Server option pattern: copy `--legal-base-url` (flag wins over
  `AGENTICML_SERVER_CONTACT_EMAIL`; a bad value prints `agenticml server:
  --contact-email ...` and exits 1). Unlike `--legal-base-url`, empty means
  the project default, and `off` turns it off.

## 37.1 Docs site button

### Files

- `website/src/components/ContactButton.astro` (new): button, panel, small
  script for Copy, scoped styles.
- `website/src/components/PageFrame.astro` (new): overrides Starlight's
  `PageFrame`, renders the default frame and then `<ContactButton />`.
  `PageFrame` is on every page, including the splash home page.
- `website/astro.config.mjs`: add `PageFrame` to `components`.
- `website/scripts/overflow-check.mjs`: extend (see Tests).

The facts test for the address is part of 37.3 (one coder per file).

### Approach

1. Constants at the top of `ContactButton.astro`: `CONTACT_EMAIL =
   'contact@agenticml.xyz'`, `DISCUSSIONS_URL =
   'https://github.com/agenticML/agenticml-lite/discussions'`,
   `NEW_ISSUE_URL =
   'https://github.com/agenticML/agenticml-lite/issues/new/choose'`.
2. Build the `mailto:` href at build time from the page title and the
   canonical page URL (`Astro.url` against `site` + `base`), encoded as in
   D37.4. One href per page, no runtime script needed for it.
3. Button with an envelope icon (inline SVG, `aria-hidden`) and a
   "Contact us" label; `popovertarget="contact-panel"`.
4. Panel (`<div id="contact-panel" popover role="dialog"
   aria-labelledby=...>`) with the title "Contact us", the four actions
   (GitHub links open in a new tab with `rel="noopener"`), the address text
   and a Close button (`popovertarget` + `popovertargetaction="hide"`).
5. Copy script: `navigator.clipboard?.writeText`; on any failure or when
   missing, show the fallback text and select the address text.
6. Position: fixed, bottom-right, above Starlight's content but below its
   mobile menu (check the menu still covers it when open). Add bottom
   padding to the main content so the pagination links and footer clear
   the button.

## 37.2 Server option and dashboard button

### Files

- `src/agenticml/server/webauth.py`: `DEFAULT_CONTACT_EMAIL`,
  `resolve_contact_email(value)` next to `normalize_legal_base_url`; a
  `contact_email` constructor argument resolved and stored on the WebAuth
  object; `"contact"` and `"version"` in `_session_payload`;
  `"contact_email"` in `_firebase_config` in operator mode only.
- `src/agenticml/server/app.py`: pass `contact_email` through the same
  three places as `legal_base_url`.
- `src/agenticml/cli.py`: `_contact_email_from_args`, the `--contact-email
  ADDRESS|off` argument on `server serve` (help text names the environment
  variable and the default), wire it into `cmd_server_serve`.
- `src/agenticml/server/routes.py`: in operator mode, the workspace picker
  topbar gets a `Contact us` link (`<a class="btn btn-ghost btn-sm"
  href="mailto:...">`, HTML-escaped, subject `agenticml: workspaces`).
- `src/agenticml/server/static/firebase-login.js` (and `.html` if needed):
  when the config has `contact_email`, add "Questions? Contact us" with a
  `mailto:` link under the policy line (shown even when the legal links are
  not set).
- `src/agenticml/ui/static/index.html`: the button and panel, `hidden` by
  default.
- `src/agenticml/ui/static/app.js`: in `sessionReady`, when `data.contact`
  is an object with a non-empty `email` string, set the label, title, note
  (project mode only) and address text, fill the `mailto:` href per D37.4
  (rebuilt on each open so the page path is current: set it in the panel's
  `beforetoggle`/`toggle` event), wire Copy, and unhide.
- `src/agenticml/ui/static/app.css`: styles (see Constraints).

### Approach

1. `resolve_contact_email(value)` returns `None` (off) or a pair
   `(address, is_default)`:
   - `None` or blank after stripping → `(DEFAULT_CONTACT_EMAIL, True)`;
   - `"off"` in any case → `None`;
   - otherwise check D37.5 with one anchored regex plus the length limit
     and return `(stripped, False)`; on failure raise
     `ValueError("--contact-email must be a plain address like
     name@example.com, or off")`. No case change.
   The CLI calls it early so a bad value stops the server before it binds.
   `serve()` and the server constructors take the raw value
   (`contact_email=None` means the default), so existing callers and tests
   get project mode.
2. Session payload: `"contact": {"email": ..., "default": true|false}` or
   `null` when off, and `"version": agenticml.__version__`. Signed-in users
   only, like the rest of the payload.
3. Dashboard markup at the end of `<body>`: `<button id="contact"
   class="contact-fab" popovertarget="contact-panel" hidden>` and
   `<div id="contact-panel" class="contact-panel" popover role="dialog">`.
   No GitHub actions here (the top bar already links GitHub); just Write an
   email, Copy address, the address, the project-mode note, Close.
4. Bottom padding on `main#app` only while the button is visible (a class
   on `body`, for example `has-contact`).
5. Keep the existing dialogs working: the lightbox and other `<dialog>`
   elements must stack above the button (`showModal` puts them in the top
   layer; check that an open popover closes or stays under them).
6. The button now shows by default on every server dashboard, so run the
   whole UI suite: no existing test may break because the button covers
   something it clicks. Fix such cases with the bottom padding, not by
   turning the button off in tests (a test may use `off` only when it
   tests `off`).

## 37.3 Hosted template and docs

### Files

- `deploy/aws/cloudformation/hosted.yaml`: parameter `ContactEmail`
  (`Default: contact@agenticml.xyz`, `MaxLength: 254`, an
  `AllowedPattern` matching D37.5, `off` or empty) and the environment
  entry `AGENTICML_SERVER_CONTACT_EMAIL`. No `deploy.yml` change: the
  default applies when the stack first gets the parameter, so the hosted
  service runs in operator mode. An existing stack keeps its stored value
  (`aws cloudformation deploy` reuses previous values), so changing the
  address later means updating the stack parameter; the README says so.
- `tests/test_deploy_cloudformation.py`: the parameter and environment
  entry (next to the existing `hosted.yaml` tests).
- `tests/test_website_facts.py`: the address in `DEFAULT_CONTACT_EMAIL`,
  `ContactButton.astro`, the `hosted.yaml` default and `legal/terms.md` is
  the same.
- `deploy/aws/README.md`: one line where the template parameters or server
  environment are described.
- `docs/SERVER.md`: a row in the options table after `--legal-base-url`,
  plus a short note where the dashboard is described: the Feedback button
  is on by default, it only opens the user's email app, and how to point
  it at your own address or turn it off.
- `docs/THREAT_MODEL.md`: one line on the button (no data leaves the
  server; a user may choose to email the project).
- Any page that says a self-hosted server sends nothing to us: keep it
  true and, where it fits, add that the Feedback button only drafts an
  email (`grep -rn -i "nothing" docs website/src/content/docs README.md`).
- `website/src/content/docs/server/...`: only if a docs-site page lists the
  server options (none does today; check again).
- `docs/plans/on-prem-roadmap.md`: statuses.

Docs prose may go to Antigravity with the facts above; the supervisor
checks it against the code.

## Edge cases

- `off`: no button, no picker link, no sign-in link, and `contact` is
  `null` in the session payload.
- Unset: Feedback button in project mode; no picker link, no sign-in link.
- Session request fails or answers 404 (local `agenticml ui`): no button.
- Identity-proxy sessions (`via == "proxy"`): button shows like for anyone
  signed in.
- A page title or slug with `&`, `?`, `#`, quotes, non-ASCII or emoji:
  encoded correctly in the subject; the decoded `mailto:` matches.
- Hash routes in the dashboard (`#/runs/...`): the body's page line uses
  the current `location.pathname + location.hash`, no query string.
- Clipboard refused (insecure context, permission denied): fallback text,
  address selected, no error in the console.
- Very narrow screens (320px): the open panel fits inside the viewport
  with a 16px gutter and scrolls inside itself if needed.
- The Starlight mobile menu and search dialog cover the button when open.

## Tests and checks

Unit and server tests (`tests/`):

- `resolve_contact_email`: `None`, `""`, `"  "` → default pair; `"off"`,
  `"OFF"`, `" Off "` → `None`; accepts `contact@agenticml.xyz`,
  `a.b+c@sub.example.org` as operator pairs; rejects `a b@x.org`,
  `a@x.org?cc=b@y.org`, `a@x.org&bcc=b`, `a%40x@y.org`, `a,b@x.org`,
  `"a"@x.org`, `a@@x.org`, `a@x`, `a@x.org\n`, a 255-character address.
- CLI flag, environment and default, modelled on
  `test_cli_legal_base_url_flag_env_and_default`; a bad value exits 1 with
  `--contact-email` in stderr.
- Session payload: `contact` is the default pair when unset, the operator
  pair when set, `null` when `off`; `version` equals
  `agenticml.__version__`.
- Firebase config has `contact_email` only in operator mode.
- Picker page: a `mailto:` link in operator mode, none in project mode or
  `off`.

Dashboard UI tests (`testbed/`, `-m ui`), in a new
`testbed/tests/test_server_contact_dashboard.py`:

- Operator mode, on a workspace page and on `/admin`: the button reads
  "Contact us"; clicking opens the panel; Escape closes it and focus
  returns to the button; the decoded `mailto:` has the address, the subject
  and the workspace, page and version lines; Copy puts the address on the
  clipboard (grant `clipboard-read`/`clipboard-write`).
- Project mode (unset): the button reads "Feedback", the panel shows the
  team title and the admin note, and the decoded `mailto:` body has the
  version line and no page or workspace line.
- `off`: no visible button.
- Local `agenticml ui`: no visible button.
- 375px, light and dark, panel open and closed: zero horizontal overflow;
  the last element of a long runs list can be scrolled above the button.
- The Firebase sign-in page shows the link in operator mode (extend the
  existing Firebase page test file).
- The whole existing UI suite still passes.

Docs site (`npm run build` then `npm run check:overflow` in `website/`):

- Extend `overflow-check.mjs`: every built page has the button; on the
  home page and one guide at 375px, open the panel and check zero
  overflow in both themes, and that the `mailto:` href decodes to the
  address with the page title in the subject.
- `npm run build` passes, including the links validator.

Repo checks:

- `testbed/.venv/bin/python -m pytest -q tests` from the repo root.
- `.venv/bin/python -m pytest -q -m ui` from `testbed/`.

## Done when

- The docs site shows a Contact us button on every page whose panel offers
  email, copy, GitHub Discussions and a new issue, with zero overflow at
  375px.
- A server with no `--contact-email` shows the Feedback button in project
  mode; one with an address shows Contact us with the picker and Firebase
  sign-in links; one with `off` shows none of these.
- The hosted template sets `contact@agenticml.xyz`.
- SERVER.md and the threat model describe the option and the default, the
  roadmap rows are `done`, and all checks above pass.

## Owner steps

- Approve the hosted deploy after the merge, then check the button on
  app.agenticml.xyz.
- Make sure `contact@agenticml.xyz` is read, or forward it in Zoho (Settings
  → Mail forwarding) to the inbox you read. Self-hosted feedback arrives
  with the subject prefix `agenticml (self-hosted):`, which a Zoho filter
  can sort.
