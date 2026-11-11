document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".react");
  if (!btn) return;
  if (!document.body.dataset.auth) {
    location.href = "/login";
    return;
  }
  const card = btn.closest(".poem");
  btn.disabled = true;
  try {
    const r = await fetch(`/poem/${card.dataset.id}/react`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF": document.body.dataset.csrf,
      },
      body: JSON.stringify({ kind: btn.dataset.kind }),
    });
    if (r.status === 401) { location.href = "/login"; return; }
    if (r.status === 429) { alert("Слишком часто. Подожди минуту."); return; }
    if (!r.ok) throw new Error(r.status);
    const d = await r.json();
    card.querySelectorAll(".react").forEach((b) => {
      const k = b.dataset.kind;
      const on = d.mine === k;
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", on);
      b.querySelector("b").textContent = d.counts[k] || "";
    });
  } catch (err) {
    alert("Не получилось поставить реакцию. Попробуй ещё раз.");
  } finally {
    btn.disabled = false;
  }
});

// Счётчик непрочитанных уведомлений подгружается после показа страницы,
// чтобы не замедлять её открытие.
(async () => {
  const badge = document.getElementById("notif-badge");
  if (!badge) return;
  try {
    const r = await fetch("/notifications/count", { cache: "no-store" });
    if (!r.ok) return;
    const { count } = await r.json();
    if (count > 0) {
      badge.textContent = count > 99 ? "99+" : count;
      badge.hidden = false;
    }
  } catch (err) { /* не критично */ }
})();
