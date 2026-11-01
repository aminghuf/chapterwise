// Progressive enhancement for the summary article. The page is complete without this file.
(() => {
  "use strict";
  const root = document.documentElement;
  root.classList.add("js");

  const store = {
    get(key) {
      try { return JSON.parse(localStorage.getItem(key)); } catch { return null; }
    },
    set(key, value) {
      try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode: no memory */ }
    },
  };

  // --- Inside Telegram (opened from the bot): use Telegram's colors, take the full height. ---
  const hash = new URLSearchParams(location.hash.slice(1));
  if (hash.has("tgWebAppVersion") || hash.has("tgWebAppData") || hash.has("tgWebAppThemeParams")) {
    root.classList.add("in-telegram");
    try {
      const theme = JSON.parse(hash.get("tgWebAppThemeParams") || "{}");
      const vars = {
        "--bg": theme.bg_color,
        "--surface": theme.secondary_bg_color || theme.bg_color,
        "--text": theme.text_color,
        "--muted": theme.hint_color,
        "--accent": theme.button_color || theme.link_color,
        "--accent-text": theme.button_text_color,
      };
      for (const [name, value] of Object.entries(vars)) {
        if (/^#[0-9a-f]{3,8}$/i.test(value || "")) root.style.setProperty(name, value);
      }
    } catch { /* keep our own colors */ }
    const post = (type) => {
      const data = JSON.stringify({ eventType: type, eventData: {} });
      if (window.TelegramWebviewProxy) window.TelegramWebviewProxy.postEvent(type, "{}");
      else if (window.external && "notify" in window.external) window.external.notify(data);
      else if (window.parent !== window) window.parent.postMessage(data, "https://web.telegram.org");
    };
    post("web_app_ready");
    post("web_app_expand");
  }

  const article = document.querySelector("[data-article]");
  if (!article) return;

  // --- Text size ---
  let scale = Number(store.get("cw:scale")) || 0;
  const applyScale = () => root.style.setProperty("--reading-scale", String(1 + scale * 0.08));
  applyScale();
  article.querySelectorAll("[data-scale]").forEach((button) => {
    button.addEventListener("click", () => {
      scale = Math.max(-2, Math.min(4, scale + Number(button.dataset.scale)));
      store.set("cw:scale", scale);
      applyScale();
    });
  });

  // --- "Got it" checks with progress, remembered per article ---
  const ideas = [...article.querySelectorAll(".idea")];
  const key = `cw:got:${article.dataset.article}`;
  const done = new Set(store.get(key) || []);
  const counter = article.querySelector(".got-count");
  const render = () => {
    ideas.forEach((idea, n) => {
      const on = done.has(n);
      idea.classList.toggle("done", on);
      const button = idea.querySelector(".got-it");
      button.setAttribute("aria-pressed", String(on));
      button.textContent = on ? "✓ Got it" : "Got it";
    });
    if (counter) counter.textContent = `${done.size}/${ideas.length} got it`;
  };
  ideas.forEach((idea, n) => {
    idea.querySelector(".got-it").addEventListener("click", () => {
      done.has(n) ? done.delete(n) : done.add(n);
      store.set(key, [...done]);
      render();
      if (article.classList.contains("focus") && done.has(n)) step(1);
    });
  });
  render();

  // --- Focus mode: one idea at a time ---
  let current = 0;
  const stepLabel = article.querySelector(".focus-step");
  const focusButton = article.querySelector("[data-focus]");
  const show = () => {
    ideas.forEach((idea, n) => idea.classList.toggle("current", n === current));
    if (stepLabel) stepLabel.textContent = `${current + 1} / ${ideas.length}`;
    article.querySelector("[data-focus-prev]").disabled = current === 0;
    article.querySelector("[data-focus-next]").textContent = current === ideas.length - 1 ? "Finish ✓" : "Next →";
  };
  const setFocus = (on) => {
    article.classList.toggle("focus", on);
    focusButton.setAttribute("aria-pressed", String(on));
    focusButton.textContent = on ? "Show everything" : "🎯 One idea at a time";
    if (on) { show(); ideas[current].scrollIntoView({ block: "center", behavior: "smooth" }); }
  };
  const step = (delta) => {
    const next = current + delta;
    if (next >= ideas.length) {
      setFocus(false);
      (article.querySelector(".takeaway") || article).scrollIntoView({ block: "center", behavior: "smooth" });
      return;
    }
    current = Math.max(0, next);
    show();
  };
  if (focusButton && ideas.length) {
    focusButton.addEventListener("click", () => setFocus(!article.classList.contains("focus")));
    article.querySelector("[data-focus-prev]").addEventListener("click", () => step(-1));
    article.querySelector("[data-focus-next]").addEventListener("click", () => step(1));
    document.addEventListener("keydown", (event) => {
      if (!article.classList.contains("focus") || event.target.closest("input, textarea")) return;
      if (event.key === "ArrowRight") step(1);
      else if (event.key === "ArrowLeft") step(-1);
      else if (event.key === "Escape") setFocus(false);
    });
  }
})();
