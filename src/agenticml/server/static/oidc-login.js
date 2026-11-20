// Sign-in with an OIDC provider. The button submits a small form to
// /auth/oidc/start. This script only fills it from what the browser holds:
// a same-origin ?next= path, and the invite token from the #token= fragment
// (an invite link's form). With an invite the form is POSTed, so the token
// travels in a request body and never in a request line that a load balancer
// or proxy might log; without one it is a plain GET. Nothing here reads or
// stores tokens beyond that hand-over.
(() => {
  const form = document.querySelector("#signin-form");
  const note = document.querySelector("#invite-note");
  const [inviteField, nextField] = form.querySelectorAll("input");
  const here = new URLSearchParams(location.search);
  const next = here.get("next");
  if (next) {
    nextField.value = next;
    nextField.disabled = false;
  }
  const invite =
    new URLSearchParams(location.hash.slice(1)).get("token") || here.get("token");
  if (invite) {
    inviteField.value = invite;
    inviteField.disabled = false;
    form.method = "post";
  }
  // A server-rendered retry after a failed sign-in already carries the invite.
  if (!inviteField.disabled) note.hidden = false;
  if (location.pathname === "/invite") {
    // The token is in the form now; keep it out of history and the address bar.
    if (location.search || location.hash) history.replaceState(null, "", "/invite");
  } else if (location.pathname !== "/login") {
    // The provider's answer (code, state) lands on the callback URL: scrub it.
    history.replaceState(null, "", "/login");
  }
})();
