/* Sign-in. The access token is posted in a request body and kept in an HttpOnly cookie; it is never put in a URL. */
"use strict";
(() => {
  const required = document.querySelector('meta[name="hydraweave-login"]');
  let leaving = false;
  // A signed-in page whose session ended (token rotated, cookie gone) receives 401: drop the data on screen and
  // load the sign-in screen instead of leaving a stale page up.
  window.HydraweaveLogin = () => {
    if (required || leaving) return;
    leaving = true;
    location.reload();
  };
  if (!required) return;
  const panel = document.createElement("section");
  panel.className = "banner warn";
  panel.setAttribute("aria-label", "Portal sign-in");
  // method=post: without scripts the form still cannot place the token in a URL.
  panel.innerHTML = '<form action="/hydraweave/api/login" method="post" autocomplete="off"><p>Sign in to view this portal. Enter the access token printed on the portal console.</p><label>Access token <input name="token" type="password" required autocomplete="off"></label> <button type="submit">Sign in</button><p id="signin-error" role="alert" hidden></p></form>';
  (document.querySelector("#signin") || document.body).append(panel);
  const form = panel.querySelector("form"), input = panel.querySelector("input"), error = panel.querySelector("#signin-error");
  form.addEventListener("submit", async event => {
    event.preventDefault();
    error.hidden = true;
    const csrf = document.querySelector('meta[name="hydraweave-csrf"]')?.content || "";
    let message = "The portal did not answer. Check that it is still running.";
    try {
      const response = await fetch("/hydraweave/api/login", {method: "POST", credentials: "same-origin",
        headers: {"Content-Type": "application/json", "X-Hydraweave-CSRF": csrf}, body: JSON.stringify({token: input.value})});
      if (response.ok) {
        input.value = "";
        location.reload();
        return;
      }
      message = response.status === 401 ? "That access token is not valid."
        : ((await response.json().catch(() => ({}))).error || "Sign-in failed (" + response.status + ").");
      if (response.status === 403) message += " Reload this page and try again.";
    } catch (_) { /* network failure: the default message */ }
    input.value = "";
    error.textContent = message;
    error.hidden = false;
    input.focus();
  });
  input.focus();
})();
