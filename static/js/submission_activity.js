(() => {
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  document.querySelectorAll("[data-submission-activity]").forEach((viewport) => {
    const items = Array.from(viewport.querySelectorAll("[data-submission-activity-item]"));
    if (items.length < 2) return;

    let current = 0;
    let timer = null;

    const showNext = () => {
      const previous = items[current];
      const nextIndex = (current + 1) % items.length;
      const next = items[nextIndex];
      current = nextIndex;

      if (reducedMotion) {
        previous.classList.remove("is-active");
        previous.hidden = true;
        next.hidden = false;
        next.classList.add("is-active");
        return;
      }

      previous.classList.remove("is-active");
      window.setTimeout(() => {
        previous.hidden = true;
        next.hidden = false;
        window.requestAnimationFrame(() => next.classList.add("is-active"));
      }, 350);
    };

    const start = () => {
      if (!timer && !document.hidden) timer = window.setInterval(showNext, 4500);
    };

    const stop = () => {
      if (timer) {
        window.clearInterval(timer);
        timer = null;
      }
    };

    document.addEventListener("visibilitychange", () => {
      if (document.hidden) stop();
      else start();
    });
    start();
  });
})();
