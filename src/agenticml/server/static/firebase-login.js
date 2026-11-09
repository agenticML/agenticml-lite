(() => {
  const error = document.querySelector("#error");
  const resend = document.querySelector("#resend");
  const continueWithoutInvite = document.querySelector("#continue-without-invite");
  const google = document.querySelector("#google");
  const form = document.querySelector("#email-login");
  const submit = form.querySelector('button[type="submit"]');
  const emailInput = document.querySelector("#email");
  const passwordInput = document.querySelector("#password");
  const createAccount = document.querySelector("#create-account");
  const forgot = document.querySelector("#forgot");

  const NO_ACCOUNT =
    "Your account has no access to this server. Ask a workspace admin for an invite.";
  const VERIFY_EMAIL =
    "Check your inbox to verify your email, then sign in again.";
  const GENERIC = "Sign-in failed. Try again.";
  const POPUP_BLOCKED_INVITE =
    "Your browser blocked the sign-in window. Allow pop-ups for this site and try again.";
  const FIREBASE_ERRORS = {
    "auth/invalid-credential": "Wrong email or password.",
    "auth/wrong-password": "Wrong email or password.",
    "auth/user-not-found": "Wrong email or password.",
    "auth/email-already-in-use": "An account with this email already exists. Sign in instead.",
    "auth/weak-password": "Choose a longer password.",
    "auth/too-many-requests": "Too many attempts. Try again later.",
  };
  const SILENT_FIREBASE = {
    "auth/popup-closed-by-user": true,
    "auth/cancelled-popup-request": true,
  };

  let invite = null;
  let busy = false;
  if (location.pathname === "/invite") {
    invite = new URLSearchParams(location.search).get("token") || null;
    if (location.search) history.replaceState(null, "", "/invite");
  }

  function hideExtras() {
    resend.hidden = true;
    continueWithoutInvite.hidden = true;
  }

  function showError(message, extra) {
    error.textContent = message;
    error.hidden = false;
    resend.hidden = extra !== "resend";
    continueWithoutInvite.hidden = extra !== "continue";
  }

  function clearError() {
    error.hidden = true;
    error.textContent = "";
    hideExtras();
  }

  function firebaseError(err) {
    const code = err && err.code;
    if (code && SILENT_FIREBASE[code]) return null;
    if (code && FIREBASE_ERRORS[code]) return FIREBASE_ERRORS[code];
    return GENERIC;
  }

  function showFirebaseError(err, extra) {
    const message = firebaseError(err);
    if (message == null) return;
    showError(message, extra);
  }

  function setBusy(on) {
    busy = on;
    google.disabled = on;
    submit.disabled = on;
  }

  async function runExclusive(work) {
    if (busy) return;
    setBusy(true);
    let keepDisabled = false;
    try {
      keepDisabled = Boolean(await work());
    } finally {
      if (!keepDisabled) setBusy(false);
    }
  }

  async function signOutQuiet() {
    try {
      await firebase.auth().signOut();
    } catch {
      /* best effort */
    }
  }

  async function postIdToken(idToken, inviteToken) {
    const body = { id_token: idToken };
    if (inviteToken) body.invite = inviteToken;
    return fetch("/auth/firebase", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-AgenticML-Request": "1",
      },
      body: JSON.stringify(body),
    });
  }

  async function handleServerResponse(response) {
    if (response.ok) {
      await signOutQuiet();
      location.assign("/");
      return true;
    }
    let data = null;
    if (
      response.status === 401 ||
      response.status === 403 ||
      response.status === 429 ||
      response.status === 503
    ) {
      try {
        data = await response.json();
      } catch {
        data = null;
      }
    }
    const reason = data && data.reason;
    const serverText =
      data && typeof data.error === "string" && data.error ? data.error : GENERIC;
    if (reason === "email_unverified") {
      showError(VERIFY_EMAIL, "resend");
      return false;
    }
    if (reason === "invite_invalid") {
      showError(serverText, "continue");
      return false;
    }
    await signOutQuiet();
    if (reason === "no_account") {
      showError(NO_ACCOUNT);
      return false;
    }
    if (reason === "account_exists") {
      showError(serverText);
      return false;
    }
    showError(serverText);
    return false;
  }

  async function afterSignIn() {
    clearError();
    const user = firebase.auth().currentUser;
    if (!user) {
      showError(GENERIC);
      return false;
    }
    let idToken;
    try {
      idToken = await user.getIdToken(true);
    } catch (err) {
      showFirebaseError(err);
      return false;
    }
    let response;
    try {
      response = await postIdToken(idToken, invite);
    } catch {
      showError(GENERIC);
      return false;
    }
    return handleServerResponse(response);
  }

  async function signInWithGoogle() {
    clearError();
    const provider = new firebase.auth.GoogleAuthProvider();
    try {
      await firebase.auth().signInWithPopup(provider);
    } catch (err) {
      if (err && err.code === "auth/popup-blocked") {
        if (invite) {
          showError(POPUP_BLOCKED_INVITE);
          return false;
        }
        try {
          await firebase.auth().signInWithRedirect(provider);
        } catch (redirectErr) {
          showFirebaseError(redirectErr);
        }
        return false;
      }
      showFirebaseError(err);
      return false;
    }
    return afterSignIn();
  }

  async function submitEmail() {
    clearError();
    try {
      await firebase.auth().signInWithEmailAndPassword(
        emailInput.value, passwordInput.value);
    } catch (err) {
      showFirebaseError(err);
      return false;
    }
    return afterSignIn();
  }

  function bind() {
    google.addEventListener("click", () => {
      void runExclusive(signInWithGoogle);
    });
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      void runExclusive(submitEmail);
    });
    createAccount.addEventListener("click", () => {
      if (busy || !form.reportValidity()) return;
      clearError();
      firebase.auth()
        .createUserWithEmailAndPassword(emailInput.value, passwordInput.value)
        .then((cred) => cred.user.sendEmailVerification())
        .then(() => showError(VERIFY_EMAIL, "resend"))
        .catch((err) => showFirebaseError(err));
    });
    forgot.addEventListener("click", () => {
      if (busy || !emailInput.reportValidity()) return;
      clearError();
      firebase.auth()
        .sendPasswordResetEmail(emailInput.value)
        .then(() => showError("Check your inbox for a password reset email."))
        .catch((err) => showFirebaseError(err));
    });
    resend.addEventListener("click", () => {
      const user = firebase.auth().currentUser;
      if (!user) {
        showError(VERIFY_EMAIL);
        return;
      }
      user.sendEmailVerification()
        .then(() => showError(VERIFY_EMAIL, "resend"))
        .catch((err) => showFirebaseError(err, "resend"));
    });
    continueWithoutInvite.addEventListener("click", () => {
      void runExclusive(async () => {
        const user = firebase.auth().currentUser;
        if (!user) {
          showError(GENERIC);
          return false;
        }
        invite = null;
        clearError();
        try {
          const idToken = await user.getIdToken(true);
          const response = await postIdToken(idToken, null);
          return handleServerResponse(response);
        } catch {
          showError(GENERIC);
          return false;
        }
      });
    });
  }

  async function start() {
    let response;
    try {
      response = await fetch("/auth/firebase/config", { credentials: "same-origin" });
    } catch {
      showError(GENERIC);
      return;
    }
    if (!response.ok) {
      showError(GENERIC);
      return;
    }
    let config;
    try {
      config = await response.json();
    } catch {
      showError(GENERIC);
      return;
    }
    try {
      firebase.initializeApp({
        apiKey: config.apiKey,
        authDomain: config.authDomain,
        projectId: config.projectId,
      });
      await firebase.auth().setPersistence(firebase.auth.Auth.Persistence.NONE);
    } catch (err) {
      showFirebaseError(err);
      return;
    }
    bind();
    try {
      const redirected = await firebase.auth().getRedirectResult();
      if (redirected && redirected.user) {
        await runExclusive(afterSignIn);
      }
    } catch (err) {
      showFirebaseError(err);
    }
  }

  window.addEventListener("pagehide", () => {
    try {
      firebase.auth().signOut();
    } catch {
      /* best effort */
    }
  });

  start();
})();
