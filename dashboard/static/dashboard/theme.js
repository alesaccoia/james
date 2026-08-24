(function () {
  "use strict";

  function currentTheme() {
    return document.documentElement.dataset.theme === "light" ? "light" : "dark";
  }

  function updateControls() {
    var next = currentTheme() === "dark" ? "Light" : "Dark";
    document.querySelectorAll("[data-theme-label]").forEach(function (label) {
      label.textContent = next;
    });
  }

  function setTheme(theme) {
    document.documentElement.dataset.theme = theme;
    document.cookie = "james_theme=" + theme + "; Path=/; Max-Age=31536000; SameSite=Lax";
    window.dispatchEvent(new CustomEvent("themechange", { detail: { theme: theme } }));
    updateControls();
  }

  document.addEventListener("DOMContentLoaded", function () {
    updateControls();
    document.querySelectorAll("[data-theme-toggle]").forEach(function (button) {
      button.addEventListener("click", function () {
        setTheme(currentTheme() === "dark" ? "light" : "dark");
        window.location.reload();
      });
    });
  });
})();
