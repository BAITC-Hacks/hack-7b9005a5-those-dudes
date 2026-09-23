(() => {
  "use strict";
  const runId = new URLSearchParams(window.location.search).get("run") || "default";
  const message = document.querySelector("#dashboardLoadMessage");
  const title = document.querySelector("#dashboardLoadTitle");
  const loader = document.querySelector("#dashboardLoader");

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = src;
      script.onload = resolve;
      script.onerror = () => reject(new Error("Не удалось загрузить данные или интерфейс. Проверьте, что локальный сервер работает."));
      document.head.appendChild(script);
    });
  }

  async function start() {
    try {
      if (!/^(?:default|[a-f0-9]{32})$/.test(runId)) throw new Error("Некорректная ссылка на анализ. Вернитесь к загрузке файлов.");
      const base = `/api/datasets/${encodeURIComponent(runId)}`;
      const response = await fetch(base, { cache: "no-store", signal: AbortSignal.timeout(20_000) });
      const status = await response.json();
      if (!response.ok || status.status !== "ready") throw new Error(typeof status.message === "string" ? status.message : "Этот анализ ещё не готов или больше недоступен. Вернитесь к загрузке файлов.");
      window.HACKALEM_CONTEXT = Object.freeze({
        runId,
        apiUrl: path => `${path}?run=${encodeURIComponent(runId)}`,
        exportUrl: name => `${base}/exports/${encodeURIComponent(name)}`
      });
      document.querySelectorAll("[data-export]").forEach(link => {
        link.href = window.HACKALEM_CONTEXT.exportUrl(link.dataset.export) + (link.dataset.mode === "contest" ? "?mode=contest" : "");
        link.removeAttribute("aria-disabled");
      });
      document.querySelector("#datasetLabel").textContent = runId === "default" ? "Исходный набор" : `Загруженный набор · ${runId.slice(0, 8)}`;
      const calculationNotice = document.querySelector("#calculationNotice");
      calculationNotice.textContent = typeof status.calculation_notice === "string" ? status.calculation_notice : "";
      calculationNotice.classList.toggle("hidden", !calculationNotice.textContent);
      message.textContent = "Загружаем граф, роли и проверенные результаты…";
      await loadScript(`${base}/dashboard_data.js`);
      if (!window.HACKALEM_DATA) throw new Error("Данные анализа не найдены. Вернитесь к загрузке файлов.");
      await loadScript("/app.js");
      if (!window.HACKALEM_READY) throw new Error("Интерфейс не смог обработать данные анализа. Вернитесь к загрузке файлов или обновите страницу.");
      document.body.classList.remove("dashboard-loading");
      loader.classList.add("hidden");
    } catch (error) {
      title.textContent = "Не удалось открыть анализ";
      message.textContent = error.message;
      loader.classList.add("load-failed");
      loader.setAttribute("role", "alert");
    }
  }
  start();
})();
