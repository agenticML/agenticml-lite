# Package 40: Blog on the hosted sign-in page

Status: done (#105, merged Nov 27, 2026; live after hosted deploy run 46012459165).

## Goal

Show the blog on the hosted sign-in and sign-up page (`app.agenticml.xyz`).
The owner asked for this on Nov 27, 2026: "how are we gonna show blog on
app.agenticml.xyz, we should have it in sign up page". They chose a "Latest
from the blog" card plus a Blog link over a plain link the same day.

Nothing new is hosted. The blog stays on the docs site (package 39). The sign-in
page links to it, and the visitor's browser reads the blog's RSS feed to show
the newest post, so a new post shows up with no app deploy.

Depends on package 39: the feed `https://agenticml.xyz/blog/rss.xml` must be
live (after the 38.2 cutover). Until then the card stays hidden and the link
leads to a page that does not exist yet, so ship it after the cutover.

## What the visitor sees

- **Brand panel (wider than 960px):** under the pitch, a card headed "From the
  blog" with the newest post's title (a link to the post), its date, and an
  "All posts" link to the blog. The panel is hidden at 960px and narrower, and so
  is the card.
- **Under the form (every width):** a "Blog" link to the blog, on its own line
  `#blog` below the policy and contact lines, in the same `.auth-legal` style.
- Both appear on the Sign in and Sign up tabs. Without the option nothing
  changes.

## Part 40.1: server option (Sonnet)

- `src/agenticml/server/webauth.py`:
  - Generalize `normalize_legal_base_url(value)` into a helper that takes the
    flag name for its messages (for example
    `_normalize_https_base_url(value, flag)`). Keep
    `normalize_legal_base_url` as a thin wrapper so the current callers and
    tests keep working. Add `normalize_blog_url(value)` for `--blog-url`.
    The rules are the same: `https://` only, a host, no spaces or control
    characters, no credentials, query or fragment, and a `/` added at the end.
  - `WebAuth.__init__` takes `blog_url=None`. Like `legal_base_url`, it is
    ignored with a warning (`--blog-url is ignored without --firebase-project`)
    when Firebase is off, because only the Firebase sign-in page uses it.
  - `_firebase_config`: when set, add `"blog": {"url": blog_url, "feed":
    blog_url + "rss.xml"}`.
  - `firebase_login_csp(auth_domain, blog_origin=None)`: when a blog URL is
    set, append its origin (`https://host[:port]`, no path) to `connect-src`
    and to nothing else. The call at about line 1236 passes it. No other page's
    CSP changes.
- `src/agenticml/server/app.py`: pass `blog_url` through the same three
  places as `legal_base_url`.
- `src/agenticml/cli.py`: `--blog-url URL` on `server serve` (env
  `AGENTICML_SERVER_BLOG_URL`; the flag wins; empty means unset), copying
  `_legal_base_url_from_args`. A bad value stops the server at startup with the
  message, as `--legal-base-url` does.
- `deploy/aws/cloudformation/hosted.yaml`: a `BlogUrl` parameter, default
  `https://agenticml.xyz/blog/`, with the same `AllowedPattern` and
  `MaxLength` as `LegalBaseUrl` and the description "https:// URL of the blog
  shown on the sign-in page; empty shows nothing. Needs Firebase sign-in." Map it
  to `AGENTICML_SERVER_BLOG_URL`. Leave `deploy.yml` as it is: the default
  covers the hosted service, as with `ContactEmail`.
- Do not add it to the self-deploy template (`deploy/aws-self-hosted/`), which
  uses OIDC, not Firebase.

## Part 40.2: sign-in page (Sonnet)

- `src/agenticml/server/static/firebase-login.html`: an empty, hidden card
  `<section id="blog-card" class="auth-blog" hidden aria-label="From the blog">`
  in the brand panel after `.auth-brand-pitch`, and `<p id="blog"
  class="auth-legal" hidden></p>` after `#contact`.
- `src/agenticml/server/static/firebase-login.js`, `showBlog(config.blog)`
  called next to `showLegal`/`showContact`:
  - Check `url` and `feed` with `httpsUrl()`. Either one bad means do nothing.
  - Fill `#blog` with a "Blog" link to `url` (use `legalLink`) and unhide it.
    This does not wait for the feed.
  - Fetch the feed without blocking sign-in. Firebase setup must not wait for it,
    and a slow or failed feed never shows an error. Use
    `fetch(feed, {credentials: "omit", mode: "cors", redirect: "error",
    referrerPolicy: "no-referrer"})` with a 5 s `AbortController` timeout.
    Require `response.ok`. Read at most 256 KB of text (check the
    `content-length` header and the text length) and parse it with
    `DOMParser(..., "application/xml")`. Give up on a `parsererror`.
  - Take the first `<item>`: its `title`, `link` and `pubDate`. Use it only
    if the title is non-empty after trimming and the link is `https://` on
    the same origin as `url`. Cut the title to 120 characters with an ellipsis.
  - Render the card with DOM methods and `textContent` only (never
    `innerHTML`):
    - the heading "From the blog";
    - the title as a link to the post;
    - the date, formatted with `toLocaleDateString("en", {year: "numeric",
      month: "short", day: "numeric", timeZone: "UTC"})`, or left out when it
      doesn't parse;
    - an "All posts" link to `url`.
    Links open in the same tab, like the policy links, with
    `rel="noopener"` if `legalLink` sets a target. Then unhide the card.
  - Any failure leaves the card hidden. No `console.error`: at most a
    `console.debug`.
- `src/agenticml/ui/static/app.css`: `.auth-blog` styles inside the dark
  brand panel (use the panel's own variables, a hairline border, small type, a
  title clamped to two lines, `min-width: 0`, `overflow-wrap: anywhere`). Keep
  `.auth-brand-pitch`'s `margin-top: auto` working: put the card after the
  pitch so the pitch stays at the bottom, and give the card a `margin-top` of
  its own. Nothing may overflow at 961px and above, and the page must keep zero
  horizontal overflow at 375px.

## Part 40.3: docs (Sonnet)

- `docs/SERVER.md`: a row for `--blog-url` in the options table, after
  `--legal-base-url`, written in the same style. It covers what it shows, that
  it needs Firebase, the CSP `connect-src` addition, and that the visitor's
  browser fetches the feed (so the host must send `Access-Control-Allow-Origin`;
  GitHub Pages does).
- `docs/HOSTED.md`: one sentence next to the policy-links paragraph saying the
  hosted sign-in page shows the newest post from `agenticml.xyz/blog/`.
- `docs/THREAT_MODEL.md`: under section 6 (Firebase authentication), a short
  paragraph. The sign-in page reads one feed URL that the operator set. The CSP
  allows only that origin for `connect-src`. The page sends no cookies and no
  referrer, renders text only, and accepts post links only on the blog's
  origin. A compromised blog host could change the card's text and link, but it
  cannot run script on the app origin.

## Tests (Sonnet)

Unit tests in `tests/test_server_firebase.py`, following the legal-URL tests
near line 870:

- The config has `blog.url`/`blog.feed` only when set.
- Bad values (`http://`, credentials, query, fragment, spaces) raise
  `ValueError` mentioning `--blog-url`, and the CLI exits 1 with that message.
- The flag beats the env, and empty means unset.
- The option is ignored with a warning when Firebase is off.
- The Firebase sign-in page's CSP has the blog origin in `connect-src` only
  when set. No other page's CSP has it, and `script-src` and the other
  directives are unchanged.

`tests/test_deploy_cloudformation.py`: `BlogUrl` exists with its default, and
the env var maps to it.

UI tests in `testbed/tests/test_server_firebase_dashboard.py`, using Playwright
`page.route` to serve the feed for a test blog URL such as
`https://blog.example.test/blog/`, with no network access:

- A valid feed with two items shows the card with the first item's title, date
  and link, and the "All posts" link. `#blog` shows the Blog link.
- Without the option, there is no card and no `#blog`.
- The feed fails (500, abort/timeout, not XML, a link on another origin, an
  `http:` link, an empty title): the card stays hidden, `#blog` still shows,
  sign-in still works (the Sign in button is enabled), and there is no console
  error.
- A title containing `<img src=x onerror=...>` renders as text, with no element
  created.
- No horizontal overflow at 375px and at 1280px, in both themes, with a 200
  character title.

## Checks

- `testbed/.venv/bin/python -m pytest -q tests` from the repo root.
- `.venv/bin/python -m pytest -q -m ui` from `testbed/`.
- `cfn-lint deploy/aws/cloudformation/*.yaml` if it is installed (CI runs it).

## Review

This changes a Content-Security-Policy, so it needs the † second review. With
Codex and Cursor off, the supervisor does it.

## Done when

- The tests above pass, and the PR is merged after the 38.2 cutover.
- After the hosted deploy, `app.agenticml.xyz` shows the newest post from
  `agenticml.xyz/blog/` in the brand panel and a Blog link under the form.
