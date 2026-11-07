let token = new URLSearchParams(location.search).get("token");
history.replaceState(null, "", "/invite");

const form = document.querySelector("#invite");
const error = document.querySelector("#error");
const passwordInput = document.querySelector("#password");
const password2Input = document.querySelector("#password2");
const submit = form.querySelector("button");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  error.hidden = true;
  error.textContent = "";
  if (passwordInput.value !== password2Input.value) {
    error.textContent = "Passwords do not match";
    error.hidden = false;
    return;
  }
  submit.disabled = true;
  try {
    const response = await fetch("/invite", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-AgenticML-Request": "1",
      },
      body: JSON.stringify({ token, password: passwordInput.value }),
    });
    const data = await response.json().catch(() => null);
    if (!response.ok) throw new Error(
      (typeof data?.error === "string" && data.error) || `Invite failed (${response.status})`);
    if (data === null || typeof data !== "object" || Array.isArray(data) || data.ok !== true)
      throw new Error(`Invite failed (${response.status})`);
    token = null;
    location.assign("/");
  } catch (failure) {
    error.textContent = failure.message || "Invite failed";
    error.hidden = false;
    submit.disabled = false;
  }
});

window.addEventListener("pagehide", () => {
  token = null;
  passwordInput.value = "";
  password2Input.value = "";
});
