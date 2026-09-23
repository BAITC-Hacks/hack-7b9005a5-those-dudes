(() => {
  "use strict";

  const D = window.HACKALEM_DATA;
  const P = D?.production || { available: false, files: {}, top_nodes: [], resilience: [], warnings: [] };
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const apiUrl = path => window.HACKALEM_CONTEXT?.apiUrl(path) || path;

  if (!D) {
    const banner = $("#errorBanner");
    banner.textContent = "Файл dashboard_data.js не найден. Запустите build_dashboard.py и обновите страницу.";
    banner.classList.remove("hidden");
    return;
  }

  const COLORS = {
    depth: ["#ffe373", "#3cffcd", "#70d7ff", "#d7acff", "#ff91c4"],
    role: {
      consolidator: "#3cffcd",
      transit: "#70d7ff",
      distributor: "#ffe373",
      terminal: "#ff91c4",
      coordinator: "#d7acff",
      peripheral: "#b9cfe5",
      unknown: "#b9cfe5"
    },
    cluster: ["#3cffcd", "#ffe373", "#70d7ff", "#d7acff", "#ff91c4", "#80ed99", "#ff93e0", "#86dcff", "#ffe875", "#bef264", "#ff9875", "#99b5ff"]
  };

  const ROLE_LABELS = {
    consolidator: "Сборщик",
    transit: "Транзит",
    distributor: "Распределитель",
    terminal: "Конечный получатель",
    coordinator: "Координатор",
    peripheral: "Без явной роли"
  };

  const nodeById = new Map(D.nodes.map(node => [node.gid, node]));
  const outgoingById = new Map();
  const incomingById = new Map();
  const txByNode = new Map();
  D.nodes.forEach(node => {
    outgoingById.set(node.gid, []);
    incomingById.set(node.gid, []);
    txByNode.set(node.gid, []);
  });
  D.edges.forEach(edge => {
    if (outgoingById.has(edge.src)) outgoingById.get(edge.src).push(edge);
    if (incomingById.has(edge.dst)) incomingById.get(edge.dst).push(edge);
  });
  D.transactions.forEach(tx => {
    if (txByNode.has(tx.src)) txByNode.get(tx.src).push({ ...tx, direction: "out" });
    if (txByNode.has(tx.dst)) txByNode.get(tx.dst).push({ ...tx, direction: "in" });
  });

  const escapeHtml = value => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

  const fmtInt = value => new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 }).format(Number(value || 0));
  const fmtMoney = value => {
    const amount = Number(value || 0);
    if (Math.abs(amount) >= 1e9) return `${(amount / 1e9).toLocaleString("ru-RU", { maximumFractionDigits: 2 })} млрд ₸`;
    if (Math.abs(amount) >= 1e6) return `${(amount / 1e6).toLocaleString("ru-RU", { maximumFractionDigits: 2 })} млн ₸`;
    if (Math.abs(amount) >= 1e3) return `${(amount / 1e3).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} тыс. ₸`;
    return `${fmtInt(amount)} ₸`;
  };
  const fmtScore = value => value == null || Number.isNaN(Number(value)) ? "—" : Number(value).toLocaleString("ru-RU", { maximumFractionDigits: 4 });
  const fmtPercent = value => value == null || Number.isNaN(Number(value)) ? "—" : `${(Number(value) * 100).toLocaleString("ru-RU", { maximumFractionDigits: 1 })}%`;
  const roleLabel = role => ROLE_LABELS[role] || role || "Не рассчитана";
  const shortGid = gid => String(gid).length > 13 ? `${String(gid).slice(0, 7)}…${String(gid).slice(-5)}` : String(gid);
  const dateLabel = iso => new Date(`${iso}T00:00:00Z`).toLocaleDateString("ru-RU", { day: "2-digit", month: "short" });
  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));

  function metricCard(label, value, note = "", accent = false) {
    return `<article class="metric-card${accent ? " accent" : ""}"><span class="metric-label">${escapeHtml(label)}</span><strong class="metric-value">${escapeHtml(value)}</strong>${note ? `<span class="metric-note">${escapeHtml(note)}</span>` : ""}</article>`;
  }

  function scoreTrack(value, color = "#3cffcd") {
    const score = clamp(Number(value || 0), 0, 1);
    return `<div class="score-track"><span><i style="width:${(score * 100).toFixed(1)}%;background:${color}"></i></span><strong>${escapeHtml(fmtScore(value))}</strong></div>`;
  }

  function emptyInline(message) {
    return `<div class="empty-inline">${escapeHtml(message)}</div>`;
  }

  function tableHtml(rows, columns, { clickable = false, rowData = null, maxRows = null } = {}) {
    const selectedRows = maxRows ? rows.slice(0, maxRows) : rows;
    const head = columns.map(column => `<th>${escapeHtml(column.label)}</th>`).join("");
    const body = selectedRows.map((row, index) => {
      const attrs = clickable && rowData ? rowData(row, index) : "";
      const cells = columns.map(column => {
        const raw = typeof column.value === "function" ? column.value(row) : row[column.value];
        const value = column.format ? column.format(raw, row) : raw;
        const className = typeof column.className === "function" ? column.className(raw, row) : (column.className || "");
        return `<td class="${className}">${column.html ? value : escapeHtml(value ?? "—")}</td>`;
      }).join("");
      return `<tr${clickable ? " class=\"clickable\"" : ""} ${attrs}>${cells}</tr>`;
    }).join("");
    return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
  }

  function setupNavigation() {
    const buttons = $$(".nav-item");
    const allowed = new Set(buttons.map(button => button.dataset.view));
    const activate = (requested, push = false) => {
      const name = allowed.has(requested) ? requested : "network";
      buttons.forEach(button => {
        const active = button.dataset.view === name;
        button.classList.toggle("active", active);
        if (active) button.setAttribute("aria-current", "page");
        else button.removeAttribute("aria-current");
      });
      $$(".view").forEach(view => view.classList.toggle("active", view.id === `view-${name}`));
      document.body.dataset.view = name;
      const label = buttons.find(button => button.dataset.view === name).getAttribute("aria-label");
      document.title = `${label} — Граф денег`;
      if (push && window.location.hash !== `#${name}`) {
        // A section change never drops the dataset selector in the query string.
        window.history.pushState(null, "", `${window.location.pathname}${window.location.search}#${name}`);
      }
      if (name === "network") network.resize();
    };
    buttons.forEach(button => button.addEventListener("click", () => activate(button.dataset.view, true)));
    const restore = () => activate(window.location.hash.slice(1));
    window.addEventListener("popstate", restore);
    window.addEventListener("hashchange", restore);
    restore();
  }

  function renderHeader() {
    $("#periodLabel").textContent = `${D.meta.period_start} — ${D.meta.period_end} · данные локальны`;
    $("#dailyPeriodLabel").textContent = `${D.meta.period_start} — ${D.meta.period_end}`;
    $("#transactionTotalLabel").textContent = `${fmtInt(D.transactions.length)} СОБЫТИЙ`;
    $("#censoredMethodText").textContent = `${fmtInt(D.meta.n_censored)} узлов имеют depth = 4 без наблюдаемого выхода. На границе обхода нулевой выход нельзя считать доказательством «оседания» денег.`;
  }

  function renderMetrics() {
    const networkMetrics = [
      ["Узлы", fmtInt(D.meta.n_nodes), "включая изоляты", true],
      ["Рёбра", fmtInt(D.meta.n_edges), "уникальные пары"],
      ["Транзакции", fmtInt(D.meta.n_transactions), "отдельные события"],
      ["Оборот", fmtMoney(D.meta.turnover_kzt), "наблюдаемый"],
      ["Seed", fmtInt(D.meta.n_seed), `${D.meta.n_isolated} изолированы`],
      ["Depth-4", fmtInt(D.meta.n_censored), "выход цензурирован"]
    ];
    $("#networkMetrics").innerHTML = networkMetrics.map(item => metricCard(...item)).join("");

    const overviewMetrics = [
      ["Крупнейшая компонента", fmtInt(D.meta.largest_component), `${((D.meta.largest_component / D.meta.n_nodes) * 100).toFixed(1)}% узлов`, true],
      ["Компоненты с рёбрами", fmtInt(D.meta.n_edge_components), `${D.meta.n_components} с изолятами`],
      ["Вход и выход", fmtInt(D.meta.n_both_flow), "кандидаты для flow-анализа"],
      [D.meta.has_production_clusters ? "Production-кластеры" : "Louvain-кластеры", fmtInt(D.meta.n_clusters), D.meta.has_production_clusters ? "backend output" : "исследовательский baseline"]
    ];
    $("#overviewMetrics").innerHTML = overviewMetrics.map(item => metricCard(...item)).join("");

    const clusterSizes = D.clusters.map(cluster => cluster.n_nodes).sort((a, b) => b - a);
    const multiSeed = D.clusters.filter(cluster => cluster.n_seed > 1).length;
    $("#clusterMetrics").innerHTML = [
      ["Кластеры", fmtInt(D.meta.n_clusters), D.meta.has_production_clusters ? "production" : "Louvain baseline", true],
      ["Крупнейший", fmtInt(clusterSizes[0] || 0), "узлов"],
      ["Multi-seed", fmtInt(multiSeed), "кластеров с >1 seed"],
      ["Компоненты", fmtInt(D.meta.n_components), `${D.meta.n_isolated} изолятов`]
    ].map(item => metricCard(...item)).join("");
  }

  function lineChart(container, rows, valueKey, secondaryKey = null) {
    const width = 860, height = 270, left = 50, right = 20, top = 18, bottom = 38;
    const innerW = width - left - right, innerH = height - top - bottom;
    const values = rows.map(row => Number(row[valueKey] || 0));
    const secondary = secondaryKey ? rows.map(row => Number(row[secondaryKey] || 0)) : [];
    const max = Math.max(...values, 1);
    const max2 = Math.max(...secondary, 1);
    const x = index => left + (rows.length <= 1 ? innerW / 2 : index * innerW / (rows.length - 1));
    const y = value => top + innerH - value / max * innerH;
    const y2 = value => top + innerH - value / max2 * innerH;
    const path = values.map((value, index) => `${index ? "L" : "M"}${x(index).toFixed(1)},${y(value).toFixed(1)}`).join(" ");
    const secondPath = secondaryKey ? secondary.map((value, index) => `${index ? "L" : "M"}${x(index).toFixed(1)},${y2(value).toFixed(1)}`).join(" ") : "";
    const area = `${path} L${x(rows.length - 1)},${top + innerH} L${x(0)},${top + innerH} Z`;
    const grid = [0, .25, .5, .75, 1].map(step => {
      const gy = top + innerH - step * innerH;
      return `<line class="grid-line" x1="${left}" x2="${left + innerW}" y1="${gy}" y2="${gy}"/><text x="${left - 8}" y="${gy + 4}" text-anchor="end">${fmtCompact(max * step)}</text>`;
    }).join("");
    const labels = rows.filter((_, index) => index === 0 || index === rows.length - 1 || index % 5 === 0).map(row => {
      const index = rows.indexOf(row);
      return `<text x="${x(index)}" y="${height - 12}" text-anchor="middle">${dateLabel(row.date)}</text>`;
    }).join("");
    container.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img"><defs><linearGradient id="areaGradient" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#3cffcd" stop-opacity=".3"/><stop offset="1" stop-color="#3cffcd" stop-opacity="0"/></linearGradient></defs>${grid}<path class="area-main" d="${area}"/><path class="line-main" d="${path}"/>${secondaryKey ? `<path class="line-secondary" d="${secondPath}"/>` : ""}<line class="axis-line" x1="${left}" x2="${left + innerW}" y1="${top + innerH}" y2="${top + innerH}"/>${labels}</svg>`;
  }

  function fmtCompact(value) {
    const n = Number(value || 0);
    if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
    if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
    if (n >= 1e3) return `${(n / 1e3).toFixed(0)}K`;
    return `${Math.round(n)}`;
  }

  function barChart(container, rows, labelKey, valueKey, altKey = null) {
    const width = 620, height = 270, left = 48, right = 18, top = 18, bottom = 38;
    const innerW = width - left - right, innerH = height - top - bottom;
    const max = Math.max(...rows.map(row => Number(row[valueKey] || 0)), 1);
    const groupW = innerW / Math.max(rows.length, 1);
    const barW = altKey ? groupW * .28 : groupW * .58;
    const bars = rows.map((row, index) => {
      const value = Number(row[valueKey] || 0);
      const h = value / max * innerH;
      const x = left + index * groupW + groupW * .2;
      let result = `<rect class="bar-main" x="${x}" y="${top + innerH - h}" width="${barW}" height="${h}" rx="3"><title>${escapeHtml(row[labelKey])}: ${fmtInt(value)}</title></rect>`;
      if (altKey) {
        const alt = Number(row[altKey] || 0);
        const altH = alt / max * innerH;
        result += `<rect class="bar-alt" x="${x + barW + 3}" y="${top + innerH - altH}" width="${barW}" height="${altH}" rx="3"><title>${escapeHtml(row[labelKey])}: ${fmtInt(alt)}</title></rect>`;
      }
      result += `<text x="${left + index * groupW + groupW / 2}" y="${height - 13}" text-anchor="middle">${escapeHtml(row[labelKey])}</text>`;
      return result;
    }).join("");
    container.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img"><line class="axis-line" x1="${left}" x2="${left + innerW}" y1="${top + innerH}" y2="${top + innerH}"/>${bars}</svg>`;
  }

  function histogram(container, values, bins = 18, logScale = false) {
    const clean = values.map(Number).filter(value => Number.isFinite(value) && value >= 0);
    const transformed = logScale ? clean.map(value => Math.log10(Math.max(value, 1))) : clean;
    const min = Math.min(...transformed, 0), max = Math.max(...transformed, 1);
    const counts = Array.from({ length: bins }, () => 0);
    transformed.forEach(value => {
      const index = Math.min(bins - 1, Math.floor((value - min) / Math.max(max - min, 1e-9) * bins));
      counts[index] += 1;
    });
    const rows = counts.map((value, index) => ({ label: index === 0 || index === bins - 1 ? fmtCompact(logScale ? 10 ** (min + (index / bins) * (max - min)) : min + (index / bins) * (max - min)) : "", value }));
    barChart(container, rows, "label", "value");
  }

  function scatterChart(container, nodes) {
    const width = 860, height = 310, left = 58, right = 22, top = 20, bottom = 48;
    const innerW = width - left - right, innerH = height - top - bottom;
    const maxX = Math.max(...nodes.map(node => Math.log1p(node.in_deg)), 1);
    const maxY = Math.max(...nodes.map(node => Math.log1p(node.out_deg)), 1);
    const x = value => left + Math.log1p(value) / maxX * innerW;
    const y = value => top + innerH - Math.log1p(value) / maxY * innerH;
    const grid = [0, .25, .5, .75, 1].map(step => `<line class="grid-line" x1="${left + step * innerW}" x2="${left + step * innerW}" y1="${top}" y2="${top + innerH}"/><line class="grid-line" x1="${left}" x2="${left + innerW}" y1="${top + step * innerH}" y2="${top + step * innerH}"/>`).join("");
    const circles = nodes.map(node => `<circle class="scatter-point" data-gid="${node.gid}" cx="${x(node.in_deg).toFixed(1)}" cy="${y(node.out_deg).toFixed(1)}" r="${(2.4 + Math.min(3, Math.log1p(node.total_kzt) / 8)).toFixed(1)}" fill="${COLORS.depth[node.depth] || "#b9cfe5"}" fill-opacity=".66"><title>${node.gid} · in ${node.in_deg} / out ${node.out_deg}</title></circle>`).join("");
    container.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img">${grid}<line class="axis-line" x1="${left}" x2="${left + innerW}" y1="${top + innerH}" y2="${top + innerH}"/><line class="axis-line" x1="${left}" x2="${left}" y1="${top}" y2="${top + innerH}"/>${circles}<text x="${left + innerW / 2}" y="${height - 10}" text-anchor="middle">Число источников →</text><text x="15" y="${top + innerH / 2}" transform="rotate(-90 15 ${top + innerH / 2})" text-anchor="middle">Число получателей →</text></svg>`;
    $$(".scatter-point", container).forEach(point => {
      point.style.cursor = "pointer";
      point.addEventListener("click", () => openNode(point.dataset.gid));
    });
  }

  function renderOverview() {
    lineChart($("#dailyChart"), D.daily, "sum_kzt", "n_tx");
    barChart($("#depthChart"), D.depth_summary.map(row => ({ ...row, label: `d${row.depth}` })), "label", "n_nodes", "n_seed");
    histogram($("#amountHistogram"), D.transactions.map(tx => tx.sum_kzt), 22, true);
    const topEdges = [...D.edges].sort((a, b) => b.sum_kzt - a.sum_kzt).slice(0, 12);
    $("#topEdgesTable").innerHTML = tableHtml(topEdges, [
      { label: "Плательщик", value: "src", className: "gid-cell" },
      { label: "Получатель", value: "dst", className: "gid-cell" },
      { label: "Сумма", value: "sum_kzt", format: fmtMoney, className: "money-cell" },
      { label: "Tx", value: "n_tx", format: fmtInt },
      { label: "Depth", value: "depth" },
      { label: "Взаимность", value: "reciprocal", format: value => value ? "Да" : "Нет" }
    ], { clickable: true, rowData: row => `data-gid="${row.dst}"` });
    $$("#topEdgesTable tr[data-gid]").forEach(row => row.addEventListener("click", () => openNode(row.dataset.gid)));
  }

  function renderPatterns() {
    $("#patternMetrics").innerHTML = [
      ["FIFO тот же день", fmtInt(D.meta.n_fifo_0d_80), "покрытие ≥80%", true],
      ["FIFO ≤1 день", fmtInt(D.meta.n_fifo_1d_80), "покрытие ≥80%"],
      ["Reciprocal-пары", fmtInt(D.meta.n_reciprocal_pairs), "оба направления"],
      ["Повторные рёбра", fmtInt(D.meta.n_repeated_edges), "n_tx ≥2"],
      ["Multi-seed", fmtInt(D.meta.n_multi_seed_direct), "прямых получателей"],
      ["Синхронный веер", fmtInt(D.meta.n_sync_fanout_5), "≥5 получателей/день"]
    ].map(item => metricCard(...item)).join("");

    const bothFlow = D.nodes.filter(node => node.in_deg > 0 && node.out_deg > 0);
    scatterChart($("#fanScatter"), bothFlow);
    histogram($("#passThroughHistogram"), bothFlow.filter(node => !node.is_seed && node.pass_through > 0).map(node => Math.min(node.pass_through, 100)), 20, true);

    const fifoRows = D.nodes
      .filter(node => node.fifo_match_1d != null && node.fifo_match_1d >= .5)
      .map(node => ({ ...node, matched_proxy: node.fifo_match_1d * Math.min(node.in_kzt, node.out_kzt) }))
      .sort((a, b) => b.matched_proxy - a.matched_proxy)
      .slice(0, 15);
    $("#fifoTable").innerHTML = tableHtml(fifoRows, [
      { label: "gid", value: "gid", className: "gid-cell", format: shortGid },
      { label: "FIFO 0д", value: "fifo_match_0d", format: value => `${(Number(value) * 100).toFixed(0)}%` },
      { label: "FIFO 1д", value: "fifo_match_1d", format: value => `${(Number(value) * 100).toFixed(0)}%` },
      { label: "Поток", value: "matched_proxy", format: fmtMoney, className: "money-cell" }
    ], { clickable: true, rowData: row => `data-gid="${row.gid}"` });

    const multiSeed = D.nodes.filter(node => node.direct_seed_in >= 2).sort((a, b) => b.direct_seed_in - a.direct_seed_in || b.in_kzt - a.in_kzt);
    $("#multiSeedTable").innerHTML = tableHtml(multiSeed, [
      { label: "gid", value: "gid", className: "gid-cell", format: shortGid },
      { label: "Seed", value: "direct_seed_in", format: fmtInt },
      { label: "Вход", value: "in_kzt", format: fmtMoney, className: "money-cell" },
      { label: "Depth", value: "depth" }
    ], { clickable: true, rowData: row => `data-gid="${row.gid}"`, maxRows: 15 });

    const edgeMap = new Map(D.edges.map(edge => [`${edge.src}>${edge.dst}`, edge]));
    const reciprocal = D.edges
      .filter(edge => edge.reciprocal && edge.src < edge.dst)
      .map(edge => {
        const reverse = edgeMap.get(`${edge.dst}>${edge.src}`);
        return { a: edge.src, b: edge.dst, total_kzt: edge.sum_kzt + (reverse?.sum_kzt || 0), n_tx: edge.n_tx + (reverse?.n_tx || 0) };
      })
      .sort((a, b) => b.total_kzt - a.total_kzt)
      .slice(0, 15);
    $("#reciprocalTable").innerHTML = tableHtml(reciprocal, [
      { label: "Узел A", value: "a", className: "gid-cell", format: shortGid },
      { label: "Узел B", value: "b", className: "gid-cell", format: shortGid },
      { label: "Сумма", value: "total_kzt", format: fmtMoney, className: "money-cell" },
      { label: "Tx", value: "n_tx", format: fmtInt }
    ], { clickable: true, rowData: row => `data-gid="${row.a}"` });

    ["#fifoTable", "#multiSeedTable", "#reciprocalTable"].forEach(selector => {
      $$(`${selector} tr[data-gid]`).forEach(row => row.addEventListener("click", () => openNode(row.dataset.gid)));
    });
  }

  function flattenScalars(value, prefix = "", rows = [], depth = 0) {
    if (rows.length >= 40 || depth > 4 || value == null) return rows;
    if (Array.isArray(value)) {
      value.slice(0, 8).forEach((item, index) => flattenScalars(item, `${prefix}[${index}]`, rows, depth + 1));
    } else if (typeof value === "object") {
      Object.entries(value).forEach(([key, item]) => flattenScalars(item, prefix ? `${prefix}.${key}` : key, rows, depth + 1));
    } else if (["string", "number", "boolean"].includes(typeof value)) {
      rows.push({ metric: prefix || "value", value });
    }
    return rows;
  }

  function renderAnalytics() {
    const roleNodes = D.nodes.filter(node => node.role && ROLE_LABELS[node.role]);
    const priorityNodes = D.nodes.filter(node => node.priority_score != null && Number.isFinite(Number(node.priority_score)));
    const hasRoles = roleNodes.length > 0;
    const loadedFiles = Object.entries(P.files || {}).filter(([, loaded]) => loaded).map(([name]) => name);
    const badge = $("#productionBadge");
    badge.textContent = P.available ? `Backend · ${loadedFiles.length} файлов` : "Raw-data fallback";
    badge.classList.toggle("offline", !P.available);
    $("#productionBanner").className = `notice-panel${hasRoles ? "" : " warning"}`;
    $("#productionBanner").innerHTML = hasRoles
      ? `<strong>Production-результаты подключены.</strong> Загружены: ${escapeHtml(loadedFiles.join(", ") || "nodes_roles.csv")}.${(P.warnings || []).length ? ` Предупреждения: ${escapeHtml(P.warnings.join("; "))}` : ""}`
      : `<strong>Роли backend пока не найдены.</strong> Сырые визуализации продолжают работать. После появления <code>output/nodes_roles.csv</code> пересоберите bundle — этот раздел заполнится автоматически.`;

    const meanConfidence = roleNodes.length ? roleNodes.reduce((sum, node) => sum + Number(node.role_score || 0), 0) / roleNodes.length : null;
    const highPriority = priorityNodes.filter(node => Number(node.priority_score) >= .9).length;
    const uncertain = priorityNodes.filter(node => Number(node.priority_score) >= .75 && Number(node.role_score || 0) < .6).length;
    const censoredTerminals = roleNodes.filter(node => node.truncated_by_depth && node.role === "terminal").length;
    $("#roleMetrics").innerHTML = [
      ["Классифицировано", fmtInt(roleNodes.length), `${fmtInt(D.meta.n_nodes)} узлов всего`, true],
      ["Средняя уверенность", fmtPercent(meanConfidence), "role_score"],
      ["Priority ≥ 0,90", fmtInt(highPriority), "очередь проверки"],
      ["Приоритетные, но спорные", fmtInt(uncertain), "priority ≥0,75 · confidence <0,60"],
      ["Depth-4 terminal", fmtInt(censoredTerminals), "требуют censoring-cap"],
      ["Кластеры", fmtInt(D.meta.n_clusters), D.meta.has_production_clusters ? "production" : "Louvain fallback"]
    ].map(item => metricCard(...item)).join("");

    if (hasRoles) {
      const roleRows = Object.keys(ROLE_LABELS).map(role => ({
        label: roleLabel(role),
        value: roleNodes.filter(node => node.role === role).length
      }));
      barChart($("#roleDistribution"), roleRows, "label", "value");
    } else {
      $("#roleDistribution").innerHTML = emptyInline("Распределение появится после расчёта ролей.");
    }

    let topRows = (P.top_nodes || []).map(row => {
      const node = nodeById.get(String(row.gid));
      return { ...(node || {}), ...row, gid: String(row.gid ?? node?.gid ?? "") };
    });
    if (!topRows.length) topRows = [...priorityNodes].sort((a, b) => Number(b.priority_score) - Number(a.priority_score)).slice(0, 20);
    topRows = topRows.sort((a, b) => Number(a.rank ?? 1e9) - Number(b.rank ?? 1e9) || Number(b.priority_score) - Number(a.priority_score))
      .map((row, index) => ({ ...row, display_rank: row.rank ?? index + 1 }));
    $("#priorityTable").innerHTML = topRows.length ? tableHtml(topRows, [
      { label: "#", value: "display_rank", format: fmtInt },
      { label: "gid", value: "gid", className: "gid-cell", format: shortGid },
      { label: "Роль", value: "role", format: roleLabel },
      { label: "Приоритет", value: "priority_score", html: true, format: value => scoreTrack(value, "#ffe373") },
      { label: "Почему", value: row => row.why || row.evidence || "—" }
    ], { clickable: true, rowData: row => `data-gid="${row.gid}"`, maxRows: 20 }) : emptyInline("top_nodes.csv или priority_score пока не рассчитаны.");

    const uncertainRows = [...priorityNodes]
      .map(node => ({ ...node, review_value: Number(node.priority_score) * (1 - Number(node.role_score || 0)) }))
      .filter(node => node.review_value > 0)
      .sort((a, b) => b.review_value - a.review_value)
      .slice(0, 18);
    $("#uncertaintyTable").innerHTML = uncertainRows.length ? tableHtml(uncertainRows, [
      { label: "gid", value: "gid", className: "gid-cell", format: shortGid },
      { label: "Гипотеза", value: "role", format: roleLabel },
      { label: "Альтернатива", value: row => roleLabel(row.secondary_role) },
      { label: "Уверенность", value: "role_score", format: fmtPercent },
      { label: "Приоритет", value: "priority_score", format: fmtScore },
      { label: "Причина", value: row => row.uncertainty_reason || (row.truncated_by_depth ? "Depth-4: выход не наблюдается" : "Малый разрыв role-score") }
    ], { clickable: true, rowData: row => `data-gid="${row.gid}"` }) : emptyInline("Неопределённость станет доступна вместе с role_score и priority_score.");
    ["#priorityTable", "#uncertaintyTable"].forEach(selector => {
      $$(`${selector} tr[data-gid]`).forEach(row => row.addEventListener("click", () => openNode(row.dataset.gid)));
    });

    const resilience = P.resilience || [];
    if (resilience.length) {
      const strategies = [...new Set(resilience.map(row => row.strategy || row.ranking || "основная"))];
      const strategy = strategies[0];
      const primary = resilience.filter(row => (row.strategy || row.ranking || "основная") === strategy)
        .sort((a, b) => Number(a.n_removed ?? a.N ?? 0) - Number(b.n_removed ?? b.N ?? 0))
        .map(row => ({
          label: String(row.n_removed ?? row.N ?? "—"),
          value: 100 * Number(row.lcc_ratio ?? row.lcc_ratio_base ?? 0)
        }));
      barChart($("#resilienceChart"), primary, "label", "value");
      $("#resilienceTable").innerHTML = tableHtml(resilience, [
        { label: "Стратегия", value: row => row.strategy || row.ranking || "—" },
        { label: "N", value: row => row.n_removed ?? row.N ?? "—", format: fmtInt },
        { label: "LCC", value: row => row.lcc_nodes ?? row.lcc ?? "—", format: fmtInt },
        { label: "LCC, %", value: row => row.lcc_ratio ?? row.lcc_ratio_base, format: fmtPercent },
        { label: "Компоненты", value: row => row.n_components ?? row.n_wcc ?? "—", format: fmtInt },
        { label: "Остаток потока", value: "remaining_edge_weight_ratio", format: fmtPercent }
      ], { maxRows: 18 });
    } else {
      $("#resilienceChart").innerHTML = emptyInline("resilience.csv не найден. Raw-граф остаётся доступен.");
      $("#resilienceTable").innerHTML = "";
    }

    const validationRows = flattenScalars(P.validation).slice(0, 12);
    $("#validationCards").innerHTML = validationRows.length
      ? validationRows.map(row => `<article class="validation-card"><span>${escapeHtml(row.metric)}</span><strong>${escapeHtml(typeof row.value === "boolean" ? (row.value ? "Пройдено" : "Не пройдено") : typeof row.value === "number" ? fmtScore(row.value) : row.value)}</strong><p>Метрика устойчивости процедуры</p></article>`).join("")
      : `<article class="validation-card"><span>Synthetic motifs</span><strong>Не загружено</strong><p>Ожидается validation_report.json</p></article><article class="validation-card"><span>Perturbation</span><strong>Не загружено</strong><p>Agreement / Spearman / Jaccard</p></article><article class="validation-card"><span>Ablation</span><strong>Не загружено</strong><p>Вклад групп признаков</p></article><article class="validation-card"><span>Cluster stability</span><strong>Не загружено</strong><p>ARI / VI / co-assignment</p></article>`;

    const thresholdRows = flattenScalars(P.thresholds).slice(0, 30);
    $("#thresholdsSummary").innerHTML = thresholdRows.length ? tableHtml(thresholdRows, [
      { label: "Параметр", value: "metric" },
      { label: "Значение", value: "value", format: value => typeof value === "number" ? fmtScore(value) : value }
    ]) : emptyInline("thresholds.json не найден. В production-сборке сохраните использованные квантили и gate-пороги.");
  }

  const network = {
    canvas: null,
    ctx: null,
    width: 0,
    height: 0,
    scale: 1,
    panX: 0,
    panY: 0,
    fitScale: 1,
    dragging: false,
    moved: false,
    lastX: 0,
    lastY: 0,
    selected: null,
    hover: null,
    visibleNodes: [],
    visibleEdges: [],
    positions: new Map(),
    filters: { component: "all", cluster: "all", depths: new Set([0, 1, 2, 3, 4]), minAmount: 0, seedOnly: false, censoredOnly: false, color: "role", layout: "depth" },

    setup() {
      this.canvas = $("#networkCanvas");
      this.ctx = this.canvas.getContext("2d");
      this.populateControls();
      this.attachEvents();
      // Measure before projecting normalized coordinates into canvas pixels.
      this.resize();
      new ResizeObserver(() => this.resize()).observe(this.canvas.parentElement);
      this.applyFilters();
    },

    populateControls() {
      $("#componentFilter").innerHTML = `<option value="all">Все (${D.meta.n_components})</option>` + D.components.map(row => `<option value="${row.component_id}">#${row.component_id} · ${fmtInt(row.n_nodes)} узл.</option>`).join("");
      $("#clusterFilter").innerHTML = `<option value="all">Все (${D.meta.n_clusters})</option>` + D.clusters.map(row => `<option value="${row.cluster_id}">#${row.cluster_id} · ${fmtInt(row.n_nodes)} узл.</option>`).join("");
      $("#depthChecks").innerHTML = [0, 1, 2, 3, 4].map(depth => `<label><input type="checkbox" data-depth="${depth}" checked> ${depth}</label>`).join("");
      this.renderLegend();
    },

    attachEvents() {
      $("#componentFilter").addEventListener("change", event => { this.filters.component = event.target.value; this.applyFilters(); });
      $("#clusterFilter").addEventListener("change", event => { this.filters.cluster = event.target.value; this.applyFilters(); });
      $$("#depthChecks input").forEach(input => input.addEventListener("change", () => {
        this.filters.depths = new Set($$("#depthChecks input:checked").map(item => Number(item.dataset.depth)));
        this.applyFilters();
      }));
      $("#amountFilter").addEventListener("input", event => {
        const pct = Number(event.target.value) / 100;
        const min = 5_000, max = Math.max(...D.edges.map(edge => edge.sum_kzt));
        this.filters.minAmount = pct === 0 ? 0 : Math.exp(Math.log(min) + pct * (Math.log(max) - Math.log(min)));
        $("#amountFilterLabel").textContent = fmtMoney(this.filters.minAmount);
        this.applyFilters();
      });
      $("#seedOnly").addEventListener("change", event => { this.filters.seedOnly = event.target.checked; this.applyFilters(); });
      $("#censoredOnly").addEventListener("change", event => { this.filters.censoredOnly = event.target.checked; this.applyFilters(); });
      $("#colorMode").addEventListener("change", event => { this.filters.color = event.target.value; this.renderLegend(); this.draw(); });
      $("#layoutMode").addEventListener("change", event => { this.filters.layout = event.target.value; this.applyFilters(); });
      $("#resetNetwork").addEventListener("click", () => this.resetTransform());
      $("#fitNetwork").addEventListener("click", () => this.resetTransform());
      $("#zoomIn").addEventListener("click", () => this.zoomAt(1.35));
      $("#zoomOut").addEventListener("click", () => this.zoomAt(1 / 1.35));
      this.canvas.addEventListener("keydown", event => {
        if (["+", "=", "-", "0"].includes(event.key)) {
          event.preventDefault();
          if (event.key === "0") this.resetTransform();
          else this.zoomAt(event.key === "-" ? 1 / 1.35 : 1.35);
        }
      });

      const search = $("#nodeSearch");
      const executeSearch = () => {
        const query = search.value.trim();
        if (!query) return;
        const exact = nodeById.get(query);
        const node = exact || D.nodes.find(item => item.gid.includes(query));
        search.setCustomValidity("");
        $("#nodeSearchStatus").textContent = "";
        if (node) openNode(node.gid);
        else {
          search.setCustomValidity("gid не найден");
          $("#nodeSearchStatus").textContent = "gid не найден в этом наборе. Предыдущая карточка закрыта.";
          this.closeInspector();
        }
      };
      search.addEventListener("input", () => { search.setCustomValidity(""); $("#nodeSearchStatus").textContent = ""; });
      search.addEventListener("search", executeSearch);
      search.addEventListener("keydown", event => { if (event.key === "Enter") executeSearch(); });

      this.canvas.addEventListener("pointerdown", event => {
        this.dragging = true; this.moved = false; this.lastX = event.clientX; this.lastY = event.clientY;
        this.canvas.setPointerCapture(event.pointerId);
      });
      this.canvas.addEventListener("pointermove", event => {
        const rect = this.canvas.getBoundingClientRect();
        const x = event.clientX - rect.left, y = event.clientY - rect.top;
        if (this.dragging) {
          const dx = event.clientX - this.lastX, dy = event.clientY - this.lastY;
          if (Math.abs(dx) + Math.abs(dy) > 2) this.moved = true;
          this.panX += dx; this.panY += dy; this.lastX = event.clientX; this.lastY = event.clientY; this.draw();
        } else {
          const hit = this.hitTest(x, y);
          if (hit !== this.hover) { this.hover = hit; this.draw(); }
          this.showTooltip(hit, x, y);
        }
      });
      this.canvas.addEventListener("pointerup", event => {
        if (!this.moved) {
          const rect = this.canvas.getBoundingClientRect();
          const hit = this.hitTest(event.clientX - rect.left, event.clientY - rect.top);
          if (hit) openNode(hit.gid);
        }
        this.dragging = false;
      });
      this.canvas.addEventListener("pointerleave", () => { this.dragging = false; this.hover = null; $("#networkTooltip").classList.add("hidden"); this.draw(); });
      this.canvas.addEventListener("wheel", event => {
        event.preventDefault();
        const rect = this.canvas.getBoundingClientRect();
        const x = event.clientX - rect.left, y = event.clientY - rect.top;
        this.zoomAt(event.deltaY < 0 ? 1.14 : 1 / 1.14, x, y);
      }, { passive: false });
    },

    zoomAt(factor, x = this.width / 2, y = this.height / 2) {
      const oldScale = this.scale;
      this.scale = clamp(this.scale * factor, this.fitScale * .35, Math.max(24, this.fitScale * 12));
      const ratio = this.scale / oldScale;
      this.panX = x - (x - this.panX - this.width / 2) * ratio - this.width / 2;
      this.panY = y - (y - this.panY - this.height / 2) * ratio - this.height / 2;
      this.draw();
    },

    fitNodes(nodes = this.visibleNodes) {
      const points = nodes.map(node => this.positions.get(node.gid)).filter(Boolean);
      this.scale = 1; this.panX = 0; this.panY = 0;
      if (points.length && this.width && this.height) {
        const xs = points.map(point => point.x), ys = points.map(point => point.y);
        const minX = Math.min(...xs), maxX = Math.max(...xs);
        const minY = Math.min(...ys), maxY = Math.max(...ys);
        // Fit the actual selection, not the full dataset's coordinate space.
        // A single node is centered without an extreme zoom.
        if (points.length > 1) this.scale = Math.min(
          Math.max(1, this.width - 96) / Math.max(maxX - minX, 1),
          Math.max(1, this.height - 116) / Math.max(maxY - minY, 1),
          40
        );
        this.panX = (this.width / 2 - (minX + maxX) / 2) * this.scale;
        this.panY = (this.height / 2 - (minY + maxY) / 2) * this.scale;
      }
      this.fitScale = this.scale;
    },

    resetTransform() {
      this.fitNodes();
      this.draw();
    },

    resize() {
      const rect = this.canvas.parentElement.getBoundingClientRect();
      // Hidden tabs report zero size. Keep their last valid camera until shown.
      if (rect.width < 2 || rect.height < 2) return;
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const oldWidth = this.width, oldHeight = this.height;
      const wasFitted = Math.abs(this.scale - this.fitScale) < 1e-9 && !this.selected;
      this.width = rect.width;
      this.height = rect.height;
      this.canvas.width = Math.floor(this.width * dpr);
      this.canvas.height = Math.floor(this.height * dpr);
      this.canvas.style.width = `${this.width}px`;
      this.canvas.style.height = `${this.height}px`;
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      // Coordinates are pixels, so every size change must re-project them.
      this.updatePositions();
      if (wasFitted || oldWidth === 0 || oldHeight === 0) this.fitNodes();
      else {
        this.panX *= Math.max(1, this.width - 96) / Math.max(1, oldWidth - 96);
        this.panY *= Math.max(1, this.height - 116) / Math.max(1, oldHeight - 116);
      }
      this.draw();
    },

    nodeEligible(node) {
      if (this.filters.component !== "all" && String(node.component_id) !== this.filters.component) return false;
      if (this.filters.cluster !== "all" && String(node.cluster_id) !== this.filters.cluster) return false;
      if (!this.filters.depths.has(Number(node.depth))) return false;
      if (this.filters.seedOnly && !node.is_seed) return false;
      if (this.filters.censoredOnly && !node.truncated_by_depth) return false;
      return true;
    },

    applyFilters() {
      const candidate = new Set(D.nodes.filter(node => this.nodeEligible(node)).map(node => node.gid));
      this.visibleEdges = D.edges.filter(edge => candidate.has(edge.src) && candidate.has(edge.dst) && edge.sum_kzt >= this.filters.minAmount);
      if (this.filters.minAmount > 0) {
        const incident = new Set();
        this.visibleEdges.forEach(edge => { incident.add(edge.src); incident.add(edge.dst); });
        this.visibleNodes = D.nodes.filter(node => candidate.has(node.gid) && incident.has(node.gid));
      } else {
        this.visibleNodes = D.nodes.filter(node => candidate.has(node.gid));
      }
      $("#visibleCount").textContent = `${fmtInt(this.visibleNodes.length)} узл. · ${fmtInt(this.visibleEdges.length)} рёбер`;
      this.hover = null;
      $("#networkTooltip").classList.add("hidden");
      if (this.selected && !this.visibleNodes.some(node => node.gid === this.selected.gid)) this.closeInspector();
      this.updatePositions();
      this.fitNodes();
      this.draw();
    },

    updatePositions() {
      this.positions.clear();
      const marginX = 48, marginY = 58;
      const keyX = this.filters.layout === "cluster" ? "x_cluster" : "x_depth";
      const keyY = this.filters.layout === "cluster" ? "y_cluster" : "y_depth";
      const localY = new Map();
      if (this.filters.layout === "depth" && this.visibleNodes.length < D.nodes.length) {
        // A filtered component occupied only a thin slice of the global depth
        // columns. Redistribute each visible column so its nodes stay readable.
        const columns = new Map();
        this.visibleNodes.forEach(node => {
          if (!columns.has(node.depth)) columns.set(node.depth, []);
          columns.get(node.depth).push(node);
        });
        columns.forEach(nodes => {
          nodes.sort((a, b) => a.y_depth - b.y_depth);
          nodes.forEach((node, index) => localY.set(node.gid, nodes.length === 1 ? .5 : index / (nodes.length - 1)));
        });
      }
      this.visibleNodes.forEach(node => {
        this.positions.set(node.gid, {
          x: marginX + clamp(Number(node[keyX]), -.08, 1.08) * Math.max(1, this.width - marginX * 2),
          y: marginY + clamp(localY.get(node.gid) ?? Number(node[keyY]), -.08, 1.08) * Math.max(1, this.height - marginY * 2)
        });
      });
    },

    screenPosition(gid) {
      const pos = this.positions.get(gid);
      if (!pos) return null;
      return {
        x: (pos.x - this.width / 2) * this.scale + this.width / 2 + this.panX,
        y: (pos.y - this.height / 2) * this.scale + this.height / 2 + this.panY
      };
    },

    nodeColor(node) {
      if (this.filters.color === "cluster") return COLORS.cluster[node.cluster_id % COLORS.cluster.length];
      if (this.filters.color === "status") return node.is_seed ? COLORS.depth[0] : (node.truncated_by_depth ? COLORS.depth[4] : "#3cffcd");
      if (this.filters.color === "role") return COLORS.role[node.role] || COLORS.role.unknown;
      return COLORS.depth[Number(node.depth)] || "#b9cfe5";
    },

    renderLegend() {
      let entries;
      if (this.filters.color === "cluster") entries = D.clusters.slice(0, 10).map(item => [`Кластер ${item.cluster_id}`, COLORS.cluster[item.cluster_id % COLORS.cluster.length]]);
      else if (this.filters.color === "status") entries = [["Seed", COLORS.depth[0]], ["Depth-4", COLORS.depth[4]], ["Наблюдаемый", "#3cffcd"]];
      else if (this.filters.color === "role") entries = Object.entries(COLORS.role).filter(([key]) => key !== "unknown").map(([key, color]) => [roleLabel(key), color]);
      else entries = COLORS.depth.map((color, depth) => [`Depth ${depth}`, color]);
      $("#networkLegend").innerHTML = entries.map(([label, color]) => `<div class="legend-item"><span class="legend-dot" style="background:${color}"></span>${escapeHtml(label)}</div>`).join("");
    },

    draw() {
      if (!this.ctx || !this.width || !this.height) return;
      const ctx = this.ctx;
      ctx.clearRect(0, 0, this.width, this.height);
      $("#networkZoom").textContent = `${Math.round(this.scale / this.fitScale * 100)}%`;
      if (!this.visibleNodes.length) {
        ctx.fillStyle = "#dbe9f8";
        ctx.font = "14px system-ui";
        ctx.textAlign = "center";
        ctx.fillText("Нет узлов для выбранных фильтров", this.width / 2, this.height / 2);
        return;
      }
      const highlighted = this.hover || this.selected;
      const neighbors = new Set(highlighted ? [highlighted.gid] : []);
      if (highlighted) this.visibleEdges.forEach(edge => {
        if (edge.src === highlighted.gid || edge.dst === highlighted.gid) { neighbors.add(edge.src); neighbors.add(edge.dst); }
      });
      const edgeMax = Math.max(...this.visibleEdges.map(edge => edge.sum_kzt), 1);
      ctx.lineCap = "round";
      for (const edge of this.visibleEdges) {
        const a = this.screenPosition(edge.src), b = this.screenPosition(edge.dst);
        if (!a || !b) continue;
        if ((a.x < -20 && b.x < -20) || (a.y < -20 && b.y < -20) || (a.x > this.width + 20 && b.x > this.width + 20) || (a.y > this.height + 20 && b.y > this.height + 20)) continue;
        const incident = highlighted && (edge.src === highlighted.gid || edge.dst === highlighted.gid);
        const alpha = highlighted ? (incident ? .96 : .1) : this.visibleEdges.length < 750 ? .58 : .28;
        ctx.strokeStyle = incident ? `rgba(223, 186, 255, ${alpha})` : `rgba(155, 203, 244, ${alpha})`;
        ctx.lineWidth = (incident ? 1.2 : .4) + (incident ? 1.3 : .7) * Math.sqrt(edge.sum_kzt / edgeMax);
        ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
        if (incident || this.visibleEdges.length < 300) this.drawArrow(ctx, a, b);
      }
      for (const node of this.visibleNodes) {
        const pos = this.screenPosition(node.gid);
        if (!pos || pos.x < -20 || pos.y < -20 || pos.x > this.width + 20 || pos.y > this.height + 20) continue;
        const selected = node.gid === this.selected?.gid;
        const hovered = node.gid === this.hover?.gid;
        const radius = selected ? 8 : hovered ? 7 : (this.visibleNodes.length < 100 ? 4 : 2.3) + Math.min(2.5, Math.log1p(node.total_kzt || 0) / 8);
        ctx.globalAlpha = highlighted && !neighbors.has(node.gid) ? .6 : 1;
        if (selected || hovered) {
          ctx.beginPath(); ctx.arc(pos.x, pos.y, radius + 5, 0, Math.PI * 2); ctx.fillStyle = selected ? "rgba(223,186,255,.32)" : "rgba(255,255,255,.08)"; ctx.fill();
        }
        ctx.beginPath(); ctx.arc(pos.x, pos.y, radius, 0, Math.PI * 2);
        ctx.fillStyle = this.nodeColor(node); ctx.fill();
        if (node.is_seed) { ctx.lineWidth = 1.5; ctx.strokeStyle = "#fff1d5"; ctx.stroke(); }
        if (selected || hovered || this.visibleNodes.length <= 20) {
          ctx.font = "11px system-ui"; ctx.textAlign = "center";
          ctx.lineWidth = 4; ctx.strokeStyle = "#0b1422";
          ctx.strokeText(shortGid(node.gid), pos.x, pos.y - radius - 7);
          ctx.fillStyle = "#ffffff"; ctx.fillText(shortGid(node.gid), pos.x, pos.y - radius - 7);
        }
      }
      ctx.globalAlpha = 1;
    },

    drawArrow(ctx, a, b) {
      const angle = Math.atan2(b.y - a.y, b.x - a.x);
      const x = a.x + (b.x - a.x) * .82, y = a.y + (b.y - a.y) * .82;
      ctx.fillStyle = ctx.strokeStyle;
      ctx.beginPath();
      ctx.moveTo(x, y);
      ctx.lineTo(x - Math.cos(angle - .55) * 5, y - Math.sin(angle - .55) * 5);
      ctx.lineTo(x - Math.cos(angle + .55) * 5, y - Math.sin(angle + .55) * 5);
      ctx.closePath(); ctx.fill();
    },

    hitTest(x, y) {
      let best = null, bestDistance = 11;
      for (const node of this.visibleNodes) {
        const pos = this.screenPosition(node.gid);
        if (!pos) continue;
        const distance = Math.hypot(pos.x - x, pos.y - y);
        if (distance < bestDistance) { best = node; bestDistance = distance; }
      }
      return best;
    },

    showTooltip(node, x, y) {
      const tooltip = $("#networkTooltip");
      if (!node) { tooltip.classList.add("hidden"); return; }
      tooltip.innerHTML = `<strong>${escapeHtml(node.gid)}</strong><br>depth ${node.depth} · in ${node.in_deg} / out ${node.out_deg}<br>${escapeHtml(fmtMoney(node.total_kzt))}`;
      tooltip.style.left = `${Math.min(this.width - 230, x + 12)}px`;
      tooltip.style.top = `${Math.max(8, y - 58)}px`;
      tooltip.classList.remove("hidden");
    },

    focusNode(node) {
      if (!this.positions.has(node.gid)) {
        this.filters.component = "all"; this.filters.cluster = "all"; this.filters.depths = new Set([0,1,2,3,4]); this.filters.seedOnly = false; this.filters.censoredOnly = false; this.filters.minAmount = 0;
        $("#componentFilter").value = "all"; $("#clusterFilter").value = "all"; $("#seedOnly").checked = false; $("#censoredOnly").checked = false; $("#amountFilter").value = 0; $("#amountFilterLabel").textContent = "0 KZT"; $$("#depthChecks input").forEach(input => input.checked = true);
        this.applyFilters();
      }
      this.selected = node;
      const neighbors = new Set([node.gid]);
      this.visibleEdges.forEach(edge => {
        if (edge.src === node.gid || edge.dst === node.gid) { neighbors.add(edge.src); neighbors.add(edge.dst); }
      });
      this.fitNodes(this.visibleNodes.filter(item => neighbors.has(item.gid)));
      this.draw();
    },

    closeInspector() {
      this.selected = null;
      $("#nodeInspector").classList.remove("has-node");
      $(".network-layout").classList.remove("has-selection");
      this.resize();
      this.draw();
    }
  };

  function sparklineForNode(gid) {
    const events = txByNode.get(gid) || [];
    const byDate = new Map(D.daily.map(day => [day.date, { in: 0, out: 0 }]));
    events.forEach(tx => byDate.get(tx.date)[tx.direction] += Number(tx.sum_kzt));
    const rows = [...byDate.entries()].map(([date, values]) => ({ date, ...values }));
    const max = Math.max(...rows.flatMap(row => [row.in, row.out]), 1);
    const width = 250, height = 80;
    const path = key => rows.map((row, index) => `${index ? "L" : "M"}${(index / (rows.length - 1) * width).toFixed(1)},${(height - row[key] / max * (height - 8) - 4).toFixed(1)}`).join(" ");
    return `<svg viewBox="0 0 ${width} ${height}" aria-label="Динамика узла"><path d="${path("in")}" fill="none" stroke="#3cffcd" stroke-width="2"/><path d="${path("out")}" fill="none" stroke="#ffe373" stroke-width="2"/></svg>`;
  }

  const nodeExplanations = new Map();

  function renderNodeExplanation(gid) {
    const panel = $("#nodeRoleExplanation");
    if (!panel || panel.dataset.gid !== gid) return;
    const state = nodeExplanations.get(gid) || {};
    const button = $("#generateNodeExplanation", panel);
    button.disabled = Boolean(state.pending);
    button.textContent = state.pending ? "Готовим объяснение…" : state.result ? "Показать сохранённое объяснение" : "Сгенерировать объяснение роли";
    const status = $(".explanation-status", panel);
    const answer = $(".explanation-answer", panel);
    status.textContent = state.pending ? "Проверяем метрики и условия присвоения роли…" : state.error || (state.result
      ? (state.result.mode === "openai" || state.result.source === "openai") ? `OpenAI · ${state.result.model || "API"}${state.result.cached ? " · сохранённый ответ" : ""} · доступно в CSV` : state.result.notice || "Локальное объяснение по правилам · не API"
      : "Кнопка отправляет в OpenAI метрики этого узла без gid и списка переводов. Нужен API-ключ; новые ответы оплачиваются по тарифу API, сохранённые используются повторно.");
    status.classList.toggle("explanation-error", Boolean(state.error || state.result?.api_error));
    if (window.HackAlemNarratives) answer.innerHTML = window.HackAlemNarratives.structuredHtml(state.result);
    else answer.textContent = state.result?.explanation || "";
    answer.classList.toggle("hidden", !state.result);
  }

  async function explainNodeRole(gid) {
    if (nodeExplanations.get(gid)?.pending) return;
    const state = { ...nodeExplanations.get(gid), pending: true, error: null };
    nodeExplanations.set(gid, state);
    renderNodeExplanation(gid);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 130_000);
    try {
      const response = await fetch(apiUrl("/api/explain-node"), {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-HackAlem-Action": "explain-node" },
        body: JSON.stringify({ gid }),
        signal: controller.signal
      });
      if (!(response.headers.get("content-type") || "").includes("application/json")) {
        throw new Error("Перезапустите сервер проекта: endpoint объяснения недоступен.");
      }
      const result = await response.json();
      if (!response.ok || result.status !== "ok") throw new Error(result.message || "Не удалось получить объяснение.");
      if (result.gid !== gid) throw new Error("Ответ относится к другому узлу. Повторите запрос.");
      state.result = result;
      window.dispatchEvent(new Event("hackalem:explanation-saved"));
    } catch (error) {
      state.error = error.name === "AbortError" ? "Истекло время ожидания. Можно повторить запрос." : error.message;
    } finally {
      clearTimeout(timeout);
      state.pending = false;
      // The user may have selected another node while this request was pending.
      renderNodeExplanation(gid);
    }
  }

  function renderInspector(node) {
    const inspector = $("#nodeInspector");
    inspector.classList.add("has-node");
    $(".network-layout").classList.add("has-selection");
    const outgoing = [...(outgoingById.get(node.gid) || [])].sort((a, b) => b.sum_kzt - a.sum_kzt);
    const incoming = [...(incomingById.get(node.gid) || [])].sort((a, b) => b.sum_kzt - a.sum_kzt);
    const badge = [
      node.is_seed ? `<span class="badge seed">seed</span>` : "",
      node.truncated_by_depth ? `<span class="badge censored">Граница · шаг 4</span>` : "",
      node.role ? `<span class="badge role">${escapeHtml(roleLabel(node.role))} · ${fmtPercent(node.role_score)}</span>` : "",
      `<span class="badge">Кластер ${node.cluster_id}</span>`
    ].join("");
    const neighborButtons = (edges, direction) => edges.slice(0, 6).map(edge => {
      const gid = direction === "out" ? edge.dst : edge.src;
      return `<button class="neighbor-button" data-gid="${gid}"><span>${direction === "out" ? "→" : "←"} ${escapeHtml(shortGid(gid))}</span><strong>${escapeHtml(fmtMoney(edge.sum_kzt))}</strong></button>`;
    }).join("") || `<span class="quiet-label">Нет наблюдаемых связей</span>`;
    inspector.innerHTML = `
      <div class="inspector-head"><button id="closeInspector" class="inspector-close" aria-label="Закрыть карточку узла">×</button><span class="eyebrow">Карточка клиента</span><strong class="gid">${escapeHtml(node.gid)}</strong><div class="badge-row">${badge}</div></div>

      <div class="node-stat-grid">
        <div class="node-stat"><span>Входящие</span><strong>${fmtMoney(node.in_kzt)}</strong></div>
        <div class="node-stat"><span>Исходящие</span><strong>${fmtMoney(node.out_kzt)}</strong></div>
        <div class="node-stat"><span>Источники / tx</span><strong>${fmtInt(node.in_deg)} / ${fmtInt(node.in_tx)}</strong></div>
        <div class="node-stat"><span>Получатели / tx</span><strong>${fmtInt(node.out_deg)} / ${fmtInt(node.out_tx)}</strong></div>
        <div class="node-stat"><span>PageRank</span><strong>${fmtScore(node.pagerank_amount)}</strong></div>
        <div class="node-stat"><span>Betweenness</span><strong>${fmtScore(node.betweenness)}</strong></div>
        <div class="node-stat"><span>Сила гипотезы</span><strong>${fmtPercent(node.role_score)}</strong></div>
        <div class="node-stat"><span>Приоритет проверки</span><strong>${fmtScore(node.priority_score)}</strong></div>
      </div>
      ${node.evidence ? `<div class="inspector-section"><h4>Числовые основания</h4><p class="evidence">${escapeHtml(node.evidence)}</p></div>` : ""}
      ${(node.secondary_role || node.uncertainty_reason) ? `<div class="inspector-section"><h4>Неопределённость</h4><p class="evidence">${node.secondary_role ? `Альтернатива: ${escapeHtml(roleLabel(node.secondary_role))}. ` : ""}${escapeHtml(node.uncertainty_reason || "Роли имеют близкие значения score.")}</p></div>` : ""}
      ${node.p_continue != null ? `<div class="inspector-section"><h4>${node.truncated_by_depth ? "Цензура выхода" : "Оценка продолжения"}</h4><p class="evidence">Оценка продолжения наблюдаемого маршрута p_continue=${escapeHtml(fmtScore(node.p_continue))}. ${node.truncated_by_depth ? "Выход на depth=4 не наблюдаем, поэтому terminal не следует из out_degree=0." : "Для этого узла выход наблюдаем; модель показана как дополнительный inbound-only сигнал."} Это не доказательство роли.</p></div>` : ""}
      ${node.truncated_by_depth ? `<div class="inspector-section"><h4>Ограничение</h4><p class="evidence">Исходящие связи после четвёртого шага не наблюдаются. Узел нельзя автоматически считать конечным получателем.</p></div>` : ""}
      <section id="nodeRoleExplanation" class="node-role-explanation" data-gid="${escapeHtml(node.gid)}">
        <button id="generateNodeExplanation" type="button" class="button">Сгенерировать объяснение роли</button>
        <p class="explanation-status" role="status"></p>
        <div class="explanation-answer hidden" aria-live="polite"></div>
      </section>
      <div class="inspector-section"><h4>Динамика · вход / выход</h4>${sparklineForNode(node.gid)}</div>
      <div class="inspector-section"><h4>Крупнейшие исходящие</h4><div class="neighbor-list">${neighborButtons(outgoing, "out")}</div></div>
      <div class="inspector-section"><h4>Крупнейшие входящие</h4><div class="neighbor-list">${neighborButtons(incoming, "in")}</div></div>`;
    $$(".neighbor-button", inspector).forEach(button => button.addEventListener("click", () => openNode(button.dataset.gid)));
    $("#closeInspector").addEventListener("click", () => network.closeInspector());
    $("#generateNodeExplanation").addEventListener("click", () => explainNodeRole(node.gid));
    renderNodeExplanation(node.gid);
  }

  function openNode(gid) {
    const node = nodeById.get(String(gid));
    if (!node) return;
    const networkButton = $('.nav-item[data-view="network"]');
    networkButton.click();
    $("#nodeSearch").value = node.gid;
    renderInspector(node);
    network.resize();
    network.focusNode(node);
  }

  function filteredTransactions() {
    const query = $("#txGidFilter").value.trim();
    const min = Number($("#txMinAmount").value || 0);
    return D.transactions.filter(tx => tx.sum_kzt >= min && (!query || tx.src.includes(query) || tx.dst.includes(query)));
  }

  function renderTransactions() {
    const rows = filteredTransactions();
    const turnover = rows.reduce((sum, tx) => sum + Number(tx.sum_kzt), 0);
    const uniquePairs = new Set(rows.map(tx => `${tx.src}>${tx.dst}`)).size;
    const uniqueNodes = new Set(rows.flatMap(tx => [tx.src, tx.dst])).size;
    $("#transactionMetrics").innerHTML = [
      ["События", fmtInt(rows.length), `${((rows.length / D.meta.n_transactions) * 100).toFixed(1)}% выборки`, true],
      ["Оборот", fmtMoney(turnover), "после фильтра"],
      ["Пары", fmtInt(uniquePairs), "src → dst"],
      ["Узлы", fmtInt(uniqueNodes), "участники"],
      ["Средняя сумма", fmtMoney(rows.length ? turnover / rows.length : 0), "на транзакцию"],
      ["Максимум", fmtMoney(Math.max(...rows.map(tx => tx.sum_kzt), 0)), "одна транзакция"]
    ].map(item => metricCard(...item)).join("");

    const dailyMap = new Map(D.daily.map(day => [day.date, { date: day.date, sum_kzt: 0, n_tx: 0 }]));
    rows.forEach(tx => { const day = dailyMap.get(tx.date); day.sum_kzt += Number(tx.sum_kzt); day.n_tx += 1; });
    lineChart($("#txTimeline"), [...dailyMap.values()], "sum_kzt", "n_tx");
    histogram($("#txAmountHistogram"), rows.map(tx => tx.sum_kzt), 18, true);
    const repeatRows = [1,2,3,4,5].map(n => ({ label: n === 5 ? "5+" : String(n), value: D.edges.filter(edge => n === 5 ? edge.n_tx >= 5 : edge.n_tx === n).length }));
    barChart($("#repeatChart"), repeatRows, "label", "value");

    const tableRows = [...rows].sort((a, b) => b.date.localeCompare(a.date) || b.sum_kzt - a.sum_kzt);
    $("#txTableCount").textContent = `${fmtInt(rows.length)} строк · показано до 150`;
    $("#txFilterSummary").textContent = rows.length === D.meta.n_transactions ? "Все транзакции" : `${fmtInt(rows.length)} после фильтра`;
    $("#transactionsTable").innerHTML = tableHtml(tableRows, [
      { label: "Дата", value: "date" },
      { label: "Плательщик", value: "src", className: "gid-cell" },
      { label: "Получатель", value: "dst", className: "gid-cell" },
      { label: "Сумма", value: "sum_kzt", format: fmtMoney, className: "money-cell" }
    ], { maxRows: 150, clickable: true, rowData: row => `data-gid="${row.dst}"` });
    $$("#transactionsTable tr[data-gid]").forEach(row => row.addEventListener("click", () => openNode(row.dataset.gid)));
  }

  function setupTransactions() {
    let timer;
    const update = () => { clearTimeout(timer); timer = setTimeout(renderTransactions, 100); };
    $("#txGidFilter").addEventListener("input", update);
    $("#txMinAmount").addEventListener("input", update);
    renderTransactions();
  }

  function renderClusters() {
    const clusterRows = [...D.clusters].sort((a, b) => b.n_nodes - a.n_nodes);
    const method = P.run_metadata?.cluster_method || P.run_metadata?.clustering_method || P.run_metadata?.cluster_algorithm || P.run_metadata?.cluster_metadata?.method;
    $("#clusterMethodText").textContent = D.meta.has_production_clusters
      ? `${method ? `${method}: ` : "Production-разбиение: "}показаны рассчитанные backend-кластеры и их числовые гипотезы. Кластер не равен организованной группе.`
      : "Production clusters.csv не найден; показан Louvain-baseline на симметризованном графе. Кластер не равен организованной группе.";
    const clusterColumns = [
      { label: "ID", value: "cluster_id", format: value => `#${value}` },
      { label: "Узлы", value: "n_nodes", format: fmtInt },
      { label: "Seed", value: "n_seed", format: fmtInt },
      { label: "Ср. depth", value: "mean_depth", format: value => Number(value).toFixed(2) },
      { label: "Внутр. рёбра", value: "n_internal_edges", format: fmtInt },
      { label: "Внутр. сумма", value: "internal_kzt", format: fmtMoney, className: "money-cell" }
    ];
    if (clusterRows.some(row => row.hypothesis)) clusterColumns.push({ label: "Гипотеза", value: "hypothesis" });
    if (clusterRows.some(row => row.top_gids)) clusterColumns.push({ label: "Top gid", value: "top_gids" });
    $("#clustersTable").innerHTML = tableHtml(clusterRows, clusterColumns, { clickable: true, rowData: row => `data-cluster="${row.cluster_id}"` });
    $$("#clustersTable tr[data-cluster]").forEach(row => row.addEventListener("click", () => {
      $('.nav-item[data-view="network"]').click();
      $("#clusterFilter").value = row.dataset.cluster;
      network.filters.cluster = row.dataset.cluster;
      network.filters.component = "all";
      $("#componentFilter").value = "all";
      network.resetTransform(); network.applyFilters();
    }));

    $("#componentsTable").innerHTML = tableHtml(D.components, [
      { label: "ID", value: "component_id", format: value => `#${value}` },
      { label: "Узлы", value: "n_nodes", format: fmtInt },
      { label: "Рёбра", value: "n_edges", format: fmtInt },
      { label: "Seed", value: "n_seed", format: fmtInt },
      { label: "Сумма", value: "sum_kzt", format: fmtMoney, className: "money-cell" }
    ], { clickable: true, rowData: row => `data-component="${row.component_id}"` });
    $$("#componentsTable tr[data-component]").forEach(row => row.addEventListener("click", () => {
      $('.nav-item[data-view="network"]').click();
      $("#componentFilter").value = row.dataset.component;
      network.filters.component = row.dataset.component;
      network.filters.cluster = "all";
      $("#clusterFilter").value = "all";
      network.resetTransform(); network.applyFilters();
    }));
  }

  const rawTable = { page: 0, size: 50, dataset: "nodes", search: "" };
  const dataColumns = {
    nodes: [
      ["gid", "gid"], ["depth", "depth"], ["seed", "is_seed"], ["in_deg", "in_deg"], ["out_deg", "out_deg"], ["in_kzt", "in_kzt"], ["out_kzt", "out_kzt"], ["FIFO_1d", "fifo_match_1d"], ["max_in/day", "max_in_sources_day"], ["max_out/day", "max_out_targets_day"], ["PageRank", "pagerank_amount"], ["betweenness", "betweenness"], ["component", "component_id"], ["cluster", "cluster_id"], ["censored", "truncated_by_depth"], ["role", "role"], ["role_score", "role_score"], ["secondary_role", "secondary_role"], ["uncertainty", "uncertainty_reason"], ["p_continue", "p_continue"], ["priority", "priority_score"]
    ],
    edges: [["src", "src"], ["dst", "dst"], ["sum_kzt", "sum_kzt"], ["n_tx", "n_tx"], ["depth", "depth"], ["reciprocal", "reciprocal"]],
    transactions: [["date", "date"], ["src", "src"], ["dst", "dst"], ["sum_kzt", "sum_kzt"]]
  };

  function renderRawData() {
    const source = D[rawTable.dataset];
    const query = rawTable.search.toLowerCase();
    const filtered = query ? source.filter(row => Object.values(row).some(value => String(value ?? "").toLowerCase().includes(query))) : source;
    const pages = Math.max(1, Math.ceil(filtered.length / rawTable.size));
    rawTable.page = clamp(rawTable.page, 0, pages - 1);
    const pageRows = filtered.slice(rawTable.page * rawTable.size, (rawTable.page + 1) * rawTable.size);
    const cols = dataColumns[rawTable.dataset].map(([label, value]) => ({
      label, value,
      format: (raw, row) => value.endsWith("kzt") ? fmtMoney(raw) : (["pagerank_amount", "betweenness", "fifo_match_1d", "role_score", "p_continue", "priority_score"].includes(value) ? fmtScore(raw) : raw),
      className: value === "gid" || value === "src" || value === "dst" ? "gid-cell" : (value.endsWith("kzt") ? "money-cell" : "")
    }));
    $("#rawDataTable").innerHTML = tableHtml(pageRows, cols, { clickable: rawTable.dataset === "nodes", rowData: row => rawTable.dataset === "nodes" ? `data-gid="${row.gid}"` : "" });
    $("#dataPageLabel").textContent = `${rawTable.page + 1} / ${pages} · ${fmtInt(filtered.length)} строк`;
    $("#dataTableTitle").textContent = rawTable.dataset === "nodes" ? "Узлы" : rawTable.dataset === "edges" ? "Рёбра" : "Транзакции";
    $("#dataPrev").disabled = rawTable.page === 0;
    $("#dataNext").disabled = rawTable.page >= pages - 1;
    $$("#rawDataTable tr[data-gid]").forEach(row => row.addEventListener("click", () => openNode(row.dataset.gid)));
  }

  function setupRawData() {
    $("#dataTableSelect").addEventListener("change", event => { rawTable.dataset = event.target.value; rawTable.page = 0; renderRawData(); });
    $("#dataSearch").addEventListener("input", event => { rawTable.search = event.target.value.trim(); rawTable.page = 0; renderRawData(); });
    $("#dataPrev").addEventListener("click", () => { rawTable.page -= 1; renderRawData(); });
    $("#dataNext").addEventListener("click", () => { rawTable.page += 1; renderRawData(); });
    renderRawData();
  }

  function renderQuality() {
    const duplicateExact = (() => {
      const seen = new Set(), duplicates = [];
      D.transactions.forEach(tx => {
        const key = `${tx.src}|${tx.dst}|${tx.date}|${tx.sum_kzt}`;
        if (seen.has(key)) duplicates.push(key); else seen.add(key);
      });
      return duplicates.length;
    })();
    const checks = [
      [D.meta.reconciliation_ok, "Edges ↔ transactions", D.meta.reconciliation_ok ? "Суммы и counts сходятся" : "Обнаружено расхождение"],
      [true, "Идентификаторы", "Переданы в браузер как строки"],
      [D.meta.n_isolated === 19, "Изолированные seed", `${D.meta.n_isolated} узлов сохранено`],
      [false, "Точные повторы", `${duplicateExact} строк сохранено как события`]
    ];
    $("#qualityStatus").textContent = D.meta.reconciliation_ok ? "Базовые проверки пройдены" : "Требуется проверка";
    $("#qualityChecks").innerHTML = checks.map(([ok, title, note]) => `<div class="quality-check ${ok ? "ok" : "warn"}"><strong>${ok ? "✓" : "!"} ${escapeHtml(title)}</strong><span>${escapeHtml(note)}</span></div>`).join("");
  }

  function addAiMessage(kind, label, text) {
    const conversation = $("#aiConversation");
    const article = document.createElement("article");
    article.className = `message ${kind === "user" ? "user-message" : kind === "offline" ? "offline-message" : "assistant-message"}`;
    article.innerHTML = `<span>${escapeHtml(label)}</span><p>${escapeHtml(text)}</p>`;
    conversation.appendChild(article);
    conversation.scrollTop = conversation.scrollHeight;
  }

  function localAnalystAnswer(query) {
    const gidMatch = query.match(/\d{15,}/);
    if (gidMatch) {
      const node = nodeById.get(gidMatch[0]);
      if (!node) return `Узел ${gidMatch[0]} не найден в локальной выборке. Проверьте точность gid.`;
      const lines = [
        `gid ${node.gid}: роль — ${roleLabel(node.role)}; уверенность — ${fmtPercent(node.role_score)}; приоритет — ${fmtScore(node.priority_score)}.`,
        `Наблюдаемый поток: вход ${fmtMoney(node.in_kzt)} от ${fmtInt(node.in_deg)} источников; выход ${fmtMoney(node.out_kzt)} к ${fmtInt(node.out_deg)} получателям.`,
        node.evidence ? `Основания: ${node.evidence}` : "Production evidence пока не загружен.",
        node.uncertainty_reason ? `Неопределённость: ${node.uncertainty_reason}` : "",
        node.truncated_by_depth ? "Ограничение: depth=4, поэтому отсутствие исходящих переводов может быть следствием границы обхода." : ""
      ].filter(Boolean);
      return lines.join("\n");
    }
    const normalized = query.toLowerCase();
    if (normalized.includes("приоритет") || normalized.includes("top") || normalized.includes("перв")) {
      const rows = (P.top_nodes || []).length
        ? P.top_nodes.slice(0, 5)
        : [...D.nodes].filter(node => node.priority_score != null).sort((a, b) => Number(b.priority_score) - Number(a.priority_score)).slice(0, 5);
      if (!rows.length) return "top_nodes.csv ещё не загружен. Пересоберите dashboard после выполнения production pipeline.";
      return `Первые кандидаты по priority_score:\n${rows.map((row, index) => `${index + 1}. ${row.gid} — ${fmtScore(row.priority_score)} (${roleLabel(row.role)})`).join("\n")}\nПриоритет задаёт очередь проверки и не является оценкой виновности.`;
    }
    if (normalized.includes("depth") || normalized.includes("ценз") || normalized.includes("terminal")) {
      const uncertain = D.nodes.filter(node => node.truncated_by_depth && (node.p_continue == null || Number(node.p_continue) >= .2)).length;
      return `В выборке ${fmtInt(D.meta.n_censored)} узла на depth=4 без наблюдаемого выхода; ${fmtInt(uncertain)} из них нельзя считать надёжно подтверждёнными terminal только по топологии. Нужны исходящие операции следующего шага и последующего окна.`;
    }
    if (normalized.includes("устойчив") || normalized.includes("удален") || normalized.includes("связност")) {
      if (!(P.resilience || []).length) return "resilience.csv пока не загружен. Production pipeline должен сохранить LCC, число компонент и остаток потока для top-N удалений.";
      const rows = P.resilience.slice(-5);
      return `Локально доступны ${fmtInt(P.resilience.length)} сценариев resilience. Последние значения:\n${rows.map(row => `${row.strategy || "strategy"}, N=${row.n_removed}: LCC=${fmtPercent(row.lcc_ratio)}, компонент=${row.n_components ?? "—"}`).join("\n")}`;
    }
    return "Локальный AI API недоступен. Офлайн-fallback умеет показать числовую карточку точного gid, top priority, depth-4 ограничения и загруженную resilience-таблицу. Введите gid или выберите быстрый запрос справа.";
  }

  function setupAi() {
    const form = $("#aiForm");
    const question = $("#aiQuestion");
    const submit = $("#aiSubmit");
    const state = $("#aiRequestState");
    const status = $("#aiApiStatus");
    const setOffline = message => {
      status.textContent = "Офлайн-режим";
      status.className = "source-badge offline";
      state.textContent = message;
    };
    if (location.protocol === "file:") setOffline("Открыто через file:// — API недоступен");
    else state.textContent = "API будет проверен при отправке";

    let suggestedAction = null;
    question.addEventListener("input", () => { suggestedAction = null; });
    $$("#aiSuggestions button").forEach(button => button.addEventListener("click", () => {
      question.value = button.dataset.prompt || "";
      suggestedAction = button.dataset.action ? JSON.parse(button.dataset.action) : null;
      question.focus();
    }));
    question.addEventListener("keydown", event => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        form.requestSubmit();
      }
    });

    form.addEventListener("submit", async event => {
      event.preventDefault();
      const query = question.value.trim();
      if (!query) { question.focus(); return; }
      const action = suggestedAction;
      suggestedAction = null;
      addAiMessage("user", "Вы", query);
      question.value = "";
      submit.disabled = true;
      state.textContent = "Запрос к локальному /api/query…";

      if (location.protocol === "file:") {
        addAiMessage("offline", "Локальный fallback", localAnalystAnswer(query));
        setOffline("API не вызывается из file://");
        submit.disabled = false;
        return;
      }

      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 20_000);
      try {
        const response = await fetch(apiUrl(action ? "/api/action" : "/api/query"), {
          method: "POST",
          headers: { "Content-Type": "application/json", "Accept": "application/json" },
          body: JSON.stringify(action || { question: query }),
          signal: controller.signal
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const contentType = response.headers.get("content-type") || "";
        const payload = contentType.includes("application/json") ? await response.json() : await response.text();
        let answer = typeof payload === "string" ? payload : (payload.answer ?? payload.response ?? payload.result);
        if (answer == null && payload && typeof payload === "object") {
          const parts = [payload.message];
          if (payload.data && Object.keys(payload.data).length) parts.push(JSON.stringify(payload.data, null, 2));
          if (Array.isArray(payload.warnings) && payload.warnings.length) parts.push(`Предупреждения: ${payload.warnings.join("; ")}`);
          if (payload.caveat) parts.push(payload.caveat);
          answer = parts.filter(Boolean).join("\n\n");
        }
        if (typeof answer !== "string") answer = JSON.stringify(answer ?? payload, null, 2);
        addAiMessage("assistant", "AI-аналитик", answer);
        status.textContent = "Локальный API подключён";
        status.className = "source-badge";
        state.textContent = "Ответ получен · данные остаются локально";
      } catch (error) {
        addAiMessage("offline", "Локальный fallback", `${localAnalystAnswer(query)}\n\nAPI /api/query недоступен: ${error.name === "AbortError" ? "тайм-аут" : error.message}.`);
        setOffline("API недоступен; показан локальный fallback");
      } finally {
        clearTimeout(timeout);
        submit.disabled = false;
      }
    });
  }

  renderHeader();
  renderMetrics();
  renderOverview();
  renderPatterns();
  renderAnalytics();
  renderClusters();
  setupTransactions();
  setupRawData();
  renderQuality();
  setupAi();
  network.setup();
  setupNavigation();
  window.HackAlemNarratives?.init();
  window.HACKALEM_READY = true;
})();
