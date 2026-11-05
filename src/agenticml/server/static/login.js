const form = document.querySelector("#login");
const error = document.querySelector("#error");
const keyInput = document.querySelector("#key");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  error.hidden = true;
  error.textContent = "";
  let response;
  try {
    response = await fetch("/login", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-AgenticML-Request": "1",
      },
      body: JSON.stringify({ key: keyInput.value }),
    });
  } catch {
    error.textContent = "Sign in failed";
    error.hidden = false;
    return;
  }
  if (response.ok) {
    location.assign("/");
    return;
  }
  let message = "Sign in failed";
  if (response.status === 401 || response.status === 403 || response.status === 429) {
    try {
      const data = await response.json();
      if (typeof data.error === "string" && data.error) message = data.error;
    } catch {}
  }
  error.textContent = message;
  error.hidden = false;
});
