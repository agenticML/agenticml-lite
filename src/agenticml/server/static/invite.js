let token = new URLSearchParams(location.search).get("token");
history.replaceState(null, "", "/invite");

const existing = document.querySelector("#invite-existing");
const form = document.querySelector("#invite");
const error = document.querySelector("#error");

function showError(message) {
  if (!error) return;
  error.textContent = message;
  error.hidden = false;
}

async function postInvite(body, submit) {
  if (error) {
    error.hidden = true;
    error.textContent = "";
  }
  if (submit) submit.disabled = true;
  try {
    const response = await fetch("/invite", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-AgenticML-Request": "1",
      },
      body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => null);
    if (existing && response.status === 409) {
      token = null;
      location.assign("/login");
      return;
    }
    if (!response.ok) throw new Error(
      (typeof data?.error === "string" && data.error) || `Invite failed (${response.status})`);
    if (data === null || typeof data !== "object" || Array.isArray(data) || data.ok !== true)
      throw new Error(`Invite failed (${response.status})`);
    token = null;
    location.assign("/");
  } catch (failure) {
    showError(failure.message || "Invite failed");
    if (submit) submit.disabled = false;
  }
}

if (existing) {
  const submit = existing.querySelector("button");
  existing.addEventListener("submit", (event) => {
    event.preventDefault();
    postInvite({ token }, submit);
  });
} else if (form) {
  const passwordInput = document.querySelector("#password");
  const password2Input = document.querySelector("#password2");
  const submit = form.querySelector("button");
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (passwordInput.value !== password2Input.value) {
      showError("Passwords do not match");
      return;
    }
    postInvite({ token, password: passwordInput.value }, submit);
  });
  window.addEventListener("pagehide", () => {
    token = null;
    passwordInput.value = "";
    password2Input.value = "";
  });
}
