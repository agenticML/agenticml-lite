// Same few lines as signOut() in ui/static/app.js: the dashboard module is
// account-scoped and local `agenticml ui` does not serve this file.
const button = document.querySelector("#sign-out");
if (button) {
  button.addEventListener("click", async () => {
    await fetch("/logout", {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-AgenticML-Request": "1" },
    });
    location.assign("/login");
  });
}
