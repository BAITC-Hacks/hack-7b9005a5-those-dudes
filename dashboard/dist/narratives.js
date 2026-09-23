(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.HackAlemNarratives = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";
  const number = value => Math.max(0, Number.isFinite(Number(value)) ? Number(value) : 0);
  const integer = value => Math.floor(number(value)).toLocaleString("ru-RU");
  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  const active = status => status === "running" || status === "queued" || status === "cancelling";

  function textValue(value) {
    if (value == null || value === "") return "";
    if (typeof value === "string") return value;
    if (Array.isArray(value)) return value.map(textValue).filter(Boolean).join("\n");
    if (typeof value === "object") return Object.entries(value).map(([key, item]) => `${key}: ${textValue(item)}`).join("; ");
    return String(value);
  }

  // Treat every model field as plain text, including nested evidence and JSON.
  function structuredHtml(result) {
    if (!result) return "";
    const sections = [
      ["Почему назначена эта роль", result.explanation || result.role_explanation],
      ["Числовые основания", result.evidence],
      ["Почему этот приоритет", result.priority_explanation || result.why],
      ["Сравнение с альтернативной ролью", result.alternative_explanation],
      ["Ограничения гипотезы", result.limitations],
      ["Следующий шаг аналитика", result.analyst_next_step]
    ].filter(([, value]) => textValue(value));
    let html = sections.map(([title, value]) => `<section class="explanation-section"><h4>${escape(title)}</h4><p>${escape(textValue(value))}</p></section>`).join("");
    if (result.evidence_json) {
      let value = result.evidence_json;
      if (typeof value === "string") { try { value = JSON.parse(value); } catch (_) { /* Keep malformed server text inert. */ } }
      const pretty = typeof value === "string" ? value : JSON.stringify(value, null, 2);
      html += `<details class="explanation-json"><summary>Структурированные основания (JSON)</summary><pre>${escape(pretty)}</pre></details>`;
    }
    return html;
  }

  function viewModel(payload, fallbackTotal) {
    const total = number(payload.total);
    const completed = number(payload.completed);
    const failed = number(payload.failed);
    const pending = payload.pending == null ? Math.max(0, total - completed - failed) : number(payload.pending);
    const available = number(payload.available_nodes ?? fallbackTotal);
    const generated = number(payload.generated_count ?? completed);
    const running = active(payload.status);
    const labels = { idle: "Настроить", running: "Генерация…", queued: "В очереди", cancelling: "Останавливаем…", complete: "Завершено", partial: "Частично готово", cancelled: "Остановлено", failed: "Нужна проверка" };
    return {
      running,
      total, completed, failed, pending, available, generated,
      badge: payload.status === "complete" && generated < available ? "Выбранные узлы готовы" : labels[payload.status] || "Проверить статус",
      coverage: `API: ${integer(generated)} из ${integer(available)} узлов`,
      counts: total ? `В выбранной задаче: готово ${integer(completed)} / ${integer(total)} · ошибок ${integer(failed)} · осталось ${integer(pending)}` : `Готовых API-объяснений: ${integer(generated)} / ${integer(available)}`,
      warning: payload.status === "failed" || payload.status === "partial" || failed > 0,
      button: payload.can_resume ? "Продолжить через API" : "Сгенерировать через API"
    };
  }

  function init(options = {}) {
    const win = options.window || window;
    const doc = options.document || document;
    const fetcher = options.fetch || win.fetch.bind(win);
    const timers = options.timers || win;
    const panel = doc.querySelector("#csvExplanationPanel");
    if (!panel || panel.dataset.initialized) return null;
    panel.dataset.initialized = "true";
    const el = id => doc.querySelector(`#csvExplanation${id}`);
    const context = options.context || win.HACKALEM_CONTEXT;
    const apiUrl = path => context?.apiUrl(path) || path;
    const totalNodes = options.totalNodes ?? win.HACKALEM_DATA?.meta?.n_nodes ?? 0;
    const elements = Object.fromEntries(["Coverage", "Badge", "Scope", "Start", "Cancel", "Refresh", "Consent", "Progress", "Counts", "Status", "Usage"].map(name => [name, el(name)]));
    let payload = null, busy = false, timer = null, stopped = false, failures = 0, actionError = "";

    function controls() {
      const running = active(payload?.status);
      elements.Start.disabled = busy || running || !elements.Consent.checked || !payload || payload.ready === false;
      elements.Scope.disabled = busy || running;
      elements.Consent.disabled = busy || running;
      elements.Cancel.disabled = busy || !running || payload?.status === "cancelling";
      elements.Cancel.classList.toggle("hidden", !running);
      elements.Refresh.disabled = busy;
    }

    function render(next) {
      payload = next;
      const view = viewModel(payload, totalNodes);
      if (view.running) actionError = ""; // A read confirms that a previously uncertain start was accepted.
      panel.classList.toggle("explanation-busy", view.running);
      panel.classList.toggle("explanation-warning", view.warning || Boolean(actionError) || payload.ready === false);
      elements.Coverage.textContent = view.coverage;
      elements.Badge.textContent = actionError ? "Запуск не подтверждён" : payload.ready === false && !view.running ? "API не готов" : view.badge;
      elements.Counts.textContent = view.counts;
      elements.Start.textContent = view.button;
      elements.Progress.max = Math.max(view.total || view.available, 1);
      elements.Progress.value = Math.min(view.total ? view.completed : view.generated, elements.Progress.max);
      const defaultMessages = {
        idle: "Пакетная генерация ещё не запускалась. Числовые основания доступны в CSV; API-объяснения появляются после генерации.",
        complete: "Объяснения выбранных узлов сохранены. Скачайте CSV заново, чтобы получить обновлённые столбцы.",
        cancelled: "Генерация остановлена. Уже готовые ответы сохранены и доступны в CSV.",
        partial: "Готовые объяснения сохранены. Для остальных строк проверьте ошибку и продолжите генерацию.",
        failed: "Генерация не завершена. Проверьте API-ключ, баланс и соединение; готовые ответы не потеряны.",
        running: "Объяснения сохраняются по мере готовности. Можно закрыть страницу — задача продолжится, пока работает локальный сервер."
      };
      elements.Status.textContent = actionError || (payload.ready === false && !view.running
        ? payload.configured === false ? "Добавьте OPENAI_API_KEY в .env. API-объяснения ещё не готовы; в CSV источник указан явно." : "Подключение API недоступно. Проверьте зависимости и настройки проекта; готовые объяснения сохранены."
        : typeof payload.message === "string" && payload.message ? payload.message : defaultMessages[payload.status] || "Проверяем состояние генерации…");
      const usage = payload.usage || {};
      const input = usage.input_tokens ?? usage.prompt_tokens;
      const output = usage.output_tokens ?? usage.completion_tokens;
      const parts = [payload.model ? `Модель: ${payload.model}` : "", input != null ? `Входные токены: ${integer(input)}` : "", output != null ? `Выходные токены: ${integer(output)}` : "", usage.total_tokens != null && input == null && output == null ? `Токены: ${integer(usage.total_tokens)}` : ""];
      elements.Usage.textContent = parts.filter(Boolean).join(" · ");
      if (view.running && payload.scope) elements.Scope.value = payload.scope;
      controls();
      return view;
    }

    async function request(path, method = "GET", body) {
      const response = await fetcher(apiUrl(path), {
        method, cache: "no-store",
        headers: method === "GET" ? { Accept: "application/json" } : { "Content-Type": "application/json", "X-HackAlem-Action": "explain-batch" },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        ...(typeof AbortSignal !== "undefined" && AbortSignal.timeout ? { signal: AbortSignal.timeout(20_000) } : {})
      });
      let data;
      try { data = await response.json(); } catch (_) { throw new Error("Сервер не вернул статус. Перезапустите проект и обновите страницу."); }
      if (!response.ok) throw new Error(data.message || data.error || `Запрос не выполнен (HTTP ${response.status}).`);
      return data;
    }

    function schedule() {
      if (timer) timers.clearTimeout(timer);
      timer = null;
      if (!stopped && active(payload?.status)) timer = timers.setTimeout(refresh, Math.min(15_000, failures ? 5000 * failures : 2500));
    }

    async function refresh() {
      if (busy || stopped) return;
      busy = true; controls();
      try {
        render(await request("/api/explanations/status"));
        failures = 0;
      } catch (error) {
        failures += 1;
        elements.Badge.textContent = "Связь прервана";
        elements.Status.textContent = `${error.message} Запуск повторно не отправляется; нажмите «Обновить статус».`;
        panel.classList.add("explanation-warning");
      } finally {
        busy = false; controls(); schedule();
      }
    }

    async function start() {
      if (busy || active(payload?.status) || !elements.Consent.checked || !payload || payload.ready === false) return;
      busy = true; controls();
      actionError = "";
      try {
        const next = await request("/api/explanations/start", "POST", { scope: elements.Scope.value === "top" ? "top" : "all", consent: true });
        render(next);
        elements.Consent.checked = false;
        failures = 0;
      } catch (error) {
        actionError = error.message;
        elements.Status.textContent = error.message;
        elements.Badge.textContent = "Запуск не подтверждён";
        panel.classList.add("explanation-warning");
      } finally {
        busy = false; controls();
        // A lost response may hide an accepted job. Only read status; never retry POST automatically.
        await refresh();
      }
    }

    async function cancel() {
      if (busy || !active(payload?.status)) return;
      busy = true; controls();
      try { render(await request("/api/explanations/cancel", "POST", {})); }
      catch (error) { elements.Status.textContent = error.message; panel.classList.add("explanation-warning"); }
      finally { busy = false; controls(); schedule(); }
    }

    elements.Consent.addEventListener("change", controls);
    elements.Start.addEventListener("click", start);
    elements.Cancel.addEventListener("click", cancel);
    elements.Refresh.addEventListener("click", refresh);
    win.addEventListener("hackalem:explanation-saved", refresh);
    win.addEventListener("pagehide", () => { stopped = true; if (timer) timers.clearTimeout(timer); });
    win.addEventListener("pageshow", () => { if (stopped) { stopped = false; refresh(); } });
    refresh(); // Local status only: this must never trigger generation.
    return { refresh, start, cancel, state: () => payload };
  }

  return { init, structuredHtml, viewModel };
});
