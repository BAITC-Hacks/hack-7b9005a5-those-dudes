// Test the real upload/bootstrap scripts with a small DOM adapter, without uploads or paid AI calls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const source = name => fs.readFileSync(path.join(__dirname, 'dist', name), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
const jobId = 'a'.repeat(32);

function element() {
  const classes = new Set();
  return {
    handlers: {}, dataset: {}, attributes: {}, files: [], textContent: '', disabled: false,
    classList: { add: value => classes.add(value), remove: value => classes.delete(value), contains: value => classes.has(value), toggle(value, enabled) { if (enabled ?? !classes.has(value)) classes.add(value); else classes.delete(value); } },
    addEventListener(name, fn) { this.handlers[name] = fn; },
    setAttribute(key, value) { this.attributes[key] = value; },
    removeAttribute(key) { delete this.attributes[key]; },
    closest() { return this; }, focus() {}, reportValidity() { return true; },
  };
}

function harness({ script, search = '', responses = [], bundleReady = true, appReady = true } = {}) {
  const elements = new Map();
  const get = selector => { if (!elements.has(selector)) elements.set(selector, element()); return elements.get(selector); };
  const exports = ['results.zip', 'nodes_roles.csv', 'clusters.csv', 'top_nodes.csv'].map(name => Object.assign(element(), { dataset: { export: name } }));
  const calls = [];
  const scripts = [];
  const window = { location: { search, assign: value => { window.navigation = value; } }, handlers: {}, addEventListener(name, fn) { this.handlers[name] = fn; } };
  const document = {
    querySelector: get, querySelectorAll: () => exports, body: element(), createElement: element,
    head: { appendChild(script) { scripts.push(script.src); if (script.src.endsWith('dashboard_data.js') && bundleReady) window.HACKALEM_DATA = {}; if (script.src.endsWith('/app.js') && appReady) window.HACKALEM_READY = true; script.onload(); } },
  };
  const context = vm.createContext({
    window, document, URLSearchParams, encodeURIComponent, AbortSignal, console,
    setInterval: () => 1, clearInterval() {}, setTimeout: fn => { fn(); return 1; },
    FormData: class { constructor() { this.fields = []; } append(...args) { this.fields.push(args); } },
    fetch: async (url, options) => {
      calls.push({ url, options });
      const item = responses.shift();
      if (!item) throw new Error(`Unexpected request: ${url}`);
      return { ok: item.ok ?? true, status: item.status ?? 200, json: async () => item.body };
    },
  });
  vm.runInContext(source(script), context);
  return { window, document, get, calls, scripts, exports };
}

test('bootstrap pins bundle, API calls, and all three CSV exports to the same run', async () => {
  const h = harness({ script: 'bootstrap.js', search: `?run=${jobId}`, responses: [{ body: { status: 'ready' } }] });
  await tick();
  assert.deepEqual(h.scripts, [`/api/datasets/${jobId}/dashboard_data.js`, '/app.js']);
  assert.equal(h.window.HACKALEM_CONTEXT.apiUrl('/api/query'), `/api/query?run=${jobId}`);
  assert.equal(h.window.HACKALEM_CONTEXT.apiUrl('/api/explain-node'), `/api/explain-node?run=${jobId}`);
  for (const link of h.exports) assert.equal(link.href, `/api/datasets/${jobId}/exports/${link.dataset.export}`);
  assert(h.get('#dashboardLoader').classList.contains('hidden'));
});

test('bootstrap rejects malformed run links before making any request', async () => {
  const h = harness({ script: 'bootstrap.js', search: '?run=../../output' });
  await tick();
  assert.equal(h.calls.length, 0);
  assert.equal(h.scripts.length, 0);
  assert(h.get('#dashboardLoader').classList.contains('load-failed'));
});

test('bootstrap keeps a failed or incomplete dataset out of the dashboard', async () => {
  const h = harness({ script: 'bootstrap.js', responses: [{ body: { status: 'failed', message: 'Файлы не согласованы' } }] });
  await tick();
  assert.equal(h.scripts.length, 0);
  assert.equal(h.get('#dashboardLoadMessage').textContent, 'Файлы не согласованы');
});

test('bootstrap surfaces frontend initialization errors instead of exposing a blank graph', async () => {
  const h = harness({ script: 'bootstrap.js', appReady: false, responses: [{ body: { status: 'ready' } }] });
  await tick();
  assert(h.get('#dashboardLoader').classList.contains('load-failed'));
  assert(!h.get('#dashboardLoader').classList.contains('hidden'));
});

function selectFiles(h, change = {}) {
  for (const name of ['nodes', 'edges', 'transactions']) {
    h.get(`#${name}File`).files = [{ name: `${name}.parquet`, size: 1000, ...change }];
    h.get(`#${name}File`).handlers.change();
  }
}

