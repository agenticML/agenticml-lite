// Same few lines as signOut() in ui/static/app.js: the dashboard module is
// account-scoped and local `agenticml ui` does not serve this file.
const button = document.querySelector("#sign-out");
if (button) {
  button.addEventListener("click", async () => {
    const response = await fetch("/logout", {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-AgenticML-Request": "1" },
    });
    let target = "/login";
    // An OIDC server answers 200 with the provider's sign-out address.
    if (response.status === 200) {
      try {
        const data = await response.json();
        const url = new URL(data.redirect);
        if (url.protocol === "https:" || url.protocol === "http:") target = url.href;
      } catch {}
    }
    location.assign(target);
  });
}
