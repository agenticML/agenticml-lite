let token = new URLSearchParams(location.search).get("token");
history.replaceState(null, "", "/setup");

const form = document.querySelector("#setup");
const error = document.querySelector("#error");
const nameInput = document.querySelector("#name");
const value = document.querySelector("#value");
const command = document.querySelector("#command");
const submit = form.querySelector("button");

function shellQuote(text) {
  return "'" + text.replaceAll("'", "'\\''") + "'";
}

function copy(button, element) {
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(element.textContent);
    } catch {
      const range = document.createRange();
      range.selectNodeContents(element);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    }
  });
}
copy(document.querySelector("#copy-value"), value);
copy(document.querySelector("#copy-command"), command);

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  error.hidden = true;
  error.textContent = "";
  submit.disabled = true;
  try {
    const response = await fetch("/setup", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-AgenticML-Request": "1",
      },
      body: JSON.stringify({ token, name: nameInput.value }),
    });
    const data = await response.json().catch(() => null);
    if (!response.ok) throw new Error(
      (typeof data?.error === "string" && data.error) || `Setup failed (${response.status})`);
    if (data === null || typeof data !== "object" || Array.isArray(data) ||
        typeof data.value !== "string" || typeof data.host !== "string")
      throw new Error(`Setup failed (${response.status})`);
    const loginCommand = `agenticml login --host ${shellQuote(data.host)} --key ${shellQuote(data.value)}`;
    token = null;
    value.textContent = data.value;
    command.textContent = loginCommand;
    document.querySelector("#host-note").hidden = data.host_source !== "request";
    form.remove();
    document.querySelector("#result").hidden = false;
  } catch (failure) {
    error.textContent = failure.message || "Setup failed";
    error.hidden = false;
    submit.disabled = false;
  }
});

window.addEventListener("pagehide", () => {
  token = null;
  value.textContent = "";
  command.textContent = "";
});
