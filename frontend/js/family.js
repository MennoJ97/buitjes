/*
  A copy of https://mhjansen.nl/family.js (servarr-docker-compose,
  web/landing/family.js). Copied, not linked: this site sends
  script-src 'self'. Keep it identical to the original, bar this note.
*/
/*
  family.js — the light/dark switch every mhjansen.nl site shares, next to
  family.css (which holds both palettes; see there for how a page links them).

  Pages follow the system theme until the reader flips the switch. The choice
  is a cookie on .mhjansen.nl rather than localStorage, because localStorage is
  per origin: flipping it on stonks.mhjansen.nl must also hold on uitjes and on
  the apex. Picking the theme the system already shows deletes the cookie, so
  one click back always returns to "follow the system" — there is no third
  state to find.

  Load it blocking in <head>, straight after family.css: it sets data-theme on
  <html> before the first paint. Once the DOM is there it fills every
  <button class="theme-toggle" hidden> with the icon and shows it.

  A page that bakes colours in at draw time (stonks' canvas and Plotly charts)
  listens for the "themechange" event on window, fired whenever the theme on
  screen changes: the switch, the system flipping, or another tab or site
  changing the cookie.
*/
(function () {
  var root = document.documentElement;
  var media = matchMedia("(prefers-color-scheme: dark)");
  // Only on the real hosts: a cookie for another domain is refused outright,
  // which would leave the switch dead on a local copy.
  var scope = "; Path=/; SameSite=Lax" +
    (/(^|\.)mhjansen\.nl$/.test(location.hostname) ? "; Domain=mhjansen.nl" : "") +
    (location.protocol === "https:" ? "; Secure" : "");

  var ICON = '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">' +
    '<circle cx="8" cy="8" r="6.25" fill="none" stroke="currentColor" stroke-width="1.5"/>' +
    '<path d="M8 1.75a6.25 6.25 0 0 1 0 12.5z" fill="currentColor"/></svg>';
  var LABEL = /^nl\b/.test(root.lang) ? "Donkere modus" : "Dark mode";

  function saved() {
    var m = document.cookie.match(/(?:^|;\s*)theme=(light|dark)(?:;|$)/);
    return m ? m[1] : null;
  }
  function system() { return media.matches ? "dark" : "light"; }
  function current() { return root.getAttribute("data-theme") || system(); }

  var shown = null;
  function apply() {
    var choice = saved();
    if (choice) root.setAttribute("data-theme", choice);
    else root.removeAttribute("data-theme");

    var now = current();
    var buttons = document.querySelectorAll(".theme-toggle");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].setAttribute("aria-pressed", String(now === "dark"));
    }
    if (shown && now !== shown) window.dispatchEvent(new Event("themechange"));
    shown = now;
  }

  function toggle() {
    var next = current() === "dark" ? "light" : "dark";
    document.cookie = next === system()
      ? "theme=; Max-Age=0" + scope
      : "theme=" + next + "; Max-Age=31536000" + scope;
    apply();
  }

  function wire() {
    var buttons = document.querySelectorAll(".theme-toggle");
    for (var i = 0; i < buttons.length; i++) {
      var b = buttons[i];
      b.type = "button";
      b.innerHTML = ICON;
      b.setAttribute("aria-label", LABEL);
      b.title = LABEL;
      b.addEventListener("click", toggle);
      b.hidden = false;
    }
    apply();
  }

  apply();
  media.addEventListener("change", apply);
  // A page restored from the back/forward cache, or a tab come back to, may
  // be showing a theme the reader has since switched away from elsewhere.
  window.addEventListener("pageshow", apply);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) apply();
  });

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
  else wire();
})();
