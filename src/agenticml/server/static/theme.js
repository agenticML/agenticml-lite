// Theme for the sign-in pages. Same key, values and cycle as the dashboard
// (applyTheme() in ui/static/app.js), so a choice made here carries into the
// dashboard and back. A classic blocking script in <head>, so the right theme
// is set before first paint. It is the only code on these pages that writes
// web storage: the key `agenticml-theme`, and only when the toggle is clicked.
// It never reads or writes tokens and sends nothing.
(() => {
  const KEY = "agenticml-theme";
  const ORDER = ["system", "light", "dark"];
  let pref = "system";
  const read = () => {
    let stored = null;
    try {
      stored = localStorage.getItem(KEY);
    } catch (_) {}
    return ORDER.includes(stored) ? stored : "system";
  };
  const media = matchMedia("(prefers-color-scheme: dark)");
  const apply = () => {
    document.documentElement.dataset.theme =
      pref === "system" ? (media.matches ? "dark" : "light") : pref;
    const toggle = document.querySelector("#auth-theme");
    if (toggle) toggle.textContent = `Theme: ${pref}`;
  };
  pref = read();
  apply();
  media.addEventListener("change", apply);
  // Follow a change made in another tab (the dashboard or another sign-in page).
  addEventListener("storage", (event) => {
    if (event.key !== KEY) return;
    pref = read();
    apply();
  });
  document.addEventListener("DOMContentLoaded", () => {
    const toggle = document.querySelector("#auth-theme");
    if (!toggle) return;
    apply();
    toggle.addEventListener("click", () => {
      pref = ORDER[(ORDER.indexOf(pref) + 1) % ORDER.length];
      try {
        localStorage.setItem(KEY, pref);
      } catch (_) {}
      apply();
    });
  });
})();
