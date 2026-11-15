(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const body = document.body;
  const csrf = body.dataset.csrf;
  const isAuth = !!body.dataset.auth;

  // ---------- всплывающее сообщение ----------
  let toastTimer;
  function toast(text) {
    const el = $("#toast");
    if (!el) return;
    el.textContent = text;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 2300);
  }

  // ---------- тёмная и светлая тема ----------
  function syncThemeColor() {
    const meta = $("#theme-color");
    if (meta) meta.content = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
  }
  syncThemeColor();
  $("#theme-toggle")?.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch (e) { /* не критично */ }
    syncThemeColor();
  });

  // ---------- меню аватара ----------
  const menuBtn = $("#menu-btn");
  const menu = $("#user-menu");
  function setMenu(open) {
    menu.hidden = !open;
    menuBtn.setAttribute("aria-expanded", String(open));
  }
  if (menuBtn && menu) {
    menuBtn.addEventListener("click", (e) => { e.stopPropagation(); setMenu(menu.hidden); });
    document.addEventListener("click", (e) => {
      if (!menu.hidden && !menu.contains(e.target)) setMenu(false);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !menu.hidden) { setMenu(false); menuBtn.focus(); }
    });
  }

  // ---------- реакции и «поделиться» ----------
  document.addEventListener("click", async (e) => {
    const react = e.target.closest(".react");
    if (react) {
      if (!isAuth) { location.href = "/login"; return; }
      const card = react.closest(".poem");
      react.disabled = true;
      try {
        const r = await fetch(`/poem/${card.dataset.id}/react`, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF": csrf },
          body: JSON.stringify({ kind: react.dataset.kind }),
        });
        if (r.status === 401) { location.href = "/login"; return; }
        if (r.status === 429) { toast("Слишком часто. Подожди минуту."); return; }
        if (!r.ok) throw new Error(r.status);
        const d = await r.json();
        card.querySelectorAll(".react").forEach((b) => {
          const k = b.dataset.kind;
          const on = d.mine === k;
          b.classList.toggle("on", on);
          b.setAttribute("aria-pressed", String(on));
          b.querySelector("b").textContent = d.counts[k] || "";
        });
        if (d.mine === react.dataset.kind) {
          react.classList.remove("pop");
          void react.offsetWidth; // перезапуск анимации
          react.classList.add("pop");
        }
      } catch (err) {
        toast("Не получилось поставить реакцию. Попробуй ещё раз.");
      } finally {
        react.disabled = false;
      }
      return;
    }

    const share = e.target.closest("[data-share]");
    if (share) {
      const url = location.origin + share.dataset.path;
      if (navigator.share) {
        try { await navigator.share({ title: share.dataset.title, url }); } catch (err) { /* закрыл окно */ }
        return;
      }
      try {
        await navigator.clipboard.writeText(url);
        toast("Ссылка скопирована");
      } catch (err) {
        window.prompt("Скопируй ссылку", url);
      }
      return;
    }

    const more = e.target.closest("[data-more]");
    if (more) {
      e.preventDefault();
      loadMore(more);
    }
  });

  // ---------- подгрузка ленты ----------
  async function loadMore(btn) {
    if (btn.dataset.busy) return;
    btn.dataset.busy = "1";
    const label = btn.textContent;
    btn.textContent = "Загружаю…";
    try {
      const url = new URL(btn.href, location.href);
      url.searchParams.set("partial", "1");
      const r = await fetch(url);
      if (!r.ok) throw new Error(r.status);
      btn.insertAdjacentHTML("beforebegin", await r.text());
      btn.remove();
      watchMore();
    } catch (err) {
      btn.textContent = label;
      delete btn.dataset.busy;
      toast("Не получилось загрузить. Нажми ещё раз.");
    }
  }
  const io = "IntersectionObserver" in window
    ? new IntersectionObserver((entries) => {
        entries.forEach((en) => { if (en.isIntersecting) loadMore(en.target); });
      }, { rootMargin: "500px" })
    : null;
  function watchMore() {
    if (!io) return;
    document.querySelectorAll("[data-more]:not([data-watched])").forEach((b) => {
      b.dataset.watched = "1";
      io.observe(b);
    });
  }
  watchMore();

  // ---------- счётчик символов в формах ----------
  document.querySelectorAll("textarea[data-count]").forEach((area) => {
    const out = document.getElementById(area.dataset.count);
    if (!out) return;
    const update = () => { out.textContent = `${area.value.length} / ${area.maxLength}`; };
    area.addEventListener("input", update);
    update();
  });

  // ---------- число непрочитанных уведомлений ----------
  // Подгружается после показа страницы, чтобы не замедлять её открытие.
  const badge = $("#notif-badge");
  if (badge) {
    fetch("/notifications/count", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d && d.count > 0) {
          badge.textContent = d.count > 99 ? "99+" : d.count;
          badge.hidden = false;
        }
      })
      .catch(() => {});
  }
})();
