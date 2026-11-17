# Package 27: Sign-in page redesign (agenticml-server look)

Nov 13, 2026 · supervisor plan · coder: Sonnet

## Goal

Make the server's sign-in pages look like agenticml-server's sign-in screen
(`agenticml-server/frontend/src/components/AuthScreen.tsx`), as drawn on the
design board's "Hosted · Sign in" artboard, and show the agenticML logo.
Every behaviour of today's pages stays: Google sign-in, email and password,
account creation with email verification, password reset, resend email,
invites, errors.

## Scope

In:

- `src/agenticml/server/static/firebase-login.html` (hosted sign-in, also
  served for `/invite`): the full new layout, with Sign in / Sign up tabs.
- `src/agenticml/server/static/firebase-login.js`: tabs and the Sign up
  mode, the show-password toggle. All existing logic is kept.
- `src/agenticml/server/static/login.html` (self-hosted sign-in): the same
  two-panel shell and logo around its existing forms; its copy, ids and
  behaviour stay.
- `src/agenticml/ui/static/app.css`: new `.auth-*` classes.
- Tests in `tests/test_server_firebase.py` and
  `testbed/tests/test_server_firebase_dashboard.py`, plus new UI tests.

Out (Backlog entries in the roadmap): `setup.html` and `invite.html` (they
keep `.login-page`/`.login-card`), a light/dark toggle or system theme on
the sign-in pages, and legal links (they wait for the owner's policies).

## Layout (desktop, > 960px)

A two-column grid, `grid-template-columns: minmax(420px, 44%) 1fr`,
`min-height: 100vh`, background `var(--surface)`.

**Left: brand panel** (`<aside class="auth-brand" aria-label="agenticML">`).
It is always dark: the class sets the dark token values locally (copy the
needed values from `:root[data-theme="dark"]`: `--bg`, `--surface`,
`--text`, `--text-2`, `--text-3`, `--hairline`, `--border-strong`,
`--accent`, `--s1`, `--s3`, `--s5`, and `color-scheme: dark`). Background
`var(--bg)`, padding `48px 56px`, flex column, `gap: 40px`,
`overflow: hidden`. From top to bottom:

1. The logo, 24px tall (see Logo).
2. A block (max-width 520px, gap 14px): a row with an eyebrow
   "sweep · lr × wd" on the left and "64 runs" (mono 11px, `--text-3`) on
   the right; then the sweep chart SVG copied from `SweepChart` in
   AuthScreen.tsx (viewBox `0 0 312 132`, three grey curves, one `--s1`
   curve, the best point with its halo, label "best · 0.0417"). Colour SVG
   elements through CSS classes or `style="stroke: var(--…)"`: presentation
   attributes cannot take `var()`. `role="img"` with an `aria-label`.
3. A three-row list with a top hairline and a hairline under each row: a
   6px dot, run name (mono 11px, `--text-2`), status on the right (mono
   11px, `--text-3`): `vit-b16-aug` converged (`--s3`), `tune-lr-cosine`
   running (`--s1`), `baseline-resnet` converged (`--s5`).
4. Pushed to the bottom (`margin-top: auto`, max-width 480px, gap 12px):
   "Every run, side by side — and a reason you can share." (28px, weight
   500, line-height 1.28, letter-spacing -0.025em, `--text`), then "Log,
   organise and compare your experiments in one workspace your team
   shares." (14.5px, line-height 1.6, `--text-3`). Do not copy the server's
   "report" sentence: this product has no reports.

**Right: form side** (`<div class="auth-form-side">`): flex column, padding
`32px 40px 40px`, background `var(--surface)`, `min-width: 0`.

1. Top row: the compact logo (22px), shown only at ≤ 960px.
2. A centred area (`flex: 1`, centred both ways, padding `32px 0`) holding
   `<main class="auth-main">`: width 100%, max-width 384px, flex column,
   gap 28px.
3. No footer on either page (legal links come with the owner's policies).

**≤ 960px:** one column; the brand panel is hidden (`display: none`); the
compact logo shows. **≤ 560px:** form side padding `24px 20px 28px`. Zero
horizontal overflow at 375px wide, including with the longest error message
and both hidden error buttons shown (the existing overflow test does this).

## Logo

Inline the agenticML lockup SVG in each HTML file (no new asset route: the
CSP allows inline SVG, and routes are a security surface). The artwork is
`website/src/assets/logo-light.svg` (byte-identical to agenticml-server's
`public/logo.svg`): a 96×96 rounded mark plus the "agenticML" wordmark,
viewBox `0 0 398 96`. Copy its paths exactly. Replace the fixed fills with
classes: the mark path gets `class="auth-logo-mark"` (`fill:
var(--accent)`), the wordmark group `class="auth-logo-word"` (`fill:
var(--text)`), so the logo follows the panel it sits in (light form side:
`#2f6fdb` mark, dark text; dark brand panel: `#8ab0fa` mark, light text,
matching `logo-dark.svg`). Root `<svg class="auth-logo" role="img"
aria-label="agenticML">` with `<title>agenticML</title>`; height from CSS
(24px in the brand panel, 22px compact), `width: auto`. Only one of the two
logos is ever displayed, because the other's container is `display: none`.

## Hosted page (`firebase-login.html`) content

Inside `.auth-main`, in order:

1. Tabs: `<div role="tablist" aria-label="Sign in or sign up"
   class="auth-tabs">` with two `<button type="button" role="tab">`:
   `#tab-signin` "Sign in" (`aria-selected="true"`) and `#tab-signup`
   "Sign up". Style from `tabStyle` in AuthScreen.tsx: container
   `--surface-3`, padding 3px, radius 7px, gap 4px; selected tab
   `--surface`, `--text`, `--shadow-1`; other tab transparent, `--text-3`;
   13px weight 500, padding `7px 10px`, radius 5px, `flex: 1`. Visible
   focus ring.
2. Heading block (gap 7px): `<h1 id="auth-title">` 30px, weight 500,
   line-height 1.16, letter-spacing -0.03em; `<p id="auth-sub">` 14.5px,
   line-height 1.55, `--text-3`.
   - Sign in: "Sign in to your workspace" / "Pick up your runs and
     comparisons where you left them."
   - Sign up: "Create your workspace" / "Start logging experiments in
     minutes."
3. `<form id="email-login">` (gap 24px):
   - Fields block (gap 18px). Each field: label (mono 10.5px, uppercase,
     letter-spacing 0.12em, `--text-3`) above an input with the existing
     `.field-underline` class. `#email` (type email, required, placeholder
     `you@company.com`, autocomplete `username`), `#password` (type
     password, required, autocomplete `current-password`, `new-password` in
     Sign up). Under the password, `<button type="button"
     id="show-password" aria-pressed="false">` "show password" / "hide
     password" (mono 10.5px, uppercase, letter-spacing 0.08em, `--text-2`,
     no border or background) that toggles the input type.
   - Buttons block (gap 12px): submit `.btn .btn-primary` with
     `min-height: 40px`, text "Sign in" (Sign up mode: "Sign up"); then
     `#google` `.btn .btn-secondary` (same height, gap 10px) with the
     four-colour Google "G" SVG from AuthScreen.tsx (`aria-hidden`) and the
     exact text "Continue with Google".
4. Links row (13.5px): `#forgot` as a `.link-button` (new class: accent
   colour, no border, background or padding, `--accent-hover` on hover),
   text "Forgot password?"; hidden in Sign up mode.
5. `#error` (`role="alert"`, hidden), `#resend` "Resend email" and
   `#continue-without-invite` "Continue without the invite" (ghost buttons,
   hidden), unchanged in id, text and behaviour. Error text uses
   `--danger-text`, 13.5px, and wraps (`overflow-wrap: anywhere`).

Remove the old `#create-account` button; Sign up mode replaces it.

## JavaScript (`firebase-login.js`)

Keep every existing function and message. Changes:

- Add `let mode = "signin"` and `setMode(next)`: updates both tabs'
  `aria-selected` and `tabindex` (selected 0, other -1), the heading, sub
  line, submit text, `#forgot` hidden state and the password autocomplete,
  and calls `clearError()`. Left/Right arrow keys on a tab move focus to
  and select the other tab.
- Form submit: Sign in mode → existing `submitEmail` through
  `runExclusive`. Sign up mode → the existing create-account flow
  (`createUserWithEmailAndPassword` → `sendEmailVerification` →
  `showError(VERIFY_EMAIL, "resend")`, errors through
  `showFirebaseError`), also through `runExclusive` so the buttons are
  disabled while it runs.
- `#show-password`: toggles `passwordInput.type`, its text and
  `aria-pressed`.
- Tabs and show-password do not need Firebase: bind them at load (outside
  `bind()`), so they work even when the config request fails. Everything
  else stays in `bind()`.
- No inline handlers and no inline `<script>` (an existing test asserts
  `onclick=` and `<script>` are absent). The page's CSP is unchanged.

## Self-hosted page (`login.html`)

Same shell: brand panel and compact logo exactly as above. Inside
`.auth-main`: keep `<h1>Sign in</h1>` (many tests depend on it) with the new
heading style, add a sub line "Use your user name and password, or an admin
key.", then the existing `#password-login` form, `#use-key` button, `#login`
key form and `#error`, with their ids, texts, `hidden` attributes and
`login.js` behaviour unchanged. Restyle: labels as above, inputs
`.field-underline`, submit buttons `.btn .btn-primary` at 40px, `#use-key`
as `.link-button`. No tabs on this page. `login.js` needs no change.

## CSS (`app.css`)

Add a commented `/* Sign-in pages (package 27) */` section after the
existing `.login-*` rules. Keep `.login-page` and `.login-card`:
`setup.html` and `invite.html` still use them. Use tokens only (no new raw
hex except the brand panel's local dark token values copied from the dark
theme block, and the Google logo's brand colours inside its SVG). The
sign-in pages' body gets `class="auth-page"` (`margin: 0`).

## Tests and checks

Update:

- `tests/test_server_firebase.py` (around line 772): the hosted page's
  heading is now "Sign in to your workspace"; assert on it and keep the
  other assertions.
- `testbed/tests/test_server_firebase_dashboard.py`: headings "Sign in" →
  "Sign in to your workspace";
  `test_firebase_create_account_sends_verification` clicks the "Sign up"
  tab, fills the fields, clicks the "Sign up" button; the forgot-password
  test clicks "Forgot password?".

Add (testbed UI tests, `ui` and `browser` marks):

- Hosted: the tabs switch the heading, sub line and submit text and hide
  "Forgot password?"; `aria-selected` follows; switching back restores
  Sign in; arrow keys switch tabs.
- Hosted: show password toggles the input type and `aria-pressed`.
- Hosted and self-hosted at 1280px: the brand panel and its logo are
  visible; at 375px: the brand panel is hidden, the compact logo is
  visible, and there is no horizontal overflow (self-hosted: also with the
  key form shown).

Run:

- `testbed/.venv/bin/python -m pytest -q tests` from the repo root.
- `.venv/bin/python -m pytest -q -m ui` from `testbed/`.

This worktree has no `testbed/.venv`; use
`/home/bharath/CAD-AI/agenticML/mlLinearTracking/testbed/.venv/bin/python`
with `PYTHONPATH=<this worktree>/src` (and check `agenticml.__file__`
points into this worktree), so the tests run this branch's code.

## Edge cases

- `/invite?token=…` serves the same page; the token logic is unchanged and
  must keep working in both modes.
- Busy state disables `#google` and the submit button in both modes.
- Firebase config failure: tabs and show-password still work; the error
  shows.
- Long error text and the two hidden error buttons must not overflow at
  375px.
- Keyboard: Tab order is the selected tab → email → password → show
  password → submit → Google → Forgot password?; arrow keys switch tabs.

## Done when

Both pages match the design board's layout at 1280px and collapse to one
column with the compact logo at ≤ 960px, all existing and new tests pass,
there is no overflow at 375px, and the roadmap row for package 27 is
`done (<PR link>)`.
