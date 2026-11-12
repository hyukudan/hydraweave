/* Access-token login is a normal navigation so the credential stays in an HttpOnly cookie. */
"use strict";
(() => {
  const panel = document.createElement("section");
  panel.className = "banner warn";
  panel.hidden = true;
  panel.style.display = "none";
  panel.setAttribute("aria-label", "Portal login");
  panel.innerHTML = '<form action="/" method="get" autocomplete="off"><p>Read-only access. Enter the access token from the portal console to enable actions.</p><label>Access token <input name="token" type="password" required autocomplete="off"></label> <button type="submit">Log in</button></form>';
  document.body.prepend(panel);
  window.HydraweaveLogin = () => {
    panel.hidden = false;
    panel.style.removeProperty("display");
    panel.scrollIntoView({block: "start"});
    panel.querySelector("input").focus();
  };
  if (document.querySelector('meta[name="hydraweave-login"]')) {
    panel.hidden = false;
    panel.style.removeProperty("display");
  }
})();
