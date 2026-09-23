(() => {
  "use strict";
  const $ = selector => document.querySelector(selector);
  const fields = ["nodes", "edges", "transactions"];
  const MAX_FILE_SIZE = 20 * 1024 * 1024;
  const form = $("#uploadForm");
  const submit = $("#uploadSubmit");
  const errorBox = $("#uploadError");
  const progress = $("#uploadProgress");
  const resume = $("#resumeStatus");
  let busy = false;
  let currentJob = null;
  let clock = null;
  let startedAt = 0;

  const fmtInt = number => Number(number).toLocaleString("ru-RU");
  const selectedFiles = () => fields.map(name => ({ name, file: $(`#${name}File`).files[0] }));
  const isRunId = value => typeof value === "string" && /^(?:default|[a-f0-9]{32})$/.test(value);
  const dashboardUrl = runId => `/dashboard?run=${encodeURIComponent(runId)}`;
  const messageFrom = (payload, fallback) => typeof payload?.message === "string" ? payload.message : (typeof payload?.error === "string" ? payload.error : fallback);

  async function readResponse(response) {
    let payload;
    try { payload = await response.json(); } catch { throw new Error("Сервер вернул неожиданный ответ. Проверьте, что проект запущен, и попробуйте ещё раз."); }
    if (!response.ok) {
      const error = new Error(messageFrom(payload, `Запрос не выполнен (HTTP ${response.status}).`));
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function showError(message) {
    errorBox.textContent = message;
    errorBox.classList.remove("hidden");
    errorBox.focus();
  }

  function updateFiles() {
    let count = 0;
    let total = 0;
    for (const { name, file } of selectedFiles()) {
      $(`#${name}Selection`).textContent = file ? `${file.name} · ${(file.size / 1024 / 1024).toLocaleString("ru-RU", { maximumFractionDigits: 2 })} МБ` : "Файл не выбран";
      $(`#${name}File`).closest(".file-card").classList.toggle("has-file", Boolean(file));
      if (file) { count++; total += file.size; }
    }
    $("#fileSummary").textContent = count === 3 ? `Готово к загрузке: 3 файла · ${(total / 1024 / 1024).toLocaleString("ru-RU", { maximumFractionDigits: 2 })} МБ` : `Выбрано ${count} из 3 файлов`;
    submit.disabled = busy || Boolean(currentJob) || count !== 3;
  }

  function setBusy(value) {
    busy = value;
    $("#uploadFields").disabled = value || Boolean(currentJob);
    submit.textContent = value ? "Выполняется анализ…" : "Загрузить и открыть анализ ↗";
    progress.classList.toggle("paused", !value);
    $("#progressNote").classList.toggle("hidden", !value);
    updateFiles();
    if (clock) clearInterval(clock);
    if (value) clock = setInterval(() => {
      $("#progressElapsed").textContent = `${fmtInt(Math.floor((Date.now() - startedAt) / 1000))} сек.`;
    }, 1000);
  }

  async function pollJob() {
    let failedRequests = 0;
    const deadline = Date.now() + 6 * 60 * 1000;
    while (Date.now() < deadline) {
      let payload;
      try {
        const response = await fetch(`/api/datasets/${currentJob}`, { cache: "no-store", signal: AbortSignal.timeout(15_000) });
        payload = await readResponse(response);
        failedRequests = 0;
      } catch (error) {
        failedRequests++;
        if (failedRequests >= 3) throw new Error(`Не удалось проверить готовность анализа: ${error.message} Нажмите «Проверить готовность»; повторно загружать файлы не нужно.`);
        $("#progressMessage").textContent = "Восстанавливаем связь с локальным сервером…";
        await new Promise(resolve => setTimeout(resolve, 1500));
        continue;
      }
      if (payload.status === "ready") {
        $("#progressTitle").textContent = "Анализ готов";
        $("#progressMessage").textContent = "Открываем граф, роли, приоритеты и файлы для экспорта…";
        setBusy(false);
        window.location.assign(dashboardUrl(currentJob));
        return;
      }
      if (payload.status === "failed") {
        currentJob = null;
        throw new Error(typeof payload.error === "string" ? payload.error : messageFrom(payload, "Анализ не завершён. Проверьте формат и согласованность трёх файлов."));
      }
      if (!["queued", "running"].includes(payload.status)) throw new Error("Неизвестный статус анализа. Попробуйте проверить готовность ещё раз.");
      $("#progressTitle").textContent = payload.status === "queued" ? "Анализ в очереди" : "Проверяем и анализируем сеть";
      $("#progressMessage").textContent = messageFrom(payload, "Вычисляем признаки, роли, кластеры и приоритеты. Это может занять несколько минут.");
      await new Promise(resolve => setTimeout(resolve, 1200));
    }
    throw new Error("Ожидание затянулось. Анализ может продолжаться на сервере. Нажмите «Проверить готовность», чтобы получить его результат.");
  }

  async function monitor() {
    errorBox.classList.add("hidden");
    resume.classList.add("hidden");
    $("#progressTitle").textContent = "Проверяем готовность анализа";
    $("#progressMessage").textContent = "Запрашиваем состояние расчёта на локальном сервере…";
    setBusy(true);
    try { await pollJob(); }
    catch (error) {
      $("#progressTitle").textContent = currentJob ? "Ожидание приостановлено" : "Проверьте исходные файлы";
      $("#progressMessage").textContent = currentJob ? "Связь с сервером прервана. Нажмите «Проверить готовность»; повторно загружать файлы не нужно." : "Анализ не завершён. Проверьте причину ошибки, исправьте выбранные файлы и повторите загрузку.";
      showError(error.message);
      resume.classList.toggle("hidden", !currentJob);
      setBusy(false);
    }
  }

  fields.forEach(name => $(`#${name}File`).addEventListener("change", () => {
    errorBox.classList.add("hidden");
    updateFiles();
  }));
  resume.addEventListener("click", monitor);
  window.addEventListener("beforeunload", event => { if (busy) { event.preventDefault(); event.returnValue = ""; } });

  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (busy || currentJob || !form.reportValidity()) return;
    const files = selectedFiles();
    for (const { file } of files) {
      if (!file || !/\.parquet$/i.test(file.name)) { showError("Для каждой таблицы выберите файл с расширением .parquet."); return; }
      if (!file.size) { showError(`Файл «${file.name}» пуст. Выберите непустой Parquet-файл.`); return; }
      if (file.size > MAX_FILE_SIZE) { showError(`Файл «${file.name}» превышает ограничение 20 МБ.`); return; }
    }
    errorBox.classList.add("hidden");
    progress.classList.remove("hidden");
    $("#progressTitle").textContent = "Загружаем исходные файлы";
    $("#progressMessage").textContent = "Файлы отправляются на локальный сервер…";
    startedAt = Date.now();
    $("#progressElapsed").textContent = "0 сек.";
    setBusy(true);
    const data = new FormData();
    files.forEach(({ name, file }) => data.append(name, file));
    try {
      const response = await fetch("/api/datasets", { method: "POST", headers: { "X-HackAlem-Action": "upload" }, body: data, signal: AbortSignal.timeout(90_000) });
      const payload = await readResponse(response);
      if (!isRunId(payload.run_id) || payload.run_id === "default") throw new Error("Сервер не вернул идентификатор анализа. Попробуйте ещё раз.");
      currentJob = payload.run_id;
      await monitor();
    } catch (error) {
      const rejected = error.status >= 400 && error.status < 500;
      $("#progressTitle").textContent = rejected ? "Файлы не приняты" : "Загрузка не подтверждена";
      $("#progressMessage").textContent = rejected ? "Исправьте выбранные файлы и повторите загрузку." : "Проверьте доступность локального сервера перед повторной попыткой.";
      showError(error.name === "TimeoutError" ? "Сервер не подтвердил загрузку за 90 секунд. Проверьте его доступность перед повторной попыткой." : error.message);
      setBusy(false);
    }
  });

  async function loadCurrent() {
    try {
      const payload = await readResponse(await fetch("/api/datasets/current", { cache: "no-store", signal: AbortSignal.timeout(15_000) }));
      if (payload.status !== "ready" || !isRunId(payload.run_id)) throw new Error("Исходный анализ пока не готов. Загрузите файлы выше.");
      const counts = payload.counts || {};
      $("#currentDatasetSummary").textContent = Number.isFinite(counts.nodes) ? `${fmtInt(counts.nodes)} узлов · ${fmtInt(counts.edges)} связей · ${fmtInt(counts.transactions)} транзакций. Без повторного расчёта.` : "Сохранённый анализ готов к просмотру. Без повторного расчёта.";
      $("#openCurrent").href = dashboardUrl(payload.run_id);
      $("#openCurrent").classList.remove("hidden");
    } catch (error) { $("#currentDatasetSummary").textContent = error.message; }
  }
  loadCurrent();
})();
