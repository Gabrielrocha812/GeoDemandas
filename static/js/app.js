document.addEventListener("DOMContentLoaded", () => {
  document.documentElement.classList.add("ui-ready");

  const counters = document.querySelectorAll("[data-counter]");
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  counters.forEach((node) => {
    const target = Number(node.dataset.counter || 0);
    if (reduceMotion || target === 0) {
      node.textContent = target.toLocaleString("pt-BR");
      return;
    }
    const start = performance.now();
    const duration = 850;
    const tick = (now) => {
      const progress = Math.min((now - start) / duration, 1);
      const eased = 1 - Math.pow(1 - progress, 4);
      node.textContent = Math.round(target * eased).toLocaleString("pt-BR");
      if (progress < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });

  document.querySelectorAll("form[data-loading-form]").forEach((form) => {
    form.addEventListener("submit", () => {
      const button = form.querySelector("button[type='submit']");
      if (!button) return;
      button.disabled = true;
      button.classList.add("opacity-70", "cursor-wait");
      const label = button.querySelector("[data-button-label]");
      if (label) label.textContent = button.dataset.loadingText || "Processando...";
    });
  });
});
