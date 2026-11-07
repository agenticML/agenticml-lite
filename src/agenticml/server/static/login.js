const passwordForm = document.querySelector("#password-login");
const keyForm = document.querySelector("#login");
const useKey = document.querySelector("#use-key");
const error = document.querySelector("#error");
const userInput = document.querySelector("#user");
const passwordInput = document.querySelector("#password");
const keyInput = document.querySelector("#key");

function showError(message) {
  error.textContent = message;
  error.hidden = false;
}

async function postLogin(body) {
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
      body: JSON.stringify(body),
    });
  } catch {
    showError("Sign in failed");
    return;
  }
  if (response.ok) {
    location.assign("/");
    return;
  }
  let message = "Sign in failed";
  if (response.status === 401 || response.status === 403 || response.status === 429 || response.status === 503) {
    try {
      const data = await response.json();
      if (typeof data.error === "string" && data.error) message = data.error;
    } catch {}
  }
  showError(message);
}

passwordForm.addEventListener("submit", (event) => {
  event.preventDefault();
  postLogin({ user: userInput.value, password: passwordInput.value });
});

keyForm.addEventListener("submit", (event) => {
  event.preventDefault();
  postLogin({ key: keyInput.value });
});

useKey.addEventListener("click", () => {
  const showingKey = keyForm.hidden;
  keyForm.hidden = !showingKey;
  passwordForm.hidden = showingKey;
  useKey.textContent = showingKey ? "Sign in with a password" : "Sign in with an admin key";
});