test('upload submits all three files, polls its own run and automatically opens the matching dashboard', async () => {
  const h = harness({ script: 'upload.js', responses: [
    { body: { run_id: 'default', status: 'ready', counts: { nodes: 3, edges: 2, transactions: 2 } } },
    { body: { run_id: jobId, status: 'queued' } },
    { body: { run_id: jobId, status: 'running', message: 'Вычисляем' } },
    { body: { run_id: jobId, status: 'ready' } },
  ] });
  await tick();
  selectFiles(h);
  assert.equal(h.get('#uploadSubmit').disabled, false);
  await h.get('#uploadForm').handlers.submit({ preventDefault() {} });
  assert.deepEqual(h.calls.map(x => x.url), ['/api/datasets/current', '/api/datasets', `/api/datasets/${jobId}`, `/api/datasets/${jobId}`]);
  assert.equal(h.calls[1].options.headers['X-HackAlem-Action'], 'upload');
  assert.deepEqual(h.calls[1].options.body.fields.map(x => x[0]), ['nodes', 'edges', 'transactions']);
  assert.equal(h.window.navigation, `/dashboard?run=${jobId}`);
  let prevented = false;
  h.window.handlers.beforeunload({ preventDefault: () => { prevented = true; } });
  assert.equal(prevented, false, 'automatic ready navigation must not raise a leave-page confirmation');
});

test('upload rejects oversized files locally before sending multipart data', async () => {
  const h = harness({ script: 'upload.js', responses: [{ body: { status: 'unavailable' } }] });
  await tick();
  selectFiles(h, { size: 20 * 1024 * 1024 + 1 });
  await h.get('#uploadForm').handlers.submit({ preventDefault() {} });
  assert.equal(h.calls.length, 1);
  assert.match(h.get('#uploadError').textContent, /20 МБ/);
});

test('failed analysis preserves selected files, shows the validation error and allows retry', async () => {
  const h = harness({ script: 'upload.js', responses: [
    { body: { status: 'unavailable' } }, { body: { run_id: jobId, status: 'queued' } },
    { body: { status: 'failed', message: 'Previous phase', error: 'Неверная схема nodes.parquet' } },
  ] });
  await tick();
  selectFiles(h);
  await h.get('#uploadForm').handlers.submit({ preventDefault() {} });
  assert.equal(h.get('#uploadError').textContent, 'Неверная схема nodes.parquet');
  assert.equal(h.get('#uploadFields').disabled, false);
  assert.equal(h.get('#uploadSubmit').disabled, false);
  assert.equal(h.get('#nodesFile').files[0].name, 'nodes.parquet');
  assert.equal(h.get('#progressTitle').textContent, 'Проверьте исходные файлы');
  assert.match(h.get('#progressMessage').textContent, /Анализ не завершён/);
  assert(h.get('#progressNote').classList.contains('hidden'));
  assert.equal(h.window.navigation, undefined);
});

test('HTTP 400 validation rejection clears in-progress instructions and preserves files for retry', async () => {
  const h = harness({ script: 'upload.js', responses: [
    { body: { status: 'unavailable' } },
    { ok: false, status: 400, body: { error: 'nodes.parquet: отсутствует колонка is_seed' } },
  ] });
  await tick();
  selectFiles(h);
  await h.get('#uploadForm').handlers.submit({ preventDefault() {} });
  assert.equal(h.get('#progressTitle').textContent, 'Файлы не приняты');
  assert.equal(h.get('#progressMessage').textContent, 'Исправьте выбранные файлы и повторите загрузку.');
  assert(h.get('#progressNote').classList.contains('hidden'));
  assert(h.get('#uploadProgress').classList.contains('paused'));
  assert.match(h.get('#uploadError').textContent, /is_seed/);
  assert.equal(h.get('#uploadFields').disabled, false);
  assert.equal(h.get('#uploadSubmit').disabled, false);
  assert.equal(h.get('#nodesFile').files[0].name, 'nodes.parquet');
  assert.equal(h.calls.length, 2);
  assert.equal(h.window.navigation, undefined);
});

test('lost status connection allows resuming the original job without re-uploading files', async () => {
  const h = harness({ script: 'upload.js', responses: [
    { body: { status: 'unavailable' } }, { body: { run_id: jobId, status: 'queued' } },
    ...Array.from({ length: 3 }, () => ({ ok: false, status: 503, body: { error: 'Ожидание соединения' } })),
    { body: { status: 'ready' } },
  ] });
  await tick();
  selectFiles(h);
  await h.get('#uploadForm').handlers.submit({ preventDefault() {} });
  assert.equal(h.get('#uploadSubmit').disabled, true);
  assert(!h.get('#resumeStatus').classList.contains('hidden'));
  await h.get('#resumeStatus').handlers.click();
  assert.equal(h.window.navigation, `/dashboard?run=${jobId}`);
  assert.equal(h.calls.filter(x => x.url === '/api/datasets').length, 1);
});
