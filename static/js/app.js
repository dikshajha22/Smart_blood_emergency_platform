/**
 * Shared UI behaviour: mobile nav, dropdowns, dismissible alerts, confirmation
 * prompts and the notification badge poller.
 */
(function () {
  "use strict";

  document.addEventListener("DOMContentLoaded", function () {
    initNavToggle();
    initDropdowns();
    initAlerts();
    initConfirmations();
    initRangeOutputs();
    initNotificationPolling();
  });

  function initNavToggle() {
    const toggle = document.querySelector(".navbar__toggle");
    const links = document.getElementById("nav-links");
    if (!toggle || !links) return;

    const isMobile = () => window.matchMedia("(max-width: 900px)").matches;
    const apply = () => {
      links.hidden = isMobile();
      toggle.setAttribute("aria-expanded", "false");
    };
    apply();
    window.addEventListener("resize", apply);

    toggle.addEventListener("click", function () {
      const open = links.hidden;
      links.hidden = !open;
      toggle.setAttribute("aria-expanded", String(open));
    });
  }

  function initDropdowns() {
    const triggers = document.querySelectorAll("[data-dropdown]");

    triggers.forEach(function (trigger) {
      const panel = document.getElementById(trigger.dataset.dropdown);
      if (!panel) return;

      trigger.addEventListener("click", function (event) {
        event.preventDefault();
        event.stopPropagation();
        const willOpen = panel.hidden;
        closeAll();
        panel.hidden = !willOpen;
        trigger.setAttribute("aria-expanded", String(willOpen));
      });
    });

    function closeAll() {
      document.querySelectorAll("[data-dropdown]").forEach(function (trigger) {
        const panel = document.getElementById(trigger.dataset.dropdown);
        if (panel) panel.hidden = true;
        trigger.setAttribute("aria-expanded", "false");
      });
    }

    document.addEventListener("click", closeAll);
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") closeAll();
    });
  }

  function initAlerts() {
    document.querySelectorAll(".alert__close").forEach(function (button) {
      button.addEventListener("click", function () {
        const alert = button.closest(".alert");
        if (alert) alert.remove();
      });
    });

    // Auto-dismiss success notices; keep errors on screen until acknowledged.
    document.querySelectorAll(".alert-success, .alert-info").forEach(function (alert) {
      setTimeout(function () {
        alert.style.transition = "opacity 400ms";
        alert.style.opacity = "0";
        setTimeout(function () {
          alert.remove();
        }, 400);
      }, 6000);
    });
  }

  function initConfirmations() {
    // Any form carrying data-confirm asks before submitting - used for cancels
    // and other irreversible actions.
    document.querySelectorAll("form[data-confirm]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        if (!window.confirm(form.dataset.confirm)) {
          event.preventDefault();
        }
      });
    });
  }

  function initRangeOutputs() {
    document.querySelectorAll('input[type="range"][data-output]').forEach(function (input) {
      const output = document.getElementById(input.dataset.output);
      if (!output) return;
      const suffix = input.dataset.suffix || "";
      const sync = function () {
        output.textContent = input.value + suffix;
      };
      sync();
      input.addEventListener("input", sync);
    });
  }

  function initNotificationPolling() {
    const badge = document.getElementById("notif-badge");
    const endpoint = badge && badge.dataset.url;
    if (!badge || !endpoint) return;

    const poll = function () {
      if (document.hidden) return;
      fetch(endpoint, {
        headers: { "X-Requested-With": "XMLHttpRequest" },
      })
        .then(function (response) {
          return response.ok ? response.json() : null;
        })
        .then(function (data) {
          if (!data) return;
          badge.textContent = data.count > 99 ? "99+" : data.count;
          badge.hidden = data.count === 0;
        })
        .catch(function () {
          /* Offline or logged out: leave the badge as-is. */
        });
    };

    setInterval(poll, 45000);
  }

  /**
   * CSRF-safe POST helper for the small number of fetch-driven actions.
   */
  window.postForm = function (url, data) {
    const token = document.querySelector('input[name="csrfmiddlewaretoken"]');
    const body = new FormData();
    Object.keys(data || {}).forEach(function (key) {
      body.append(key, data[key]);
    });
    return fetch(url, {
      method: "POST",
      body: body,
      headers: {
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRFToken": token ? token.value : "",
      },
    });
  };
})();
